"""轻量定时调度器。

每分钟醒一次，扫描 data/schedules.yaml，到时间的往 inbound 队列注入消息。
调度线程挂了不影响 Supervisor 主进程。
"""
# 系统级定时任务通过 supervisor hook handler 接入，调度器只负责发 tick。

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from engine.context_store import cleanup_old_contexts
from toolbox._common import safe_subprocess_env


if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  schedules.yaml CRUD
# ---------------------------------------------------------------------------

def _schedules_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "schedules.yaml"


def load_schedules(workspace_root: Path) -> list[dict[str, Any]]:
    p = _schedules_path(workspace_root)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    items = data.get("schedules")
    if not isinstance(items, list):
        return []
    return [s for s in items if isinstance(s, dict)]


def save_schedules(workspace_root: Path, schedules: list[dict[str, Any]]) -> None:
    p = _schedules_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"schedules": schedules}, sort_keys=False, allow_unicode=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
#  Cron 匹配（5 字段：minute hour day month weekday）
# ---------------------------------------------------------------------------

def _match_field(field: str, value: int, max_val: int) -> bool:
    """判断 cron 单个字段是否匹配。支持 * / , - 和 */step。"""
    field = field.strip()
    if field == "*":
        return True

    # */step
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False

    # 逗号分隔
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue

    return False


