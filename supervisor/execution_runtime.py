"""Execution facts and restricted plugin operations; no feature-specific policy.

The supervisor lock is the transaction boundary. Subscribers only maintain short
local state; model and filesystem work must not be performed by subscribers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .types import TaskKind, TaskStatus

log = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "lost"}
EVIDENCE_LIMIT = 256_000
OWNER_EVIDENCE_LIMIT = 2_000_000
TOTAL_EVIDENCE_LIMIT = 32_000_000


class ExecutionRuntime:
    """Process-local capability object mounted as contributions.execution.

    Public methods acquire the reentrant supervisor lock. snapshot.version is a
    content digest of the selected execution graph, not a global event counter.
    watch() enables bounded evidence capture; forget() removes only those watched
    roots and their unshared evidence. Pausing never cancels an existing task.
    """

    def __init__(self, state: Any):
        self.state = state
        self.workspace_root = state.workspace_root
        self.boot_id = state.eventlog.run_id
        self.ready = False
        self.recovery_info: dict[str, Any] = {}
        self.planned_restart: dict[str, Any] = {}
        self._listeners: list[Callable] = []
        self._handlers: list[tuple[Callable, Callable | None]] = []
        self._tasks: dict[str, dict] = {}
        self._receipts: dict[str, dict] = {}
        self._requests: dict[str, dict] = {}
        self._watched: set[str] = set()
        self._paused: set[str] = set()
        self._closed: set[str] = set()
        self._evidence: dict[str, list] = {}
        self._truncated: set[str] = set()
        self._outputs: dict[str, dict] = {}
        self._output_sequence = 0
        self._output_truncated: set[str] = set()
        self._pending_candidates: list[dict] = []
        self._path = state.workspace_root / "data" / "executions" / "runtime.json"
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._tasks = data.get("tasks", {})
                self._receipts = data.get("receipts", {})
                self._requests = data.get("requests", {})
                for request in self._requests.values():
                    if request.get("phase") != "done":
                        request["phase"] = "failed_closed"  # No cross-file commit proof after restart.
                self._watched = set(data.get("watched", []))
                self._closed = set(data.get("closed", []))
                self._paused = set(self._watched)  # Boot cannot grant execution permission.
                self._evidence = data.get("evidence", {})
                self._truncated = set(data.get("truncated", []))
                self._outputs = data.get("outputs", {})
                self._output_sequence = int(data.get("output_sequence", 0))
                self._output_truncated = set(data.get("output_truncated_task_ids", []))
                if "outputs" not in data:
                    # Legacy state cannot prove which replies were omitted; do not
                    # infer an empty, complete output history from missing storage.
                    self._output_truncated.update(tid for tid in self._related(self._watched)
                                                  if not self._tasks.get(tid, {}).get("is_system_task"))
                state._async_tools.update(data.get("work", {}))
                for row in self._tasks.values():
                    if row.get("status") not in TERMINAL:
                        row["status"] = "lost"
                receipt_work = {r.get("work_id") for r in self._receipts.values()}
                for row in state._async_tools.values():
                    if row.get("status") == "running" or (row.get("status") == "done" and row.get("async_id") not in receipt_work):
                        row.update(status="lost", error="supervisor restarted before delivery")
                for row in self._receipts.values():
                    if row.get("status") != "processed":
                        row.update(status="lost", error="supervisor restarted before receipt settled")
            except (ValueError, OSError, TypeError):
                # A broken obligations file must not be interpreted as no work.
                log.exception("Execution state unreadable; automatic operations unavailable")
                self._load_error = True

    @contextmanager
    def transaction(self):
        with self.state._lock:
            if getattr(self, "_load_error", False):
                raise ValueError("execution state is unavailable")
            yield self

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state.workspace_root / "data" / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        selected = self._related(self._watched)
        # Keep all unsettled work, but bound completed unrelated bookkeeping.
        removable = [did for did, row in self._receipts.items() if row.get("status") == "processed" and row.get("origin_task_id") not in selected]
        for did in removable[:-200]:
            self._receipts.pop(did, None)
        for key in list(self._requests)[:-1000]:
            row = self._requests[key]
            if row.get("phase") == "done" and row.get("result", {}).get("task_id") not in selected:
                self._requests.pop(key, None)
        for tid in list(self._tasks):
            if tid not in selected and tid not in self._closed and tid not in self.state.tasks and self._tasks[tid]["status"] in TERMINAL:
                self._tasks.pop(tid, None)
        data = {"tasks": {k: v for k, v in self._tasks.items() if k in selected or (k in self._closed and v.get("is_system_task"))},
                "work": self.state._async_tools, "receipts": self._receipts,
                "requests": self._requests, "watched": sorted(self._watched), "closed": sorted(self._closed),
                "evidence": self._evidence, "truncated": sorted(self._truncated),
                "outputs": self._outputs, "output_sequence": self._output_sequence,
                "output_truncated_task_ids": sorted(self._output_truncated)}
        fd, path = tempfile.mkstemp(dir=temp, prefix="execution-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(path).unlink(missing_ok=True)

    def subscribe(self, callback):
        self._listeners.append(callback)
        def dispose():
            if callback in self._listeners:
                self._listeners.remove(callback)
        return dispose

    def register_input_handler(self, callback, *, matches=None):
        registration = (callback, matches)
        self._handlers.append(registration)
        def dispose():
            if registration in self._handlers:
                self._handlers.remove(registration)
        return dispose

    def notify(self, kind: str, session_id: str = "", **payload):
        event = {"kind": kind, "session_id": session_id, **copy.deepcopy(payload)}
        for callback in tuple(self._listeners):
            try:
                callback(event)
            except Exception:
                log.exception("Execution subscriber failed (%s)", kind)

    def mark_ready(self):
        # Startup reconciliation may delete registry entries; deletion here only touches
        # dedicated system history, never notifies session/Goal lifecycle recursively.
        for tid, row in self._tasks.items():
            if tid in self._closed and row.get("is_system_task"):
                self._clear_closed_history(row)
        self.ready = True
        self.notify("ready", restart=self.recovery_info, **self.recovery_info)

    def emit_plugin_event(self, event_type: str, payload: dict):
        sid = self.session_info(payload.get("session_id", ""))["session_id"]
        self.state.eventlog.append(session_id=sid, component="supervisor", type_=event_type,
                                   payload={**payload, "session_id": sid})

    def session_info(self, sid: str) -> dict:
        with self.transaction():
            seen = set()
            registry = self.state._session_store._registry
            while sid not in seen:
                seen.add(sid)
                row = registry.get(sid)
                if not row or row.get("reset"):
                    raise ValueError("session not found")
                if row.get("node_id") == "__entry_branch__":
                    sid = row.get("parent_session_id", "")
                    continue
                info = self.state.sessions.get(sid)
                if info is None:
                    raise ValueError("session not found")
                return {"session_id": sid, "is_child": bool(row.get("is_child") or row.get("parent_session_id")),
                        "generation": self.state._current_session_generation_locked(sid) or 1,
                        "workspace": copy.deepcopy(info.workspace or {}),
                        "node_id": info.entry_node_id, "boot_id": self.boot_id}
            raise ValueError("invalid session ancestry")

    def task_info(self, task_id: str, worker_id: str, generation: int, execution_id: str = "") -> dict:
        with self.transaction():
            task = self.state.tasks.get(task_id)
            if (not task or task.worker_id != worker_id or task.session_generation != generation
                    or task.status != TaskStatus.running or task.cancel_requested
                    or (task.execution_id and task.execution_id != execution_id)):
                raise ValueError("task is not active on this execution")
            info = self.session_info(task.input.get("child_session_id") or task.session_id)
            return {**info, "task_id": task_id, "is_system_task": bool(task.input.get("_system_task")),
                    "node_id": task.node_id, "execution_id": task.execution_id}

    def _related(self, roots) -> set[str]:
        selected = set(roots)
        changed = True
        while changed:
            changed = False
            for tid, row in self._tasks.items():
                if tid not in selected and not row.get("is_system_task") and selected.intersection(row.get("parents", [])):
                    selected.add(tid)
                    changed = True
        return selected

    def watch(self, root_task_ids):
        roots = [root_task_ids] if isinstance(root_task_ids, str) else root_task_ids
        with self.transaction():
            self._watched.update(roots)
            self._save()

    def unwatch(self, root_task_ids, *, delete_evidence=False):
        roots = [root_task_ids] if isinstance(root_task_ids, str) else root_task_ids
        if delete_evidence:
            return self.forget(roots)
        with self.transaction():
            self._watched.difference_update(roots)
            self._save()

    def forget(self, root_task_ids):
        roots = [root_task_ids] if isinstance(root_task_ids, str) else root_task_ids
        with self.transaction():
            old = self._related(roots)
            self._watched.difference_update(roots)
            self._paused.difference_update(roots)
            self._closed.update(old)
            removable = old - self._related(self._watched)
            for tid in removable:
                task = self.state.tasks.get(tid)
                row = self._tasks.get(tid, {})
                if row.get("is_system_task") and row.get("owner_session_id"):
                    # Keep only enough identity to delete late history on terminal/boot.
                    row["result"] = {}
                    self._clear_closed_history(row)
                self._evidence.pop(tid, None)
                if not row.get("is_system_task") and (task is None or task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}):
                    self._tasks.pop(tid, None)
            for did, receipt in list(self._receipts.items()):
                if receipt.get("origin_task_id") in removable:
                    if receipt.get("status") not in {"processed", "lost"}:
                        receipt.update(status="lost", error="execution owner removed")
                    receipt.pop("text", None)
                    receipt.pop("attachments", None)
            for key, request in list(self._requests.items()):
                if request.get("result", {}).get("task_id") in removable:
                    self._requests.pop(key, None)
            self._outputs = {key: row for key, row in self._outputs.items() if row["task_id"] not in removable}
            for tid in removable:
                self._truncated.discard(tid)
                self._output_truncated.discard(tid)
            self._save()

    def cleanup_closed_system(self, task):
        if task.task_id in self._closed and task.input.get("_system_task"):
            self._clear_closed_history({"is_system_task": True,
                "owner_session_id": task.input.get("_execution_owner_session_id"),
                "runtime_session_id": task.input.get("child_session_id") or task.session_id})

    def _clear_closed_history(self, row):
        if row.get("is_system_task") and row.get("owner_session_id") and row.get("runtime_session_id") != row["owner_session_id"]:
            self.state._delete_conversation_session_locked(row["runtime_session_id"])

    def task_changed(self, task, *, notify=True):
        inp = task.input
        origin = inp.get("_dispatch_origin") or {}
        parents = [task.caller_task_id, origin.get("origin_task_id"),
                   inp.get("_dispatch_owner_root_task_id"), inp.get("_execution_origin_task_id"),
                   task.output_chain_entry_task_id]
        row = {"task_id": task.task_id, "session_id": inp.get("parent_session_id") or task.session_id,
               "runtime_session_id": inp.get("child_session_id") or task.session_id,
               "owner_session_id": inp.get("_execution_owner_session_id", ""),
               "node_id": task.node_id, "status": str(getattr(task.status, "value", task.status)),
               "execution_id": task.execution_id, "routing_pending": task.completion_state == "routing", "parents": sorted(set(p for p in parents if p and p != task.task_id)),
               "is_system_task": bool(inp.get("_system_task")), "result": copy.deepcopy(task.result)}
        row["result"].pop("_runtime_state", None)
        row["result"].pop("dispatch_input", None)
        if len(json.dumps(row["result"], ensure_ascii=False)) > 64_000:
            row["result"] = {"action": row["result"].get("action"), "error": "result exceeds snapshot limit", "truncated": True}
        for key in ("identity_unverified", "candidate_digest"):
            if key in self._tasks.get(task.task_id, {}):
                row[key] = self._tasks[task.task_id][key]
        self._tasks[task.task_id] = row
        if task.task_id in self._closed and row.get("is_system_task"):
            row["result"] = {}
            if row["status"] in TERMINAL:
                self._clear_closed_history(row)
        watched = self._related(self._watched)
        if len(self._tasks) > len(watched) + 200:
            for tid in list(self._tasks):
                if tid not in watched and tid not in self._closed and tid not in self.state.tasks and self._tasks[tid]["status"] in TERMINAL:
                    self._tasks.pop(tid, None)
        if notify:
            if task.task_id in watched:
                try:
                    self._save()
                except Exception:
                    self._load_error = True
                    log.exception("Execution facts unavailable; normal task routing continues")
                    return
            self.notify("task", inp.get("_execution_owner_session_id") or row["session_id"], task=row)

    def _evidence_owner(self, task_id):
        row = self._tasks.get(task_id, {})
        for root in sorted(self._watched):
            if task_id in self._related([root]):
                source = self._tasks.get(root, {})
                return source.get("owner_session_id") or source.get("session_id") or root
        return row.get("owner_session_id") or row.get("session_id") or task_id

    def _record_output(self, task, identity, kind, text, attachments=None):
        """Keep immutable reply/finish facts independently of raw trace budgets."""
        if (task.task_id not in self._related(self._watched) or task.input.get("_system_task")
                or task.task_id in self._related(self._closed) or task.cancel_requested):
            return
        row = {"id": identity, "task_id": task.task_id, "node_id": task.node_id or task.tool_name or "",
               "kind": kind, "text": text, "attachments": copy.deepcopy(attachments or [])}
        prior = self._outputs.get(identity)
        if prior is not None:
            if {key: value for key, value in prior.items() if key != "sequence"} == row:
                return
            self._output_truncated.add(task.task_id)  # Identity conflict is not a revised prefix.
        else:
            # ponytail: separate 2 MB/owner and 32 MB total output budget; an
            # oversized output is explicitly unavailable, never silently sliced.
            owner = self._evidence_owner(task.task_id)
            sizes = [(r["task_id"], len(json.dumps(r, ensure_ascii=False).encode())) for r in self._outputs.values()]
            size = len(json.dumps({**row, "sequence": self._output_sequence + 1}, ensure_ascii=False).encode())
            owner_size = sum(n for tid, n in sizes if self._evidence_owner(tid) == owner)
            if size + owner_size > OWNER_EVIDENCE_LIMIT or size + sum(n for _, n in sizes) > TOTAL_EVIDENCE_LIMIT:
                self._output_truncated.add(task.task_id)
            else:
                self._output_sequence += 1
                self._outputs[identity] = {**row, "sequence": self._output_sequence}
        try:
            self._save()
        except Exception:
            self._load_error = True
            raise

    def record_reply(self, event):
        """Record accepted reply events, not guesses extracted from tool arguments."""
        if event.get("type") not in {"intermediate_reply", "assistant_text"}:
            return
        with self.transaction():
            payload = event.get("payload") or {}
            task_id = str(payload.get("task_id") or "")
            task = self.state.tasks.get(task_id)
            row = self._tasks.get(task_id, {})
            if (task_id not in self._related(self._watched) or row.get("is_system_task")
                    or task_id in self._related(self._closed)):
                return
            target_session = str(event.get("session_id") or "")
            if task is not None:
                # Completed entry branches may already be removed from the active
                # registry. The task's durable route still identifies its owner.
                route = self.state._route_session_id_for_task_locked(task)
                if not target_session or route != self.state._route_session_id_for_session_locked(target_session):
                    return
            execution = str(payload.get("execution_id") or "")
            # Never attribute an unidentified/old execution's late event to the
            # current execution. Terminal events from that exact execution may
            # still append: snapshot.version invalidates an in-flight review.
            if (task is None or not execution or execution != task.execution_id
                    or task.cancel_requested or task.status not in {
                        TaskStatus.running, TaskStatus.completed, TaskStatus.failed, TaskStatus.suspended}):
                self._output_truncated.add(task_id)
                try:
                    self._save()
                except Exception:
                    self._load_error = True
                    raise
                return
            invocation = str(payload.get("llm_request_id") or payload.get("tool_call_id") or event.get("event_id") or "")
            if not invocation:
                raise ValueError("reply event identity is required")
            text = payload.get("text", "")
            attachments = payload.get("attachments", [])
            if not isinstance(text, str) or not isinstance(attachments, list):
                raise ValueError("invalid reply payload")
            identity = f"reply:{task.task_id}:{execution}:{event['type']}:{invocation}"
            self._record_output(task, identity, "reply", text, attachments)

    def capture_evidence(self, task):
        if task.task_id not in self._related(self._watched):
            return
        action = task.result or {}
        result = action.get("result") or {}
        if action.get("action") == "finish" and isinstance(result, dict):
            self._record_output(task, f"finish:{task.task_id}:{task.execution_id}", "finish",
                                str(result.get("text") or ""), result.get("attachments"))
        from engine.conversation_store import ConversationStore, is_dynamic_context
        sid = task.input.get("child_session_id") or task.session_id
        evidence = []
        # Artifact files are deleted after capture. A resumed task must retain the
        # complete copies already saved, even if its next capture sees no file.
        retained = {row["artifact"]: copy.deepcopy(row) for row in self._evidence.get(task.task_id, [])
                    if row.get("artifact") and not row.get("truncated")}
        artifacts = {}
        if action.get("checkpoint_error") or (action.get("_runtime_state") or {}).get("checkpoint_error"):
            self._truncated.add(task.task_id)
            self._output_truncated.add(task.task_id)
        # ponytail: 2 MB per owning session, 32 MB total. At the global ceiling
        # report truncation; paging/archival is intentionally not a new storage layer.
        owner = self._evidence_owner(task.task_id)
        sizes = {tid: len(json.dumps(rows, ensure_ascii=False).encode()) for tid, rows in self._evidence.items() if tid != task.task_id}
        owner_bytes = sum(size for tid, size in sizes.items() if self._evidence_owner(tid) == owner)
        remaining = min(EVIDENCE_LIMIT, max(0, OWNER_EVIDENCE_LIMIT - owner_bytes),
                        max(0, TOTAL_EVIDENCE_LIMIT - sum(sizes.values()))) - 2
        if remaining <= 0:
            self._truncated.add(task.task_id)
        try:
            # Reserve existing complete artifacts before new messages/results.
            # If a changed budget cannot retain one, explicitly mark the gap.
            for name, row in sorted(retained.items()):
                size = len(json.dumps(row, ensure_ascii=False).encode()) + 2
                if size > remaining:
                    self._truncated.add(task.task_id)
                    continue
                artifacts[name] = row
                remaining -= size
            # A compact/resume may remove earlier messages from ConversationStore.
            # Keep their captured originals and append only new message identities.
            messages = [copy.deepcopy(row) for row in self._evidence.get(task.task_id, []) if not row.get("artifact")]
            known = {row["id"]: row for row in messages if row.get("id")}
            stored = ConversationStore(self.state.workspace_root / "data" / "conversations").load(sid)
            available_ids = set(known) | {message.id for message in stored}
            checkpoint = action.get("_runtime_state") or {}
            if any(checkpoint.get(key) and checkpoint[key] not in available_ids
                   for key in ("first_shadow_message_id", "last_shadow_message_id")):
                self._truncated.add(task.task_id)
            for message in stored:
                if message.source_task_id != task.task_id or is_dynamic_context(message):
                    continue
                row = message.to_dict()
                if row.get("id") in known:
                    prior = known[row["id"]]
                    if any(prior.get(key) != row.get(key) for key in ("role", "content", "tool_calls", "tool_call_id")):
                        self._truncated.add(task.task_id)
                    continue
                messages.append(row)
                if row.get("id"):
                    known[row["id"]] = row
            for row in messages:
                raw = json.dumps(row, ensure_ascii=False).encode()
                if len(raw) + 2 > remaining:
                    self._truncated.add(task.task_id)
                    break
                evidence.append(row)
                remaining -= len(raw) + 2
            root = self.state.workspace_root / "data" / "artifacts" / "tool_results" / task.task_id
            if root.is_dir():
                for path in sorted(root.glob("*.txt")):
                    if path.name in artifacts:
                        # Names identify immutable calls. Reuse only the same
                        # content; a conflicting reused name is not a replacement.
                        previous = artifacts[path.name]["content"]
                        with path.open(encoding="utf-8", errors="replace") as f:
                            if f.read(len(previous) + 1) != previous:
                                self._truncated.add(task.task_id)
                        continue
                    if remaining <= 0:
                        self._truncated.add(task.task_id)
                        break
                    with path.open(encoding="utf-8", errors="replace") as f:
                        text = f.read(remaining + 1)
                    row = {"source_task_id": task.task_id, "artifact": path.name, "content": text, "truncated": False}
                    size = len(json.dumps(row, ensure_ascii=False).encode()) + 2
                    if size > remaining:
                        # Escaping can cost six bytes per character. Keep a conservative prefix.
                        overhead = len(json.dumps({**row, "content": ""}, ensure_ascii=False).encode()) + 2
                        row.update(content=text[:max(0, remaining - overhead) // 6], truncated=True)
                        self._truncated.add(task.task_id)
                        size = len(json.dumps(row, ensure_ascii=False).encode()) + 2
                    if size <= remaining:
                        artifacts[path.name] = row
                        remaining -= size
            self._evidence[task.task_id] = evidence + [row for _, row in sorted(artifacts.items())]
        except Exception:
            self._truncated.add(task.task_id)
            log.exception("Execution evidence capture failed")
        try:
            self._save()
        except Exception:
            self._load_error = True
            raise

    def candidate(self, entry, result, producer=None):
        if entry.input.get("_system_task") or entry.caller_task_id or entry.input.get("_dispatch_origin") or entry.input.get("_async_dispatch"):
            return
        producer = producer or entry
        sid = self.state._route_session_id_for_task_locked(entry)
        candidate = {"candidate_id": f"{entry.task_id}:{entry.execution_id}:{producer.task_id}",
                     "entry_task_id": entry.task_id, "producer_task_id": producer.task_id,
                     "task_id": entry.task_id, "session_id": sid, "node_id": entry.node_id,
                     "execution_id": entry.execution_id, "result": copy.deepcopy(result),
                     "model": copy.deepcopy((producer.result or {}).get("_execution_model", {}))}
        self._pending_candidates.append(candidate)

    def routed(self, task):
        task.completion_state = "routed"
        self.task_changed(task, notify=False)
        candidates, self._pending_candidates = self._pending_candidates, []
        completed_ids = {task.task_id} if task.status == TaskStatus.completed and (task.result or {}).get("action") == "finish" else set()
        for candidate in candidates:
            entry = self.state.tasks.get(candidate["entry_task_id"])
            if entry is not None:
                entry.completion_state = "routed"
                self.task_changed(entry, notify=False)
                self._tasks[entry.task_id]["candidate_digest"] = hashlib.sha256(
                    json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if entry.status == TaskStatus.completed:
                    completed_ids.add(entry.task_id)
        for receipt in self._receipts.values():
            if receipt.get("carrier_task_id") in completed_ids and receipt.get("status") == "admitted":
                receipt["status"] = "processed"
                work = self.state._async_tools.get(receipt.get("work_id"))
                if work and work.get("cancel_reason") == "service_restart":
                    work["restart_result_consumed"] = True
        relevant = task.task_id in self._related(self._watched)
        try:
            if relevant:
                self._save()
        except Exception:
            self._load_error = True
            log.exception("Execution facts could not be committed; automatic operations disabled")
            return
        self.notify("task", task.input.get("_execution_owner_session_id") or self._tasks[task.task_id]["session_id"], task=self._tasks[task.task_id])
        for candidate in candidates:
            self.notify("candidate", candidate["session_id"], candidate=candidate)

    def snapshot(self, root_task_ids):
        with self.transaction():
            roots = [root_task_ids] if isinstance(root_task_ids, str) else root_task_ids
            selected = self._related(roots)
            tasks = [copy.deepcopy(row) for tid, row in sorted(self._tasks.items()) if tid in selected]
            owners = {r.get("owner_session_id") or r.get("session_id") for r in tasks}
            work = [copy.deepcopy(row) for _, row in sorted(self.state._async_tools.items())
                    if row.get("task_id") in selected or (not row.get("task_id") and row.get("session_id") in owners)]
            receipts = [copy.deepcopy(row) for _, row in sorted(self._receipts.items()) if row.get("origin_task_id") in selected]
            unresolved = [row for row in tasks + work + receipts if row.get("status") in {"failed", "cancelled", "lost", "unknown"}
                          and not (row.get("cancel_reason") == "service_restart" and row.get("restart_result_consumed"))]
            unresolved += [{**row, "status": "unknown", "error": "execution identity unverified"}
                           for row in tasks if row.get("identity_unverified")]
            unresolved += [{**row, "status": "unknown", "error": "legacy async identity missing"}
                           for row in work if not row.get("execution_id") or not row.get("async_id") or not row.get("task_id")]
            unresolved += [{**row, "status": "unknown", "error": "result identity unverified"}
                           for row in receipts if row.get("metadata", {}).get("identity_unverified")]
            missing = sorted(set(roots) - self._tasks.keys())
            unresolved += [{"task_id": tid, "status": "missing"} for tid in missing]
            busy = any(row["status"] not in TERMINAL or row.get("routing_pending") for row in tasks)
            busy = busy or any(row.get("status") == "running" for row in work)
            busy = busy or any(row.get("status") not in {"processed", "lost"} for row in receipts)
            # A computed tool result without its receipt is not delivered.
            receipt_work = {row.get("work_id") for row in receipts}
            busy = busy or any(row.get("async_id") not in receipt_work and row.get("status") != "lost" for row in work)
            evidence = [copy.deepcopy(row) for tid in sorted(selected) for row in self._evidence.get(tid, [])]
            outputs = [copy.deepcopy(row) for row in self._outputs.values() if row["task_id"] in selected]
            outputs.sort(key=lambda row: row["sequence"])
            output_truncated = sorted(selected.intersection(self._output_truncated))
            # Heartbeats are excluded; evidence, candidate results and uncertainty are not.
            facts = {"tasks": [{k: r.get(k) for k in ("task_id", "execution_id", "status", "parents", "routing_pending", "identity_unverified", "candidate_digest", "result")} for r in tasks],
                     "work": [{k: r.get(k) for k in ("async_id", "task_id", "status", "execution_id")} for r in work],
                     "receipts": [{k: r.get(k) for k in ("delivery_id", "status", "carrier_task_id", "message_id", "metadata")} for r in receipts],
                     "evidence_digest": hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                     "truncated": sorted(selected.intersection(self._truncated)),
                     "outputs": outputs, "output_truncated_task_ids": output_truncated}
            version = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
            return {"version": version, "tasks": tasks, "work": work, "receipts": receipts,
                    "busy": bool(busy), "unresolved": unresolved, "evidence": evidence,
                    "evidence_truncated": bool(selected.intersection(self._truncated)),
                    "truncated_task_ids": sorted(selected.intersection(self._truncated)),
                    "outputs": outputs, "output_truncated_task_ids": output_truncated, "boot_id": self.boot_id}

    def _request_key(self, sid, request_id):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 300:
            raise ValueError("request_id is required (max 300)")
        return sid + ":" + request_id

    @staticmethod
    def _fingerprint(kind, sid, text, **options):
        return hashlib.sha256(json.dumps([kind, sid, text, options], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _existing_request(self, key, fingerprint):
        row = self._requests.get(key)
        if row is not None:
            if row.get("fingerprint") != fingerprint:
                raise ValueError("request identity conflict")
            if row.get("phase") in {"executing", "failed_closed", "inbound_pending", "outbound_pending"}:
                raise ValueError("request outcome is unconfirmed; automatic replay refused")
            self._save()  # A previous acknowledgement save may have failed.
        return row

    def _begin_request(self, key, fingerprint, kind):
        row = {"fingerprint": fingerprint, "kind": kind, "phase": "executing"}
        self._requests[key] = row
        try:
            self._save()  # Intent before business mutation; interrupted intent is never replayed.
        except Exception:
            self._requests.pop(key, None)
            raise
        return row

    def submit_input(self, sid, text, request_id, origin_task_id=""):
        with self.transaction():
            sid = self.session_info(sid)["session_id"]
            key = self._request_key(sid, request_id)
            fingerprint = self._fingerprint("input", sid, text, origin=origin_task_id)
            old = self._existing_request(key, fingerprint)
            if old is not None:
                return copy.deepcopy(old["result"])
            if origin_task_id and (self._is_paused(origin_task_id) or origin_task_id in self._related(self._closed)):
                raise ValueError("execution is paused or closed")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("input text is required")
            row = self._begin_request(key, fingerprint, "input")
            info = self.state.sessions[sid]
            payload = {"channel": info.channel, "conversation_key": info.conversation_key,
                       "text": text, "message_id": request_id, "use_context": True,
                       "_execution_origin_task_id": origin_task_id}
            try:
                evt = self.state.eventlog.append(session_id=sid, component="supervisor", type_="inbound_message", payload=payload)
                seq = int(evt["seq"])
                self.state._apply_inbound_message(seq=seq, session_id=sid, payload=payload)
                task = self.state._create_entry_task_for_inbound_locked(inbound_seq=seq, session_id=sid, payload=payload)
                if task is None:
                    raise ValueError("input did not create a task")
            except Exception:
                row["phase"] = "failed_closed"
                raise
            self._watched.add(task.task_id)
            row.update(phase="done", result={"task_id": task.task_id, "session_id": sid, "inbound_seq": seq})
            self._save()
            return copy.deepcopy(row["result"])

    def create_system_task(self, sid, node_id, instruction, request_id, node_overrides=None):
        from engine.faces.nodes import declared_node_owner
        with self.transaction():
            sid = self.session_info(sid)["session_id"]
            if not declared_node_owner(node_id):
                raise ValueError("system task node must be declared by a plugin")
            key = self._request_key(sid, request_id)
            options = dict(node_overrides or {})
            if set(options) - {"provider", "model", "max_steps"}:
                raise ValueError("unsupported execution option")
            fingerprint = self._fingerprint("system", sid, instruction, node_id=node_id, options=options)
            old = self._existing_request(key, fingerprint)
            if old is not None:
                return copy.deepcopy(old["result"])
            row = self._begin_request(key, fingerprint, "system")
            try:
                child, _ = self.state.get_or_create_child_session(sid, node_id, request_id, "fresh")
                task = self.state._create_task_from_hook_locked(
                    session_id=child, kind="node", node_id=node_id,
                    input_data={"instruction": instruction, "use_context": False, "_system_task": True,
                                "_execution_owner_session_id": sid,
                                "_execution_options": {"max_steps": 1, **options},
                                "task_context": {"is_system_task": True}})
            except Exception:
                row["phase"] = "failed_closed"
                raise
            row.update(phase="done", result={"task_id": task.task_id, "session_id": child, "owner_session_id": sid})
            self._watched.add(task.task_id)
            self._save()
            return copy.deepcopy(row["result"])

    def cancel_system_task(self, task_id):
        with self.transaction():
            task = self.state.tasks.get(task_id)
            if task is not None and task.input.get("_execution_owner_session_id"):
                return self.state.cancel_single_task(task_id)
            return {"ok": False, "error": "not a plugin system task"}

    def handle_input(self, sid, text, request_id):
        with self.state._lock:
            handlers = [callback for callback, matches in tuple(self._handlers) if matches is None or matches(text)]
            if not handlers:
                self.notify("input", sid, request_id=request_id)
                return None
            if getattr(self, "_load_error", False):
                # Handler matching remains plugin-owned. Non-command input must still
                # reach ordinary chat while all automatic operations stay fail-closed.
                for handler in handlers:
                    if handler(sid, text, request_id) is not None:
                        raise ValueError("execution state is unavailable")
                self.notify("input", sid, request_id=request_id)
                return None
            sid = self.session_info(sid)["session_id"]
            key = self._request_key(sid, request_id)
            fingerprint = self._fingerprint("control", sid, text)
            old = self._existing_request(key, fingerprint)
            if old is not None:
                if old["phase"] == "done":
                    return copy.deepcopy(old["result"])
                return self._resume_control(key, old)
            row = self._begin_request(key, fingerprint, "control")
            row.update(session_id=sid, text=text, request_id=request_id)
            try:
                for handler in handlers:
                    result = handler(sid, text, request_id)
                    if result is not None:
                        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                            raise ValueError("input handler must return text")
                        row.update(phase="prepared", result={**result, "handled": True, "session_id": sid})
                        self._save()  # Never call business/CAS a second time after this result.
                        return self._resume_control(key, row)
            except Exception:
                if row["phase"] == "executing":
                    row["phase"] = "failed_closed"
                raise
            self._requests.pop(key, None)
            self._save()
            self.notify("input", sid, request_id=request_id)
            return None

    def _resume_control(self, key, row):
        from engine.conversation_store import ConversationStore, Message
        from datetime import datetime, timezone
        sid = row["session_id"]
        info = self.state.sessions[sid]
        payload = {"channel": info.channel, "conversation_key": info.conversation_key,
                   "text": row["text"], "message_id": row["request_id"], "message_type": "control_input"}
        if row["phase"] == "prepared":
            row["phase"] = "inbound_pending"
            try:
                self._save()
            except Exception:
                row["phase"] = "prepared"
                raise
            try:
                evt = self.state.eventlog.append(session_id=sid, component="supervisor", type_="inbound_message", payload=payload)
            except Exception:
                row["phase"] = "failed_closed"
                raise
            row["result"]["inbound_seq"] = int(evt["seq"])
            row["phase"] = "history"
            self._save()
        seq = row["result"]["inbound_seq"]
        self.state._apply_inbound_message(seq=seq, session_id=sid, payload=payload)
        # Control input must never escape to the model if an ensuing disk write fails.
        self.state._inbound_processed.add(seq)
        self.state._advance_inbound_cursor()
        if row["phase"] == "history":
            now = datetime.now(timezone.utc).isoformat()
            mid = hashlib.sha256(key.encode()).hexdigest()
            store = ConversationStore(self.workspace_root / "data" / "conversations")
            store.invalidate_cache(sid)
            existing = {m.id: m for m in store.load(sid)}
            messages = [Message(id="control-user-" + mid, role="user", content=row["text"], created_at=now,
                                message_type="user_input", meta={"source_inbound_seq": seq}),
                        Message(id="control-reply-" + mid, role="assistant", content=row["result"]["text"], created_at=now,
                                message_type="assistant_text", meta={"source_inbound_seq": seq})]
            for message in messages:
                prior = existing.get(message.id)
                if prior and (prior.content != message.content or prior.role != message.role or prior.meta != message.meta):
                    row["phase"] = "failed_closed"
                    raise ValueError("control history identity conflict")
            missing = [m for m in messages if m.id not in existing]
            if missing:
                store.append_batch(sid, missing)
            store.invalidate_cache(sid)
            recorded = {m.id: m for m in store.load(sid)}
            if any(m.id not in recorded or recorded[m.id].content != m.content for m in messages):
                row["phase"] = "failed_closed"
                raise ValueError("control history write could not be confirmed")
            row["phase"] = "outbound"
            self._save()
        if row["phase"] == "outbound":
            row["phase"] = "outbound_pending"
            try:
                self._save()
            except Exception:
                row["phase"] = "outbound"
                raise
            try:
                self.state.append_outbound_message(session_id=sid, text=row["result"]["text"], source_inbound_seq=seq, action_type="reply")
            except Exception:
                row["phase"] = "outbound" if seq in self.state._inbound_routed else "failed_closed"
                raise
            row["phase"] = "done"
            self._save()
        return copy.deepcopy(row["result"])

    def set_paused(self, root_task_ids, paused):
        roots = [root_task_ids] if isinstance(root_task_ids, str) else root_task_ids
        with self.transaction():
            previous = set(self._paused)
            if paused:
                self._paused.update(roots)
            else:
                self._paused.difference_update(roots)
            try:
                self._save()
            except Exception:
                if not paused:
                    self._paused = previous
                raise
            if not paused:
                for receipt in list(self._receipts.values()):
                    if receipt.get("status") == "held" and not self._is_paused(receipt["origin_task_id"]):
                        self._route_receipt(receipt)

    def _is_paused(self, origin):
        return origin in self._related(self._paused)

    def queue_result(self, *, delivery_id, work_id, origin_task_id, session_id, text, attachments=None,
                     message_type="async_tool_result", metadata=None, target_generation=None):
        with self.transaction():
            info = self.session_info(session_id)
            if target_generation is not None and info["generation"] != target_generation:
                raise ValueError("callback target generation changed")
            if delivery_id in self._receipts:
                previous = self._receipts[delivery_id]
                expected = {"work_id": work_id, "origin_task_id": origin_task_id, "text": text,
                            "session_id": info["session_id"], "target_generation": info["generation"],
                            "attachments": list(attachments or []), "message_type": message_type,
                            "metadata": dict(metadata or {})}
                if any(previous.get(k) != v for k, v in expected.items()):
                    raise ValueError("delivery identity conflict")
                self._save()
                if previous["status"] == "held" and not self._is_paused(origin_task_id):
                    return self._route_receipt(previous)
                return {"ok": True, "deduped": True, "strategy": previous["status"], "task_id": previous.get("carrier_task_id", "")}
            if origin_task_id in self._related(self._closed):
                return {"ok": True, "strategy": "closed"}
            receipt = {"delivery_id": delivery_id, "work_id": work_id, "origin_task_id": origin_task_id,
                       "session_id": info["session_id"], "target_generation": info["generation"],
                       "status": "held", "text": text, "attachments": list(attachments or []),
                       "message_type": message_type, "metadata": dict(metadata or {}), "carrier_task_id": ""}
            self._receipts[delivery_id] = receipt
            self._save()  # Record the obligation before releasing it to a consumer.
            if self._is_paused(origin_task_id):
                return {"ok": True, "strategy": "held"}
            return self._route_receipt(receipt)

    def _route_receipt(self, receipt):
        sid = receipt["session_id"]
        if self.session_info(sid)["generation"] != receipt["target_generation"]:
            receipt.update(status="lost", error="target reset")
            self._save()
            return {"ok": False, "error": "target reset"}
        origin = receipt["origin_task_id"]
        carrier = self.state._find_dispatch_callback_carrier_locked(origin, sid)
        did = receipt["delivery_id"]
        text = receipt["text"]
        attachments = receipt["attachments"]
        if carrier is not None:
            self.state._append_preempt_to_task_locked(carrier, "<dispatch_result>\n" + text + "\n</dispatch_result>",
                                                      attachments, source=receipt["message_type"])
            carrier.preempt_delivery_ids.append(did)
            receipt.update(status="queued", carrier_task_id=carrier.task_id)
            strategy = "preempt"
        else:
            info = self.state.sessions[sid]
            payload = {"channel": info.channel, "conversation_key": info.conversation_key,
                       "text": text, "attachments": attachments, "message_id": did,
                       "message_type": receipt["message_type"], "delivery_ids": [did],
                       "_dispatch_owner_root_task_id": origin, **receipt["metadata"]}
            evt = self.state.eventlog.append(session_id=sid, component="supervisor", type_="inbound_message", payload=payload)
            seq = int(evt["seq"])
            self.state._apply_inbound_message(seq=seq, session_id=sid, payload=payload)
            carrier = self.state._create_entry_task_for_inbound_locked(inbound_seq=seq, session_id=sid, payload=payload)
            if carrier is None:
                receipt.update(status="lost", error="empty result could not create carrier")
            else:
                receipt.update(status="queued", carrier_task_id=carrier.task_id)
            strategy = "inbound"
        self._save()
        self.notify("task", sid, receipt=receipt)
        return {"ok": True, "strategy": strategy, "task_id": receipt["carrier_task_id"]}

    def admit(self, task_id, delivery_ids, message_id, execution_id=""):
        with self.transaction():
            task = self.state.tasks.get(task_id)
            if not task or (task.execution_id and task.execution_id != execution_id):
                raise ValueError("stale input acknowledgement")
            if (not isinstance(delivery_ids, list) or any(not isinstance(d, str) for d in delivery_ids)
                    or len(set(delivery_ids)) != len(delivery_ids) or not message_id):
                raise ValueError("delivery_ids and message_id required")
            if delivery_ids:
                from engine.conversation_store import ConversationStore
                sid = task.input.get("child_session_id") or task.session_id
                stored = next((m for m in ConversationStore(self.workspace_root / "data" / "conversations").load(sid) if m.id == message_id), None)
                if (stored is None or stored.source_task_id != task_id
                        or not set(delivery_ids).issubset((stored.meta or {}).get("delivery_ids", []))):
                    raise ValueError("callback input is not durably recorded")
            for did in delivery_ids:
                receipt = self._receipts.get(did)
                if not receipt or receipt.get("carrier_task_id") != task_id:
                    raise ValueError("receipt does not belong to this task")
                if receipt["status"] not in {"queued", "admitted", "processed"}:
                    raise ValueError("receipt cannot be admitted")
            for did in delivery_ids:
                row = self._receipts[did]
                if row["status"] == "queued":
                    row.update(status="admitted", message_id=message_id, admitted_execution_id=execution_id)
            self._save()
            return {"ok": True}

    def session_removed(self, sid):
        self.notify("session_removed", sid)
        for receipt in self._receipts.values():
            if receipt.get("session_id") == sid and receipt.get("status") != "processed":
                receipt.update(status="lost", error="target session removed")
        self._save()
