"""One-shot task handoff for explicitly requested service restarts.

This is not crash recovery: only a plan sealed after workers stop is resumed.
Task history stays in ConversationStore; this record keeps scheduling identities.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ._helpers import _now
from .types import Approval, Task, TaskKind, TaskStatus


class RestartRecovery:
    def __init__(self, state: Any):
        self.state = state
        self.path = state.workspace_root / "data" / "restarts" / "pending.json"
        self.plan: dict[str, Any] | None = None
        self.preparing = False
        self.restoring = False
        self.protected_sessions: set[str] = set()
        self.info: dict[str, Any] = {}
        self.registered_generations: dict[str, str] = {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state.workspace_root / "data" / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(dir=temp, prefix="restart-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(filename, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(filename).unlink(missing_ok=True)

    def eligible(self, task: Task) -> bool:
        return (not self.state._task_terminal(task) and not task.cancel_requested
                and task.cancel_reason not in {"user_cancel", "user_pause"}
                and task.session_id not in self.state._cancelled_sessions
                and (not self.state._current_session_generation_locked(task.session_id)
                     or task.session_generation == self.state._current_session_generation_locked(task.session_id)))

    def begin(self, *, reason: str = "", task_id: str = "", tool_call_id: str = "") -> dict[str, Any]:
        with self.state._lock:
            if self.preparing:
                return self.public()
            if self.path.exists():
                old = json.loads(self.path.read_text(encoding="utf-8"))
                if old.get("phase") not in {"consumed", "aborted"}:
                    raise ValueError("An unfinished restart handoff already exists")
            selected = [t for t in self.state.tasks.values() if self.eligible(t)]
            if task_id and task_id not in {t.task_id for t in selected}:
                raise ValueError("Restart initiator is not an active task")
            restart_id = str(uuid.uuid4())
            self.plan = {"schema_version": 1, "restart_id": restart_id, "phase": "preparing",
                         "source_run_id": self.state.eventlog.run_id, "started_at": time.time(),
                         "reason": reason, "initiator_task_id": task_id,
                         "restart_tool_call_id": tool_call_id, "original_status": {}, "tasks": {},
                         "interrupted_async_ids": [aid for aid, row in self.state._async_tools.items()
                                                   if row.get("status") == "running" and not row.get("cancel_requested")]}
            previous = [(t, t.cancel_reason, t.restart_id) for t in selected]
            self.preparing = True
            try:
                for task in selected:
                    self.plan["original_status"][task.task_id] = task.status.value
                    task.restart_id = restart_id
                    task.cancel_reason = "service_restart"
                self.save()
            except Exception:
                for task, cancel_reason, old_id in previous:
                    task.cancel_reason, task.restart_id = cancel_reason, old_id
                self.preparing = False
                self.plan = None
                raise
            self.state.execution_runtime.notify("restart_preparing", restart_id=restart_id,
                                                task_ids=[t.task_id for t in selected])
            return self.public()

    def public(self) -> dict[str, Any]:
        if self.preparing and self.plan:
            return {"restart_id": self.plan["restart_id"], "preserve_tasks": True}
        if self.info:
            return {"restart_id": self.info["restart_id"], "preserve_tasks": True}
        return {}

    def registration(self, worker_id: str, generation_id: str) -> dict[str, Any]:
        if not self.info:
            return self.public() if self.preparing else {}
        first = self.registered_generations.setdefault(worker_id, generation_id)
        return self.public() if first == generation_id else {}

    def save(self) -> None:
        if not self.preparing or self.plan is None or self.restoring:
            return
        st = self.state
        # A completion can dispatch new work while shutdown is being delivered.
        for task in st.tasks.values():
            if self.eligible(task) and task.task_id not in self.plan["original_status"]:
                self.plan["original_status"][task.task_id] = task.status.value
                task.restart_id = self.plan["restart_id"]
                task.cancel_reason = "service_restart"
        # Keep terminal siblings/ancestors: batch and output routing read these records.
        self.plan["tasks"] = {tid: task.model_dump(mode="json") for tid, task in st.tasks.items()}
        self.plan["task_order"] = list(st._task_order)
        # Outbound validates and deduplicates against these indexes, not EventLog.
        # Restore them directly: replaying record_inbound would create duplicate tasks.
        self.plan["inbound_events"] = copy.deepcopy(st._inbound_events)
        self.plan["inbound_routed"] = copy.deepcopy(st._inbound_routed)
        self.plan["inbound_order"] = list(st._inbound_order)
        self.plan["inbound_processed"] = sorted(st._inbound_processed)
        self.plan["inbound_cursor"] = st._inbound_cursor
        self.plan["session_generations"] = dict(st.session_generations)
        self.plan["cancelled_sessions"] = sorted(st._cancelled_sessions)
        self.plan["approvals"] = {aid: a.model_dump(mode="json") for aid, a in st.approvals.items()}
        runtime = st.execution_runtime
        self.plan["paused_roots"] = sorted(runtime._paused)
        self.plan["async_tools"] = copy.deepcopy(st._async_tools)
        self.plan["receipts"] = copy.deepcopy(runtime._receipts)
        self._write(self.plan)

    def task_changed(self, task: Task) -> None:
        if task.cancel_requested and task.cancel_reason in {"", "service_restart"}:
            # User/session cancellation wins, even after the restart plan was saved.
            task.cancel_reason = "user_cancel"
        self.save()

    def intercept_completion(self, task: Task, result: dict[str, Any]) -> bool:
        if not self.preparing or not self.plan or task.restart_id != self.plan["restart_id"]:
            return False
        if (not self.eligible(task) or result.get("action") != "cancelled"
                or not (result.get("interruption_reason") == "service_restart"
                        or result.get("error") == "engine graceful shutdown")):
            return False
        task.status = TaskStatus.suspended
        task.cancel_reason = "service_restart"
        task.result = {**task.result, **copy.deepcopy(result), "interruption_reason": "service_restart"}
        task.worker_id = None
        task.lease_expires_at = None
        task.updated_at = _now()
        task.completion_state = "interrupted"
        self.state._event_task_snapshot("task_interrupted", task, component="engine")
        return True

    def finalize(self) -> None:
        with self.state._lock:
            if not self.preparing or self.plan is None:
                raise ValueError("No restart handoff is being prepared")
            for task in self.state.tasks.values():
                if not self.eligible(task) or task.restart_id != self.plan["restart_id"]:
                    continue
                if task.status == TaskStatus.running:
                    task.status = TaskStatus.suspended
                    task.completion_state = "interrupted"
                    task.result = {**task.result, "action": "cancelled",
                                   "interruption_reason": "service_restart",
                                   "error": "Operation interrupted by service restart; outcome may be unconfirmed.",
                                   "checkpoint_error": "Worker stopped without confirming a saved execution checkpoint"}
                    task.updated_at = _now()
                    task.worker_id = None
                    task.lease_expires_at = None
            interrupted = set(self.plan.get("interrupted_async_ids", []))
            interrupted.update(aid for aid, row in self.state._async_tools.items()
                               if row.get("status") == "running" and not row.get("cancel_requested"))
            for aid in interrupted:
                row = self.state._async_tools.get(aid)
                if row and row.get("status") in {"running", "cancelled"}:
                    row.update(status="cancelled", cancel_reason="service_restart", error="Service restart interrupted tool")
                    runtime = self.state.execution_runtime
                    # Existing held receipts are obligations too. Never bypass pause,
                    # duplicate a delivered result, or invent a second delivery channel.
                    receipts = [r for r in runtime._receipts.values() if r.get("work_id") == aid]
                    if receipts:
                        if any(r.get("status") == "processed" for r in receipts):
                            row["restart_result_consumed"] = True
                        continue
                    owner = self.state.tasks.get(row.get("task_id", ""))
                    if owner is None or owner.cancel_requested or owner.cancel_reason in {"user_cancel", "user_pause"}:
                        continue
                    sid = row.get("session_id") or self.state._route_session_id_for_task_locked(owner)
                    if sid not in self.state.sessions or sid in self.state._cancelled_sessions:
                        continue
                    runtime.queue_result(
                        delivery_id=f"restart:{self.plan['restart_id']}:{aid}", work_id=aid,
                        origin_task_id=owner.task_id, session_id=sid,
                        target_generation=row.get("target_generation"),
                        text=f"Async tool {row.get('tool_name') or aid} was cancelled by service restart. "
                             "The operation did not return a confirmed result; external side effects may already have occurred. "
                             "Check actual state before retrying.",
                        metadata={"async_id": aid, "cancelled": True, "interruption_reason": "service_restart"},
                    )
            self.plan["phase"] = "ready"
            self.plan["sealed_at"] = time.time()
            self.save()

    def restore(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported restart handoff schema")
        if data.get("phase") in {"consumed", "aborted"}:
            return {}
        if data.get("phase") != "ready":
            raise ValueError("Restart handoff was not sealed; refusing to guess execution state")
        st = self.state
        # SessionStore deliberately omits non-accumulating children from the active
        # map on ordinary boots. A sealed handoff is explicit permission to retain
        # these registry entries without mapping a branch over its parent channel.
        from datetime import datetime
        from .session_store import _session_info_from_entry
        for raw in data["tasks"].values():
            for sid in (raw.get("session_id"), (raw.get("input") or {}).get("child_session_id")):
                if not sid or sid in st.sessions:
                    continue
                entry = st._session_store._registry.get(sid)
                if entry and not entry.get("reset"):
                    created = datetime.fromisoformat(entry["created_at"]) if entry.get("created_at") else _now()
                    updated = datetime.fromisoformat(entry["updated_at"]) if entry.get("updated_at") else created
                    st.sessions[sid] = _session_info_from_entry(sid, entry, created, updated)
        tasks = {tid: Task.model_validate(raw) for tid, raw in data["tasks"].items()}
        for tid, task in tasks.items():
            if task.task_id != tid:
                raise ValueError("Restart task identity mismatch")
        approvals = {aid: Approval.model_validate(raw) for aid, raw in data.get("approvals", {}).items()}
        restored: list[str] = []
        cancelled_sessions = set(data.get("cancelled_sessions", []))
        for task in tasks.values():
            if st._task_terminal(task) or task.restart_id != data["restart_id"]:
                continue
            registry = st._session_store._registry.get(task.session_id, {})
            if (task.cancel_requested or task.cancel_reason != "service_restart"
                    or task.session_id in cancelled_sessions or task.session_id not in st.sessions
                    or registry.get("reset")):
                task.status = TaskStatus.cancelled
                task.cancel_requested = True
                continue
            interrupted = task.completion_state == "interrupted" or task.status == TaskStatus.running
            if interrupted and task.kind == TaskKind.tool:
                task.status = TaskStatus.pending
                task.input["_restart_cancelled_tool"] = True
            elif task.result.get("checkpoint_error"):
                task.status = TaskStatus.suspended
                task.cancel_reason = "restart_checkpoint_failed"
            elif interrupted:
                task.status = TaskStatus.pending
                previous_resume = task.input.get("resume_data") or {}
                task.input["resume_data"] = {"type": "restart_interrupted", "restart_id": data["restart_id"],
                                             "previous_resume_data": previous_resume}
                if task.task_id == data.get("initiator_task_id"):
                    task.input["resume_data"]["restart_tool_call_id"] = data.get("restart_tool_call_id", "")
            # pending never began: keep original input; suspended still waits for children.
            task.worker_id = None
            task.lease_expires_at = None
            task.execution_id = str(uuid.uuid4())  # fence old completions before the first new claim
            task.completed_execution_id = ""
            task.completion_state = ""
            task.updated_at = _now()
            task.current_phase = ""
            task.current_detail = ""
            restored.append(task.task_id)
        info = {"restart_id": data["restart_id"], "restored_task_ids": restored,
                "resumed_task_ids": restored, "preserved_task_ids": list(tasks),
                "planned_restart": True, "downtime_sec": max(0.0, time.time() - data["started_at"])}
        self.restoring = True
        try:
            st.tasks.update(tasks)
            st._task_order = [tid for tid in data.get("task_order", []) if tid in tasks]
            st._inbound_events = {int(seq): row for seq, row in data.get("inbound_events", {}).items()}
            st._inbound_routed = {int(seq): row for seq, row in data.get("inbound_routed", {}).items()}
            st._inbound_order = list(data.get("inbound_order", []))
            st._inbound_processed = set(data.get("inbound_processed", []))
            st._inbound_cursor = int(data.get("inbound_cursor", 0))
            st.session_generations.update(data.get("session_generations", {}))
            st._cancelled_sessions.update(cancelled_sessions)
            st.approvals.update(approvals)
            for task in tasks.values():
                if not st._task_terminal(task):
                    self.protected_sessions.add(task.session_id)
                    child = task.input.get("child_session_id")
                    if child:
                        self.protected_sessions.add(child)
            # Ancestor session registries are required to route callbacks and permissions.
            for sid in list(self.protected_sessions):
                while sid:
                    sid = st._session_store._registry.get(sid, {}).get("parent_session_id", "")
                    if not sid or sid in self.protected_sessions:
                        break
                    self.protected_sessions.add(sid)
            runtime = st.execution_runtime
            runtime._paused = set(data.get("paused_roots", []))
            runtime.recovery_info = info
            runtime.planned_restart = info
            st._async_tools = copy.deepcopy(data.get("async_tools", {}))
            runtime._receipts = copy.deepcopy(data.get("receipts", {}))
            for task in tasks.values():
                runtime.task_changed(task, notify=False)
            runtime._save()
            st.eventlog.append(session_id="__system__", component="supervisor", type_="restart_recovered", payload=info)
            for tid in restored:
                st._event_task_snapshot("task_resumed", tasks[tid])
            self.info = info
            return info
        finally:
            self.restoring = False

    def consume(self) -> None:
        """Commit only after startup reconciliation, before plugins may schedule work."""
        if not self.info:
            return
        self._write({"schema_version": 1, "restart_id": self.info["restart_id"],
                     "phase": "consumed", "consumed_by": self.state.eventlog.run_id,
                     "consumed_at": time.time()})
