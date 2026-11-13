"""Session-owned Goal control and evidence review; scheduling stays in the host.

Goal never replaces Todo or the output chain. Pause is an authorization change,
not a new goal version. Explicit submit/start/resume enables automatic input.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from clonoth_runtime import load_runtime_config

from engine.hooks.types import HookResult
from .goal_review import ReviewInputError, build_input, review_cache_blocks

log = logging.getLogger(__name__)
MAX_BYTES = 4 * 1024 * 1024
MAX_TEXT = 16000
MAX_REVIEWS = 64
MAX_REQUESTS = 128
REVIEW_TIMEOUT = 180
WAIT_TIMEOUT = 1800
REVIEW_NODE = "ide.goal_reviewer"
ACTIVE = {"running", "waiting", "reviewing"}
STATUSES = ACTIVE | {"empty", "paused", "completed", "blocked"}
_SERVICES: dict[int, "GoalService"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, key: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise HTTPException(422, f"{key} must be {'an optional' if empty else 'a nonempty'} string (max {limit})")
    return value.strip()


def _empty(sid: str, *, child: bool = False) -> dict:
    return {"schema_version": 1, "session_id": sid, "revision": 0, "goal": "", "criteria": "",
            "goal_version": 0, "status": "empty", "pause_reason": "", "rounds": 0,
            "max_rounds": 8, "stagnation": 0, "reviews": [], "updated_at": None,
            "is_child": child, "reviewer_provider": "", "reviewer_model": "",
            "_identity": uuid4().hex, "_roots": [], "_requests": {}, "_candidate": None,
            "_review": None, "_pending_input": None, "_wait_started": None,
            "_prior_runs": [], "_interruptions": [], "_review_attempts": {}, "_input_seq": 0,
            "_system_tasks": [], "_review_outputs": [], "_current_execution": None}


def _public(data: dict) -> dict:
    execution = data.get("_current_execution")
    roots = data["_roots"]
    if not execution or data.get("_needs_input") or not roots or execution["task_id"] != roots[-1]:
        execution = None
    return {**copy.deepcopy({k: v for k, v in data.items() if not k.startswith("_")}),
            "goal_id": data["_identity"], "execution": copy.deepcopy(execution),
            "capabilities": {"create": True, "progress": True}}


def parse_verdict(text: str) -> dict:
    """Strict JSON: malformed output is a technical failure, never an approval."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError("评审未返回有效 JSON。") from None
    fields = {"passed", "progress", "feedback", "missing_evidence", "needs_user"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("评审字段不完整或包含未知字段。")
    if any(type(data[k]) is not bool for k in ("passed", "progress", "needs_user")):
        raise ValueError("评审布尔字段无效。")
    if not isinstance(data["feedback"], str) or not data["feedback"].strip() or len(data["feedback"]) > 12000:
        raise ValueError("评审反馈无效。")
    missing = data["missing_evidence"]
    if not isinstance(missing, list) or len(missing) > 50 or any(not isinstance(s, str) or not s.strip() or len(s) > 2000 for s in missing):
        raise ValueError("评审缺失证据列表无效。")
    if data["passed"] and (missing or data["needs_user"]):
        raise ValueError("评审通过结论与缺失证据或用户决定相矛盾。")
    return data


class GoalService:
    def __init__(self, runtime: Any, root: Path, *, background: bool = True):
        self.runtime = runtime
        self.root = Path(root)
        self.documents: dict[str, dict] = {}
        self.enabled = True
        self.ready = False
        self._runtime_error = ""
        self._restart_preparing = False
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.unsubscribe = runtime.subscribe(self.on_event)
        try:
            if getattr(runtime, "ready", False) and not self.ready:
                self.on_event({"kind": "ready", **getattr(runtime, "planned_restart", {})})
            if background:
                self.thread = threading.Thread(target=self._run, name="ide-goal", daemon=True)
                self.thread.start()
        except Exception:
            self.enabled = False
            self.stopped.set()
            self.unsubscribe()
            raise

    def _path(self, sid: str) -> Path:
        return self.root / "data" / "ide" / "goals" / "sessions" / (hashlib.sha256(sid.encode()).hexdigest() + ".json")

    def _load(self, sid: str) -> dict:
        if sid in self.documents:
            return self.documents[sid]
        path = self._path(sid)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            data = json.loads(raw)
            if (len(raw) > MAX_BYTES or not isinstance(data, dict) or data.get("schema_version") != 1
                    or data.get("session_id") != sid or type(data.get("revision")) is not int
                    or data["revision"] < 0 or data.get("status") not in STATUSES
                    or type(data.get("goal_version")) is not int or type(data.get("rounds")) is not int
                    or type(data.get("stagnation")) is not int or not isinstance(data.get("reviews"), list)
                    or not isinstance(data.get("_roots"), list) or not isinstance(data.get("_requests"), dict)
                    or not isinstance(data.get("goal"), str) or not isinstance(data.get("criteria"), str)):
                raise ValueError("invalid document")
        except FileNotFoundError:
            data = _empty(sid)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(500, "Goal 数据损坏，未覆盖原文件。") from None
        self.documents[sid] = data
        return data

    def _save(self, data: dict) -> None:
        saved = copy.deepcopy(data)
        saved.update(revision=data["revision"] + 1, updated_at=_now())
        try:
            raw = json.dumps(saved, ensure_ascii=False, indent=2).encode()
            if len(raw) > MAX_BYTES:
                raise HTTPException(422, "Goal 记录总长度超限。")
            path = self._path(data["session_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.root / "data" / "temp"
            temp.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=temp, prefix="goal-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
            data.update(revision=saved["revision"], updated_at=saved["updated_at"])
            self.documents[data["session_id"]] = saved
            self.runtime.emit_plugin_event("ide_goal_updated", {
                "session_id": data["session_id"], "revision": data["revision"],
                "status": data["status"], "goal_version": data["goal_version"],
            })
        except Exception as exc:
            self._block(data, "Goal 保存未确认成功：" + str(getattr(exc, "detail", exc)))
            raise

    def _block(self, data: dict, reason: str) -> None:
        # Revoke local permission before attempting any fallible host operation.
        current = self.documents.setdefault(data["session_id"], copy.deepcopy(data))
        current.update(status="blocked", pause_reason=reason[:700], _wait_started=None)
        try:
            self.runtime.set_paused(list(set(current["_roots"] + data["_roots"])), True)
        except Exception:
            log.exception("Goal execution pause could not be persisted")

    def read(self, sid: str) -> dict:
        if self._runtime_error:
            raise HTTPException(503, self._runtime_error)
        with self.runtime.transaction():
            info = self.runtime.session_info(sid)
            if info["is_child"]:
                return _public(_empty(info["session_id"], child=True))
            return _public(self._load(info["session_id"]))

    def _pause(self, data: dict, reason: str) -> None:
        data["status"] = "paused"
        data["pause_reason"] = reason
        data["_wait_started"] = None
        # Revoke local permission before I/O: a failed durable pause must not
        # leave the old active document eligible for the next worker tick.
        existing = self.documents.get(data["session_id"])
        if existing is not None:
            existing.update(status="paused", pause_reason=reason, _wait_started=None)
        self.runtime.set_paused(data["_roots"], True)

    def _cleanup_created_goal(self, data: dict, *, activate: bool = False) -> dict:
        """Recoverable second phase: retire old evidence only after the new Goal is durable.

        Runtime evidence and Goal records are separate files. Keep the pending IDs
        until forget and its acknowledgement save both succeed; reload may retry
        cleanup, but only the original uninterrupted create grants automatic start.
        """
        pending = data.get("_create_cleanup")
        if pending is None:
            return data
        try:
            # forget closes delivery/history but does not stop a live reviewer.
            # Only retire plugin system work; old ordinary execution is not cancelled.
            for task in self.runtime.snapshot(pending)["tasks"]:
                if task.get("is_system_task") and task["status"] in {"pending", "running", "suspended"}:
                    result = self.runtime.cancel_system_task(task["task_id"])
                    if not result.get("ok"):
                        raise RuntimeError("旧评审取消未确认成功：" + str(result.get("error", "unknown")))
            self.runtime.forget(pending)
            clean = copy.deepcopy(data)
            clean.pop("_create_cleanup", None)
            clean.update(status="running" if activate else "paused",
                         pause_reason="" if activate else "旧 Goal 专属记录已清理，请明确恢复新目标。",
                         _wait_started=time.time() if activate else None)
            self._save(clean)
            return clean
        except Exception as exc:
            blocked = copy.deepcopy(data)
            reason = "新 Goal 已保存，但旧 Goal 专属记录清理未确认成功；请检查后恢复：" + str(getattr(exc, "detail", exc))[:300]
            blocked.update(status="blocked", pause_reason=reason, _wait_started=None)
            self.documents[data["session_id"]] = blocked
            try:
                self._save(blocked)
            except Exception:
                log.exception("Could not persist Goal create cleanup failure")
            raise HTTPException(503, reason) from exc

    def operate(self, sid: str, patch: dict) -> dict:
        if self._runtime_error:
            raise HTTPException(503, self._runtime_error)
        allowed = {"action", "expected_revision", "expected_session_id", "request_id", "goal", "criteria", "reviewer_provider", "reviewer_model"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise HTTPException(422, "未知 Goal 操作字段。")
        action = patch.get("action")
        if action not in {"create", "set", "submit", "start", "pause", "resume", "clear"}:
            raise HTTPException(422, "未知 Goal 操作。")
        if action not in {"create", "set", "submit"} and set(patch) & {"goal", "criteria", "reviewer_provider", "reviewer_model"}:
            raise HTTPException(422, "只有 create、set 或 submit 可以提交目标或评审配置。")
        expected = patch.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise HTTPException(422, "expected_revision 必须是非负整数。")
        expected_sid = _text(patch.get("expected_session_id"), "expected_session_id", 200)
        rid = _text(patch.get("request_id"), "request_id", 200)
        fingerprint = hashlib.sha256(json.dumps(patch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.runtime.transaction():
            if not self.enabled or not self.ready:
                raise HTTPException(503, "Goal 服务尚未就绪。")
            info = self.runtime.session_info(sid)
            if info["is_child"]:
                raise HTTPException(403, "子会话不能创建或控制 Goal。")
            sid = info["session_id"]
            if sid != expected_sid:
                raise HTTPException(409, "Goal 所属会话不同，请重新读取。")
            current = self._load(sid)
            prior = current["_requests"].get(rid)
            if prior is not None:
                if prior != fingerprint:
                    raise HTTPException(409, "request_id 已用于不同操作。")
                if action == "create" and current.get("_create_cleanup") is not None:
                    raise HTTPException(503, current["pause_reason"])
                return _public(current)
            if current["revision"] != expected:
                raise HTTPException(409, "Goal 已更新，请重新读取后修改。")
            if current.get("_create_cleanup") is not None:
                if action not in {"resume", "clear", "pause"}:
                    raise HTTPException(503, "旧 Goal 专属记录尚未清理，请检查后明确恢复或清除。")
                if action in {"resume", "clear"}:
                    current = self._cleanup_created_goal(copy.deepcopy(current))
            data = copy.deepcopy(current)
            if action == "create":
                if data["status"] not in {"empty", "completed"}:
                    raise HTTPException(409, "当前 Goal 尚未完成，不能创建新目标；请先处理当前目标。")
                fresh = _empty(sid)
                fresh.update(goal=_text(patch.get("goal"), "goal", MAX_TEXT),
                             criteria=_text(patch.get("criteria", ""), "criteria", MAX_TEXT, empty=True),
                             reviewer_provider=_text(patch.get("reviewer_provider", ""), "reviewer_provider", 200, empty=True),
                             reviewer_model=_text(patch.get("reviewer_model", ""), "reviewer_model", 200, empty=True),
                             revision=data["revision"], goal_version=data["goal_version"] + 1,
                             _requests=data["_requests"], _needs_input=True,
                             status="running", _wait_started=time.time())
                retired = [*data["_roots"], *(tid for run in data.get("_prior_runs", []) for tid in run),
                           *data.get("_system_tasks", []),
                           *[r["task_id"] for r in data["reviews"] if r.get("task_id")]]
                if (data.get("_review") or {}).get("task_id"):
                    retired.append(data["_review"]["task_id"])
                if retired:
                    fresh.update(_create_cleanup=list(dict.fromkeys(retired)), status="blocked",
                                 pause_reason="新 Goal 已保存，等待清理旧 Goal 专属记录。", _wait_started=None)
                data = fresh
            elif action in {"set", "submit"}:
                if action == "submit" and (data["rounds"] >= data["max_rounds"] or data["stagnation"] >= 3):
                    raise HTTPException(409, "累计预算已耗尽或连续无进展；提交不会清零，请明确清除后创建新 Goal。")
                goal = _text(patch.get("goal"), "goal", MAX_TEXT)
                criteria = _text(patch.get("criteria", data["criteria"] if action == "submit" else ""), "criteria", MAX_TEXT, empty=True)
                provider = _text(patch.get("reviewer_provider", data["reviewer_provider"]), "reviewer_provider", 200, empty=True)
                model = _text(patch.get("reviewer_model", data["reviewer_model"]), "reviewer_model", 200, empty=True)
                self._pause(data, "目标已保存，等待明确开始或恢复。")
                # Keep counters and historical verdicts when editing the same Goal.
                data.update(goal=goal, criteria=criteria, reviewer_provider=provider, reviewer_model=model)
                data["goal_version"] += 1
                data["_review_outputs"] = []
                if data["_roots"]:
                    data.setdefault("_prior_runs", []).append(list(data["_roots"]))
                    data["_roots"] = []
                data["_candidate"] = None
                data["_pending_input"] = None
                data["_current_execution"] = None
                data["_needs_input"] = True
                if action == "submit":
                    # The old roots stay paused. The next tick creates a new normal
                    # input; accepting this explicit submission is its authorization.
                    data.update(status="running", pause_reason="", _wait_started=time.time())
            elif action == "clear":
                self._pause(data, "")
                all_roots = [*data["_roots"], *(r for run in data.get("_prior_runs", []) for r in run)]
                review_ids = [*data.get("_system_tasks", []), *[r["task_id"] for r in data["reviews"] if r.get("task_id")]]
                if (data.get("_review") or {}).get("task_id"):
                    review_ids.append(data["_review"]["task_id"])
                review = data.get("_review")
                if review and review.get("task_id"):
                    self.runtime.cancel_system_task(review["task_id"])
                self.runtime.forget([*all_roots, *review_ids])
                # A content-free tombstone keeps CAS/idempotence across clear/recreate.
                fresh = _empty(sid)
                fresh["revision"] = data["revision"]
                fresh["goal_version"] = data["goal_version"] + 1
                fresh["_requests"] = data["_requests"]
                data = fresh
            elif not data["goal"]:
                raise HTTPException(409, "尚未设置 Goal。")
            elif action == "pause":
                if data["status"] == "completed":
                    raise HTTPException(409, "已完成的 Goal 不需要暂停。")
                self._pause(data, "用户暂停。")
            else:
                if data["status"] == "completed":
                    raise HTTPException(409, "Goal 已完成；如需新工作，请先修改目标。")
                if data["status"] in ACTIVE:
                    raise HTTPException(409, "Goal 已在推进。")
                candidate = data.get("_candidate") or {}
                pending_review = not data.get("_needs_input") and any(
                    r and r.get("goal_version") == data["goal_version"]
                    and r.get("candidate_id") == (candidate.get("candidate_id") or candidate.get("entry_task_id"))
                    and (r is data.get("_review") or (not r.get("applied") and r.get("verdict")))
                    for r in [data.get("_review"), *data["reviews"]])
                if (data["rounds"] >= data["max_rounds"] or data["stagnation"] >= 3) and not pending_review:
                    raise HTTPException(409, "累计预算已耗尽或连续无进展；恢复不会清零，请明确清除后创建新 Goal。")
                # An explicit recovery starts a fresh execution root when the old
                # one is irrecoverably lost. Preserve the evidence and failure facts.
                if data["_roots"]:
                    old = self.runtime.snapshot(data["_roots"])
                    if old.get("unresolved"):
                        data.setdefault("_prior_runs", []).append(list(data["_roots"]))
                        data.setdefault("_interruptions", []).extend(copy.deepcopy(old["unresolved"]))
                        data["_roots"] = []
                        data["_candidate"] = None
                        data["_needs_input"] = True
                data.update(status="running", pause_reason="", _wait_started=time.time())
            data["_requests"][rid] = fingerprint
            while len(data["_requests"]) > MAX_REQUESTS:
                del data["_requests"][next(iter(data["_requests"]))]
            self._save(data)
            if action == "create":
                data = self._cleanup_created_goal(data, activate=True)
            if action in {"start", "resume"}:
                try:
                    self.runtime.set_paused(data["_roots"], False)
                except Exception:
                    self.documents[sid].update(status="paused", pause_reason="恢复授权未确认成功。")
                    raise
            self.wake.set()
            return _public(data)

    def on_event(self, event: dict) -> None:
        if not self.enabled:
            return
        with self.runtime.transaction():
            kind = event.get("kind")
            if kind == "restart_preparing":
                self._restart_preparing = True
                for source in list(self.documents.values()):
                    if source["status"] not in ACTIVE:
                        continue
                    data = copy.deepcopy(source)
                    data["_restart_checkpoint"] = {
                        "restart_id": event["restart_id"], "goal_version": data["goal_version"],
                        "started": time.time(),
                    }
                    self._save(data)
                return
            if kind == "restart_aborted":
                self._restart_preparing = False
                for source in list(self.documents.values()):
                    if source.get("_restart_checkpoint", {}).get("restart_id") == event.get("restart_id"):
                        data = copy.deepcopy(source)
                        data.pop("_restart_checkpoint", None)
                        self._save(data)
                self.wake.set()
                return
            if kind == "ready":
                if self.ready:
                    return
                folder = self.root / "data" / "ide" / "goals" / "sessions"
                for path in folder.glob("*.json"):
                    try:
                        with path.open("rb") as source:
                            raw = source.read(MAX_BYTES + 1)
                        if len(raw) > MAX_BYTES:
                            raise ValueError("oversized goal")
                        sid = json.loads(raw)["session_id"]
                        if path != self._path(sid):
                            raise ValueError("invalid goal filename")
                        data = copy.deepcopy(self._load(sid))
                        if data.get("_create_cleanup") is not None:
                            data = self._cleanup_created_goal(data)
                        checkpoint = data.pop("_restart_checkpoint", None) or {}
                        preserved = set(event.get("preserved_task_ids") or event.get("resumed_task_ids") or [])
                        required = [*data["_roots"]]
                        review = data.get("_review")
                        if review and review.get("task_id"):
                            required.append(review["task_id"])
                        planned = (event.get("planned_restart") is True
                                   and checkpoint.get("restart_id") == event.get("restart_id")
                                   and checkpoint.get("goal_version") == data["goal_version"]
                                   and data["status"] in ACTIVE
                                   and set(required).issubset(preserved))
                        if planned:
                            downtime = max(0.0, time.time() - checkpoint["started"])
                            if data.get("_wait_started") is not None:
                                data["_wait_started"] += downtime
                            if review:
                                review["started"] += downtime
                            self._save(data)
                        elif data["status"] in ACTIVE:
                            self._pause(data, "服务重启或重新加载，等待明确恢复。")
                            self._save(data)
                        elif checkpoint:
                            self._save(data)
                        if data["_roots"]:
                            self.runtime.watch(data["_roots"])
                            self.runtime.set_paused(data["_roots"], not planned)
                    except Exception:
                        log.exception("Goal restoration failed: %s", path.name)
                self.ready = True
                self.wake.set()
                return
            sid = str(event.get("session_id") or "")
            if kind == "session_removed":
                data = self.documents.pop(sid, None)
                if data is None and self._path(sid).is_file():
                    data = self._load(sid)
                    self.documents.pop(sid, None)
                if data:
                    self.runtime.set_paused(data["_roots"], True)
                    roots = [*data["_roots"], *(r for run in data.get("_prior_runs", []) for r in run),
                             *data.get("_create_cleanup", [])]
                    reviews = [*data.get("_system_tasks", []), *[r["task_id"] for r in data["reviews"] if r.get("task_id")]]
                    if (data.get("_review") or {}).get("task_id"):
                        reviews.append(data["_review"]["task_id"])
                    self.runtime.forget([*roots, *reviews])
                self._path(sid).unlink(missing_ok=True)
                return
            if kind in {"input", "workspace"}:
                if sid not in self.documents and not self._path(sid).exists():
                    return
                data = copy.deepcopy(self._load(sid))
                if data["goal"] and data["status"] not in {"empty", "completed"}:
                    if kind == "input":
                        data["_candidate"] = None
                        data["_needs_input"] = True
                    was_active = data["status"] in ACTIVE
                    if was_active:
                        reason = "工作区已切换，Goal 暂停；目标和评审记录保留。" if kind == "workspace" else "收到新的用户输入，Goal 自动推进已暂停。"
                        self._pause(data, reason)
                    if kind == "input" or was_active:
                        self._save(data)
                return
            if kind == "candidate":
                candidate = event.get("candidate") or {}
                entry = str(candidate.get("entry_task_id") or "")
                if not entry:
                    return
                for source in list(self.documents.values()):
                    if not source["goal"] or not source["_roots"]:
                        continue
                    snap = self.runtime.snapshot(source["_roots"])
                    tasks = snap.get("tasks", [])
                    ids = set(tasks) if isinstance(tasks, dict) else {t.get("task_id") for t in tasks}
                    if entry not in ids and entry not in source["_roots"]:
                        continue
                    if candidate.get("session_id", sid) != source["session_id"]:
                        continue  # A child candidate is evidence, not a main result.
                    data = copy.deepcopy(source)
                    data["_candidate"] = copy.deepcopy(candidate)
                    data["_candidate"]["goal_version"] = data["goal_version"]
                    self._save(data)
            self.wake.set()

    def _run(self) -> None:
        while not self.stopped.is_set():
            self.wake.wait(0.5)
            self.wake.clear()
            if self.stopped.is_set():
                break
            self.tick()

    def tick(self) -> None:
        if not self.enabled or not self.ready or self._runtime_error or self._restart_preparing:
            return
        try:
            with self.runtime.transaction():
                # Unload may have occurred while this thread waited for the host lock.
                if not self.enabled or self.stopped.is_set() or self._restart_preparing:
                    return
                for sid in list(self.documents):
                    # Paused reviews may settle, but failed storage needs explicit recovery.
                    if self.documents[sid]["status"] == "blocked":
                        continue
                    try:
                        self._advance(sid)
                    except Exception as exc:
                        log.exception("Goal advance failed: %s", sid)
                        self._block(self.documents[sid], "Goal 推进失败：" + str(exc)[:500] + "。请检查记录后明确恢复。")
                        try:
                            self._save(copy.deepcopy(self.documents[sid]))
                        except Exception:
                            log.exception("Goal failure state could not be persisted")
        except Exception as exc:
            # The host lock/obligations store itself failed. Latch this fault instead
            # of killing the thread, retrying busy work, or silently reauthorizing it.
            self._runtime_error = "Goal 执行状态不可用：" + str(exc)[:500] + "。请修复后重新加载服务，再明确恢复。"
            for data in list(self.documents.values()):
                if data["status"] in ACTIVE:
                    data.update(status="blocked", pause_reason=self._runtime_error, _wait_started=None)
            log.exception("Goal runtime unavailable; automatic advance disabled")

    def _submit(self, data: dict, feedback: str = "", *, continuation: bool = False) -> None:
        if data["rounds"] >= data["max_rounds"] or data["stagnation"] >= 3:
            self._pause(data, "自动续轮预算已耗尽。")
            self._save(data)
            return
        pending = data.get("_pending_input")
        if not pending:
            index = data["rounds"] + (1 if continuation else 0)
            data["_input_seq"] = data.get("_input_seq", 0) + 1
            rid = f"goal:{data['_identity']}:{data['goal_version']}:input:{data['_input_seq']}"
            text = "继续执行用户明确启动的 Goal。\n目标：\n" + data["goal"] + "\n验收条件：\n" + (data["criteria"] or "按目标要求提供可核对的完成证据。")
            if feedback:
                text += "\n独立评审反馈（尚未通过）：\n" + feedback
            if data.get("_interruptions"):
                text += "\n先前执行中断或失败，不能视为成功；请核对并补充证据：\n" + json.dumps(data["_interruptions"], ensure_ascii=False, default=str)[:12000]
            text += "\n正常回复本轮结果；不要把 Todo 完成或自己的完成声明当作 Goal 验收通过。"
            pending = {"request_id": rid, "text": text, "round": index}
            data["_pending_input"] = pending
            self._save(data)
        origin = data["_roots"][-1] if data["_roots"] else ""
        started_at = time.time()
        result = self.runtime.submit_input(data["session_id"], pending["text"], pending["request_id"], origin_task_id=origin)
        tid = result["task_id"]
        if tid not in data["_roots"]:
            data["_roots"].append(tid)
        if (data.get("_current_execution") or {}).get("task_id") != tid:
            data["_current_execution"] = {"task_id": tid, "source_inbound_seq": result["inbound_seq"],
                                          "started_at": started_at}
        self.runtime.watch(data["_roots"])
        self.runtime.set_paused(data["_roots"], False)
        data.update(rounds=pending["round"], status="running", pause_reason="", _pending_input=None,
                    _candidate=None, _needs_input=False, _wait_started=time.time())
        self._save(data)

    def _advance(self, sid: str) -> None:
        data = copy.deepcopy(self.documents[sid])
        if data.get("_create_cleanup") is not None:
            return  # No scheduling across an unacknowledged evidence retirement.
        review = data.get("_review")
        if review:
            self._collect_review(data)
            data = copy.deepcopy(self.documents[sid])
        if data["status"] not in ACTIVE:
            return
        if data.get("_pending_input") or data.get("_needs_input") or not data["_roots"]:
            self._submit(data)
            return
        snap = self.runtime.snapshot(data["_roots"])
        if snap.get("unresolved"):
            self._pause(data, "相关执行失败、取消或结果丢失；请检查后决定如何处理。")
            self._save(data)
            return
        if snap.get("busy"):
            if data.get("_wait_started") is None:
                data["_wait_started"] = time.time()
                self._save(data)
            elif time.time() - data["_wait_started"] >= WAIT_TIMEOUT:
                self._pause(data, "等待相关执行或结果处理超过 30 分钟；后台工作未取消。")
                self._save(data)
            elif data["status"] != "waiting":
                data["status"] = "waiting"
                self._save(data)
            return
        candidate = data.get("_candidate")
        if not candidate or candidate.get("goal_version") != data["goal_version"]:
            return
        if data.get("_review"):
            return
        for prior in reversed(data["reviews"]):
            if (not prior.get("applied") and prior.get("goal_version") == data["goal_version"]
                    and prior.get("evidence_version") == snap["version"] and prior.get("verdict")
                    and prior.get("candidate_id") == (candidate.get("candidate_id") or candidate.get("entry_task_id"))):
                self._apply_verdict(data, prior, snap)
                return
        retry_key = f"{data['goal_version']}:{snap['version']}"
        attempts = data.setdefault("_review_attempts", {}).get(retry_key, 0)
        if attempts >= 2:
            self._pause(data, "该证据版本的评审技术重试已耗尽；需要补充证据或修改目标。")
            self._save(data)
            return
        self._start_review(data, snap, attempt=attempts)

    def _start_review(self, data: dict, snap: dict, attempt: int = 0) -> None:
        candidate = data["_candidate"]
        from engine.node import load_node
        node = load_node(self.root, REVIEW_NODE)
        try:
            instruction, input_info = build_input(data, snap, node, runtime_cfg=load_runtime_config(self.root))
        except ReviewInputError as exc:
            # Missing or oversized transport is not a verdict on the work. Keep the
            # candidate for an explicit retry after capacity/records are repaired.
            self._pause(data, str(exc))
            self._save(data)
            return
        model = candidate.get("model") or (candidate.get("result") or {}).get("_execution_model") or {}
        overrides = {"provider": data["reviewer_provider"] or getattr(node, "provider", "") or model.get("provider", ""),
                     "model": data["reviewer_model"] or getattr(node, "model", "") or model.get("model", ""), "max_steps": 1}
        rid = f"goal:{data['_identity']}:{data['goal_version']}:review:{data['rounds']}:{snap['version']}:{attempt}"
        review = {"request_id": rid, "task_id": "", "session_id": "", "goal_version": data["goal_version"],
                  "evidence_version": snap["version"], "attempt": attempt, "started": time.time(),
                  "candidate_id": candidate.get("candidate_id") or candidate.get("entry_task_id"),
                  "evidence_missing": False, "evidence_truncated": False,
                  "input_schema_version": 2, **input_info,
                  "evidence_task_ids": sorted({input_info["latest_main_task_id"],
                                              *[row["task_id"] for row in data["_review_outputs"]]})}
        data.setdefault("_review_attempts", {})[f"{data['goal_version']}:{snap['version']}"] = attempt + 1
        data.update(status="reviewing", _review=review, _wait_started=None)
        self._save(data)
        task = self.runtime.create_system_task(data["session_id"], REVIEW_NODE, instruction, rid, node_overrides=overrides)
        review.update(task_id=task["task_id"], session_id=task["session_id"])
        data.setdefault("_system_tasks", []).append(task["task_id"])
        # Keep review status durable without including it in the main evidence graph.
        self.runtime.watch([task["task_id"]])
        self._save(data)

    def _collect_review(self, data: dict) -> None:
        review = data["_review"]
        tid = review.get("task_id")
        if not tid:
            self._review_error(data, "评审任务未确认创建。")
            return
        snap = self.runtime.snapshot([tid])
        tasks = snap.get("tasks", [])
        task = tasks.get(tid) if isinstance(tasks, dict) else next((t for t in tasks if t.get("task_id") == tid), None)
        status = str((task or {}).get("status") or "missing")
        if status in {"pending", "running", "suspended"}:
            if time.time() - review["started"] >= REVIEW_TIMEOUT:
                self.runtime.cancel_system_task(tid)
                self._review_error(data, "评审超过 180 秒。")
            return
        if status != "completed":
            self._review_error(data, "评审任务失败、取消或丢失。")
            return
        result = task.get("result") or {}
        content = result.get("result") if isinstance(result.get("result"), dict) else result
        try:
            verdict = parse_verdict(content.get("text", ""))
            if review["evidence_missing"] and verdict["passed"]:
                verdict.update(passed=False, feedback="缺少可核对的执行证据，不能确认通过。", missing_evidence=["执行证据"])
        except ValueError as exc:
            self._review_error(data, str(exc))
            return
        record = {**review, "verdict": verdict, "error": "", "applied": False, "finished_at": _now()}
        data["reviews"].append(record)
        data["reviews"] = data["reviews"][-MAX_REVIEWS:]
        data["_review"] = None
        self._save(data)
        if data["status"] in ACTIVE:
            snap = self.runtime.snapshot(data["_roots"])
            self._apply_verdict(data, record, snap)

    def _review_error(self, data: dict, reason: str) -> None:
        review = data["_review"]
        data["reviews"].append({**review, "verdict": None, "error": reason, "applied": False, "finished_at": _now()})
        data["reviews"] = data["reviews"][-MAX_REVIEWS:]
        data["_review"] = None
        if data["status"] not in ACTIVE or review["goal_version"] != data["goal_version"]:
            # A superseded review is still recorded, but cannot pause its replacement.
            self._save(data)
            return
        snap = self.runtime.snapshot(data["_roots"])
        candidate = data.get("_candidate") or {}
        if (review["attempt"] < 1 and review["goal_version"] == data["goal_version"]
                and not data.get("_needs_input")
                and review.get("candidate_id") == (candidate.get("candidate_id") or candidate.get("entry_task_id"))
                and review["evidence_version"] == snap["version"] and not snap.get("busy") and not snap.get("unresolved")):
            self._start_review(data, snap, attempt=review["attempt"] + 1)
        else:
            self._pause(data, reason + " 自动重试已停止，请检查后恢复。")
            self._save(data)

    def _apply_verdict(self, data: dict, record: dict, snap: dict) -> None:
        candidate = data.get("_candidate") or {}
        if (data["status"] not in ACTIVE or record.get("applied") or data.get("_needs_input")
                or record.get("candidate_id") != (candidate.get("candidate_id") or candidate.get("entry_task_id"))
                or record["goal_version"] != data["goal_version"] or record["evidence_version"] != snap["version"]
                or snap.get("busy") or snap.get("unresolved")):
            return
        # Host transaction remains held from snapshot through the next input creation.
        record["applied"] = True
        record["applied_at"] = _now()
        verdict = record["verdict"]
        if verdict["passed"]:
            data.update(status="completed", pause_reason="", _wait_started=None)
            self.runtime.set_paused(data["_roots"], True)
            self._save(data)
            return
        data["stagnation"] = 0 if verdict["progress"] else data["stagnation"] + 1
        if verdict["needs_user"]:
            self._pause(data, "需要用户决定：" + verdict["feedback"])
        elif data["stagnation"] >= 3:
            self._pause(data, "连续三次评审没有实质进展。")
        elif data["rounds"] >= data["max_rounds"]:
            self._pause(data, "自动续轮预算已耗尽。")
        if data["status"] in ACTIVE:
            feedback = verdict["feedback"]
            if verdict["missing_evidence"]:
                feedback += "\n缺失证据：\n" + "\n".join(verdict["missing_evidence"])
            # _submit durably saves the applied verdict and its pending input
            # together, before invoking Core. No applied-without-feedback gap.
            self._submit(data, feedback, continuation=True)
        else:
            self._save(data)

    def close(self) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.wake.set()
        try:
            with self.runtime.transaction():
                self.enabled = False
                for source in list(self.documents.values()):
                    try:
                        self.runtime.set_paused(source["_roots"], True)
                        if source["status"] in ACTIVE:
                            data = copy.deepcopy(source)
                            self._pause(data, "Goal 插件已卸载，等待明确恢复。")
                            self._save(data)
                    except Exception:
                        log.exception("Could not persist Goal pause on unload")
        finally:
            self.enabled = False
            self.unsubscribe()
        # Do not join while the host may hold its outer transaction lock.


def summary(data: dict) -> str:
    if data.get("is_child"):
        return "当前为子会话，仅拥有自己的 Todo，不拥有父会话 Goal。"
    if not data.get("goal"):
        return "当前会话尚未设置 Goal。可通过前端 Goal 输入或 Goal API 提交目标并开始执行。"
    text = f"当前会话 Goal：{data['goal']}\n验收条件：{data['criteria'] or '按目标提供完成证据'}\n状态：{data['status']}；自动续轮 {data['rounds']}/{data['max_rounds']}；连续无进展 {data['stagnation']}/3。"
    if data.get("pause_reason"):
        text += "\n说明：" + data["pause_reason"]
    if data["status"] not in ACTIVE:
        text += "\n此记录不授权继续执行；暂停后必须由用户明确恢复。"
    text += "\nTodo 是工作记录，不代表 Goal 通过验收；本轮回复正常展示，最终验收由独立评审确认。"
    return text


def _service(request: Request) -> GoalService:
    runtime = getattr(request.app.state.state, "execution_runtime", None)
    service = _SERVICES.get(id(runtime))
    if service is None or not service.enabled:
        raise HTTPException(503, "Goal 服务未启用。")
    return service


def add_routes(router: APIRouter) -> None:
    @router.get("/goal")
    def read_goal(request: Request, session_id: str):
        try:
            # Echo the requested view separately from the canonical Goal owner (entry branches).
            return {**_service(request).read(session_id), "requested_session_id": session_id}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.post("/goal")
    async def update_goal(request: Request, session_id: str):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 64 * 1024:
                raise HTTPException(413, "Goal 请求过长。")
        try:
            patch = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(422, "无效 JSON。") from None
        try:
            return await asyncio.to_thread(_service(request).operate, session_id, patch)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("/goal/task/{task_id}")
    def task_goal(request: Request, task_id: str, worker_id: str, generation: int, execution_id: str):
        service = _service(request)
        with service.runtime.transaction():
            try:
                info = service.runtime.task_info(task_id, worker_id, generation, execution_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if info.get("is_system_task") or info.get("is_child"):
                return _public(_empty(info["session_id"], child=True))
            return service.read(info["session_id"])


async def refresh_goal_context(ctx: Any) -> HookResult | None:
    before = len(ctx.messages)
    ctx.messages[:] = [m for m in ctx.messages if not (m.get("_ide_goal") or (m.get("_meta") or {}).get("_ide_goal"))]
    rctx = ctx.rctx
    if (rctx is None or not getattr(rctx, "task_id", "") or not getattr(rctx, "execution_id", "")
            or getattr(rctx, "is_system_task", False)):
        return HookResult(modified=True) if len(ctx.messages) != before else None
    import httpx
    from urllib.parse import quote
    try:
        response = await rctx.http.get(
            f"{rctx.supervisor_url}/v1/plugins/ide/goal/task/{quote(rctx.task_id, safe='')}",
            params={"worker_id": rctx.worker_id, "generation": rctx.session_generation,
                    "execution_id": getattr(rctx, "execution_id", "")}, timeout=5.0)
        if response.status_code in (404, 503):
            return HookResult(modified=True) if len(ctx.messages) != before else None
        response.raise_for_status()
        data = response.json()
        if not data.get("goal") or data.get("is_child"):
            return HookResult(modified=True) if len(ctx.messages) != before else None
        text = summary(data)
    except (ValueError, httpx.HTTPError):
        # Unknown/unavailable Goal state is not an instruction to ordinary tasks.
        return HookResult(modified=True) if len(ctx.messages) != before else None
    ctx.messages.append({"role": "user", "content": text, "_dynamic": True, "_ide_goal": True,
                         "message_type": "dynamic_context", "_meta": {"_dynamic": True, "_ide_goal": True, "message_type": "dynamic_context"}})
    return HookResult(modified=True)


def register_engine(ctx: Any) -> None:
    register_node(ctx)
    ctx.hooks.register("before_llm_call", refresh_goal_context, priority=41)
    ctx.hooks.register("before_llm_call", review_cache_blocks, priority=42)


def register_node(ctx: Any) -> None:
    nodes = ctx.contributions.get("nodes")
    if nodes is not None:
        import yaml
        path = Path(__file__).parent / "nodes" / "ide.goal_reviewer.yaml"
        nodes.register(yaml.safe_load(path.read_text(encoding="utf-8")), source_path=str(path))


def register_supervisor(ctx: Any) -> None:
    register_node(ctx)
    runtime = ctx.contributions.get("execution")
    if runtime is None:
        return  # Existing hosts without the general execution contract retain Todo only.
    service = GoalService(runtime, runtime.workspace_root)
    _SERVICES[id(runtime)] = service

    def dispose() -> None:
        service.close()
        _SERVICES.pop(id(runtime), None)

    ctx.hooks.add_plugin_disposer("ide", dispose, key=("ide_goal", id(runtime)))