def cron_match(expr: str, dt: datetime) -> bool:
    """判断 5 字段 cron 表达式是否匹配指定时间。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, dt.minute, 59)
        and _match_field(hour, dt.hour, 23)
        and _match_field(day, dt.day, 31)
        and _match_field(month, dt.month, 12)
        and _match_field(weekday, (dt.weekday() + 1) % 7, 6)  # cron: 0=Sun, Python: 0=Mon
    )


# ---------------------------------------------------------------------------
#  调度线程
# ---------------------------------------------------------------------------

class SchedulerThread:
    """后台线程。每 60 秒检查一次 schedules.yaml，匹配的注入 inbound。"""

    def __init__(self, *, state: "SupervisorState", workspace_root: Path) -> None:
        self._state = state
        self._workspace_root = workspace_root
        self._last_fired: dict[str, str] = {}  # schedule_id -> "YYYY-MM-DD HH:MM"
        self._last_tick_minute: datetime | None = None
        self._thread: threading.Thread | None = None
        self._running_scripts: set[str] = set()  # 防重入
        self._scripts_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("[scheduler] started")

    def _loop(self) -> None:
        tick_count = 0
        while True:
            try:
                self._tick()
            except Exception as e:
                log.warning(f"[scheduler] tick error: {e}")
            tick_count += 1
            # 每 30 分钟清理一次旧上下文快照（30 ticks × 60s = 30min）
            if tick_count % 30 == 0:
                self._cleanup()
            # Align sleep to the next minute boundary (+0.2s) to prevent cumulative drift
            sleep_sec = max(1.0, 60.0 - (time.time() % 60.0) + 0.2)
            time.sleep(sleep_sec)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        cur_min = now.replace(second=0, microsecond=0)
        last_min = self._last_tick_minute
        if last_min is not None:
            gap_sec = (cur_min - last_min).total_seconds()
            if 0 < gap_sec <= 300:
                steps = int(gap_sec // 60)
                minutes_to_check = [last_min + timedelta(minutes=i) for i in range(1, steps + 1)]
            elif gap_sec <= 0:
                minutes_to_check = [cur_min]
            else:
                minutes_to_check = [cur_min]
        else:
            minutes_to_check = [cur_min]
        self._last_tick_minute = cur_min

        schedules = load_schedules(self._workspace_root)

        # [2026-11-02] 审计修复：catchup 快照内 once 只能触发一次。
        # Why: _tick 为了补上休眠期间的分钟，会用同一份 schedules 快照循环多个
        # check_dt；once 的 _remove_schedule 只改磁盘，不改已加载的 snapshot，于是
        # "* * * * *" 的 once 任务在 5 分钟 catchup 里被注入 5 次。
        # How: 在本轮快照内用 fired_once 记录已触发的 once schedule id，命中即跳过。
        # Purpose: 一次任务不论补几个分钟都只执行一次。
        fired_once: set[str] = set()

        for check_dt in minutes_to_check:
            dt_key = check_dt.strftime("%Y-%m-%d %H:%M")
            for s in schedules:
                sid = str(s.get("id") or "").strip()
                if not sid:
                    continue
                if sid in fired_once:
                    continue
                if not s.get("enabled", True):
                    continue

                cron_expr = str(s.get("cron") or "").strip()
                if not cron_expr:
                    continue

                # 防止同一分钟重复触发
                if self._last_fired.get(sid) == dt_key:
                    continue

                if not cron_match(cron_expr, check_dt):
                    continue

                # 匹配成功
                self._last_fired[sid] = dt_key
                once = bool(s.get("once", False))
                stype = str(s.get("type") or "message").strip()

                if stype == "script":
                    threading.Thread(
                        target=self._fire_script,
                        args=(dict(s), sid),
                        daemon=True,
                        name=f"sched-script-{sid}",
                    ).start()
                else:
                    self._fire_message(s, sid)

                if once:
                    fired_once.add(sid)
                    self._remove_schedule(sid)

        now_key = cur_min.strftime("%Y-%m-%d %H:%M")

        # Why: system-level scheduled features now live in engine.builtin
        # supervisor hook handlers. How: emit a dream schedule tick with a
        # callback-only context under the state's re-entrant lock. Purpose: keep
        # SchedulerThread generic while avoiding direct handler access to state.
        with self._state._lock:
            self._state.hook_registry.fire(
                "on_schedule_tick",
                self._state._build_supervisor_hook_ctx(
                    schedule_type="dream",
                    now=now,
                    now_key=now_key,
                ),
            )

    def _inject_inbound(self, sid: str, text: str, conv_key: str, entry_node_id: str = "", attachments: list | None = None) -> None:
        """共用的 inbound 注入逻辑。"""
        msg_id = f"scheduler:{sid}:{uuid.uuid4()}"
        if ":" in conv_key:
            channel = conv_key.split(":", 1)[0]
        else:
            channel = "scheduler"
        try:
            session_id = self._state.get_or_create_session(
                channel=channel, conversation_key=conv_key,
            )
            payload: dict[str, Any] = {
                "channel": channel,
                "conversation_key": conv_key,
                "message_id": msg_id,
                "text": text,
                "schedule_id": sid,
                "entry_node_id": entry_node_id,
            }
            if attachments:
                payload["attachments"] = attachments
            evt = self._state.eventlog.append(
                session_id=session_id,
                component="scheduler",
                type_="inbound_message",
                payload=payload,
            )
            self._state.record_inbound_message_event(evt)
            log.info(f"[scheduler] fired: {sid} -> session={session_id}")
        except Exception as e:
            log.warning(f"[scheduler] inject failed for {sid}: {e}")

    def _fire_message(self, s: dict, sid: str) -> None:
        """type=message：直接注入文本（原有逻辑）。"""
        text = str(s.get("text") or f"[scheduled:{sid}]").strip()
        conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
        entry_node_id = str(s.get("entry_node_id") or "").strip()
        self._inject_inbound(sid, text, conv_key, entry_node_id)

    def _fire_script(self, s: dict, sid: str) -> None:
        """type=script：执行脚本，stdout JSON 解析后注入 inbound。

        脚本 stdout 格式要求：
        - 单行 JSON 对象，必须包含 "text" 字段：{"text": "消息内容"}
        - 可选 "attachments" 字段：{"text": "...", "attachments": ["path/to/file"]}
        - 空 stdout：silent=true 时静默跳过；silent=false 时仅注入 text_prefix
        - 非零退出码或非法 JSON 均视为失败，不注入消息
        """
        command = str(s.get("command") or "").strip()
        if not command:
            log.error(f"[scheduler] script {sid}: missing 'command' field")
            return
        with self._scripts_lock:
            if sid in self._running_scripts:
                log.warning(f"[scheduler] script {sid}: still running, skipping")
                return
            self._running_scripts.add(sid)
        proc: subprocess.Popen[str] | None = None
        try:
            timeout = int(s.get("timeout") or 30)
            silent = bool(s.get("silent", True))
            text_prefix = str(s.get("text") or "").strip()
            conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
            entry_node_id = str(s.get("entry_node_id") or "").strip()
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._workspace_root),
                env=safe_subprocess_env(),
                start_new_session=True,
            )
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=3)
                except Exception:
                    pass
                log.error(f"[scheduler] script {sid}: timed out after {timeout}s")
                return
            if stderr_raw and stderr_raw.strip():
                log.warning(f"[scheduler] script {sid} stderr: {stderr_raw.strip()[:500]}")
            if proc.returncode != 0:
                log.error(f"[scheduler] script {sid} exited with rc={proc.returncode}")
                return
            stdout = (stdout_raw or "").strip()
            if not stdout:
                if silent:
                    log.debug(f"[scheduler] script {sid}: empty stdout, silent skip")
                else:
                    log.info(f"[scheduler] script {sid}: empty stdout, injecting prefix")
                    if text_prefix:
                        self._inject_inbound(sid, text_prefix, conv_key, entry_node_id)
                return
            # 解析 JSON
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError as e:
                log.error(f"[scheduler] script {sid}: invalid JSON output: {e}")
                return
            body_text = str(data.get("text") or "").strip()
            if not body_text:
                log.error(f"[scheduler] script {sid}: JSON missing 'text' field")
                return
            if text_prefix:
                body_text = text_prefix + "\n" + body_text
            attachments = data.get("attachments") or None
            self._inject_inbound(sid, body_text, conv_key, entry_node_id, attachments)
        except Exception as e:
            log.error(f"[scheduler] script {sid}: unexpected error: {e}")
        finally:
            with self._scripts_lock:
                self._running_scripts.discard(sid)

    def _remove_schedule(self, sid: str) -> None:
        try:
            items = load_schedules(self._workspace_root)
            items = [s for s in items if str(s.get("id") or "").strip() != sid]
            save_schedules(self._workspace_root, items)
            log.info(f"[scheduler] removed once-schedule: {sid}")
        except Exception as e:
            log.warning(f"[scheduler] failed to remove once-schedule {sid}: {e}")

    def _cleanup(self) -> None:
        """定期清理旧的上下文快照和已完成 task 的内存。"""
        try:
            from .types import TaskStatus

            # 在锁内收集 keep_refs 快照
            with self._state._lock:
                keep_refs: set[str] = set()
                for task in self._state.tasks.values():
                    if task.status not in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
                        ref = str(task.input.get("context_ref") or "").strip()
                        if ref:
                            keep_refs.add(ref)
                        cref = str(task.continuation.get("resume_context_ref") or "").strip()
                        if cref:
                            keep_refs.add(cref)
                        for frame in (task.continuation.get("resume_stack") or []):
                            if isinstance(frame, dict):
                                fref = str(frame.get("context_ref") or "").strip()
                                if fref:
                                    keep_refs.add(fref)

            # 文件清理在锁外执行（IO 操作）
            count = cleanup_old_contexts(
                self._workspace_root,
                max_age_seconds=3600.0,  # 1 小时前的快照
                keep_refs=keep_refs,
            )
            if count > 0:
                log.info(f"[scheduler] cleaned up {count} old context snapshots")

            # 在锁内清理内存中已终结的旧 task（保留最近 80 个）并瘦身历史入站与事件缓冲
            with self._state._lock:
                terminal = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
                for _t in self._state.tasks.values():
                    if _t.status in terminal and isinstance(_t.result, dict):
                        _t.result.pop("_runtime_state", None)
                finished_ids = [
                    tid for tid in self._state._task_order
                    if tid in self._state.tasks and self._state.tasks[tid].status in terminal
                ]
                if len(finished_ids) > 80:
                    to_remove = finished_ids[:-80]
                    for tid in to_remove:
                        self._state.tasks.pop(tid, None)
                    self._state._task_order = [
                        tid for tid in self._state._task_order if tid in self._state.tasks
                    ]
                    log.info(f"[scheduler] pruned {len(to_remove)} old tasks from memory")

                # 清理已处理的入站消息 payload 并裁剪过老的入站序号（保留最近 500 个及所有活跃 task 关联序号）
                active_seqs = {
                    t.source_inbound_seq for t in self._state.tasks.values()
                    if t.source_inbound_seq is not None
                }
                for _seq, _ievt in list(self._state._inbound_events.items()):
                    if (_seq in self._state._inbound_processed or _seq in self._state._inbound_routed) and isinstance(_ievt, dict) and "payload" in _ievt:
                        self._state._inbound_events[_seq] = {"session_id": str(_ievt.get("session_id") or "")}
                if self._state._inbound_cursor > 500:
                    cut = self._state._inbound_cursor - 500
                    evict_seqs = [
                        s for s in self._state._inbound_order[:cut]
                        if s not in active_seqs
                    ]
                    if evict_seqs:
                        evict_set = set(evict_seqs)
                        for s in evict_seqs:
                            self._state._inbound_events.pop(s, None)
                            self._state._inbound_processed.discard(s)
                            self._state._inbound_routed.pop(s, None)
                            self._state._inbound_leases.pop(s, None)
                        self._state._inbound_order = [
                            s for s in self._state._inbound_order if s not in evict_set
                        ]
                        self._state._inbound_cursor = max(0, self._state._inbound_cursor - len(evict_seqs))

                if hasattr(self._state, "eventlog") and hasattr(self._state.eventlog, "_events"):
                    with self._state.eventlog._lock:
                        if len(self._state.eventlog._events) > 1000:
                            self._state.eventlog._events = self._state.eventlog._events[-1000:]

                # 回收超时的 running 状态 task（安全网，防止 worker 崩溃后 task 永久孤立）
                _now_ts = datetime.now(timezone.utc)
                _stale_cutoff = _now_ts - timedelta(minutes=10)
                _stale_ids = [
                    tid for tid, t in self._state.tasks.items()
                    if t.status == TaskStatus.running
                    and t.updated_at < _stale_cutoff
                ]
                for tid in _stale_ids:
                    task = self._state.tasks.get(tid)
                    if task is None or task.status != TaskStatus.running:
                        continue
                    _stale_min = int((_now_ts - task.updated_at).total_seconds() / 60)
                    task.status = TaskStatus.failed
                    task.result = {
                        "action": "fail",
                        "node_id": task.node_id or "",
                        "error": f"Task timed out (no response for {_stale_min} minutes, worker likely crashed)",
                    }
                    task.updated_at = _now_ts
                    task.lease_expires_at = None
                    # [2026-09-14] 超时回收也是终态，写回子会话 registry，避免
                    # supervisor 重启/裁剪后这类子会话在面板上变回 running 幽灵。
                    self._state._mark_child_session_terminal(task)
                    self._state._event_task_snapshot("task_completed", task, component="supervisor")
                    self._state._route_completed_task_locked(task)
                    log.warning(f"[scheduler] reaped stale running task {tid} (node={task.node_id}, stale {_stale_min}min)")
        except Exception as e:
            log.warning(f"[scheduler] cleanup error: {e}")
        try:
            import ctypes, gc
            gc.collect()
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

