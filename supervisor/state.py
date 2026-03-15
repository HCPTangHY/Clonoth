from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_str, load_runtime_config
from engine.graph import handoff_target, load_workflow, next_node

from .eventlog import EventLog, SYSTEM_SESSION_ID
from .policy import PolicyEngine
from .types import (
    AdminStateOut,
    Approval,
    ApprovalStatus,
    OpRequestOut,
    SafetyLevel,
    Task,
    TaskKind,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionInfo:
    session_id: str
    channel: str
    conversation_key: str
    created_at: datetime
    updated_at: datetime


class SupervisorState:
    """Supervisor 核心状态。管理会话、inbound 队列、task 队列与审批。"""

    def __init__(self, *, workspace_root: Path, eventlog: EventLog, policy: PolicyEngine) -> None:
        self.workspace_root = workspace_root
        self.eventlog = eventlog
        self.policy = policy
        self.started_at = _now()

        self._lock = threading.RLock()

        self.sessions: dict[str, SessionInfo] = {}
        self.conversation_map: dict[str, str] = {}
        self.session_generations: dict[str, int] = {}

        self.approvals: dict[str, Approval] = {}

        self.tasks: dict[str, Task] = {}
        self._task_order: list[str] = []

        self._engine_last_seen_at: datetime | None = None
        self._engine_last_worker_id: str | None = None

        self._inbound_order: list[int] = []
        self._inbound_events: dict[int, dict[str, Any]] = {}
        self._inbound_processed: set[int] = set()
        self._inbound_routed: dict[int, dict[str, Any]] = {}
        self._inbound_cursor: int = 0

        @dataclass
        class _InboundLease:
            worker_id: str
            expires_at: datetime

        self._InboundLease = _InboundLease
        self._inbound_leases: dict[int, _InboundLease] = {}

        self._cancelled_sessions: set[str] = set()
        self._tools_reload_seq: int = 0

        self.rebuild_from_events(eventlog.events)

    # ---- 基础工具 ----

    def _default_workflow_id(self) -> str:
        cfg = load_runtime_config(self.workspace_root)
        return get_str(cfg, "shell.workflow_id", "bootstrap.default_chat").strip() or "bootstrap.default_chat"

    def _entry_node_for_workflow(self, workflow_id: str) -> str:
        wf = load_workflow(self.workspace_root, workflow_id)
        if wf is None or not wf.entry_node:
            return "bootstrap.shell_orchestrator"
        return wf.entry_node

    def _task_terminal(self, task: Task) -> bool:
        return task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}

    def _to_positive_int(self, value: Any) -> int | None:
        try:
            v = int(value)
        except Exception:
            return None
        return v if v > 0 else None

    def _next_session_generation_locked(self, session_id: str) -> int:
        cur = int(self.session_generations.get(session_id, 0) or 0) + 1
        self.session_generations[session_id] = cur
        return cur

    def _current_session_generation_locked(self, session_id: str) -> int:
        return int(self.session_generations.get(session_id, 0) or 0)

    def _event_task_snapshot(self, event_type: str, task: Task, *, component: str = "supervisor") -> None:
        self.eventlog.append(
            session_id=task.session_id,
            component=component,
            type_=event_type,
            payload=task.model_dump(mode="json"),
        )

    def _apply_task_snapshot(self, payload: dict[str, Any]) -> None:
        try:
            t = Task.model_validate(payload)
        except Exception:
            return
        existed = t.task_id in self.tasks
        self.tasks[t.task_id] = t
        if not existed:
            self._task_order.append(t.task_id)
        cur = self.session_generations.get(t.session_id, 0)
        if t.session_generation > cur:
            self.session_generations[t.session_id] = t.session_generation

    def _apply_cancel_requested(self, session_id: str, payload: dict[str, Any]) -> None:
        sid = str(session_id or payload.get("session_id") or "")
        if not sid:
            return
        self._cancelled_sessions.add(sid)
        gen = self._to_positive_int(payload.get("session_generation"))
        if gen is not None and gen > self.session_generations.get(sid, 0):
            self.session_generations[sid] = gen

    def _create_task_locked(
        self,
        *,
        session_id: str,
        session_generation: int,
        workflow_id: str,
        kind: TaskKind,
        node_id: str | None = None,
        tool_name: str | None = None,
        input_data: dict[str, Any] | None = None,
        continuation: dict[str, Any] | None = None,
        source_inbound_seq: int | None = None,
        parent_task_id: str | None = None,
    ) -> Task:
        now = _now()
        task = Task(
            task_id=str(uuid.uuid4()),
            session_id=session_id,
            session_generation=session_generation,
            workflow_id=workflow_id,
            kind=kind,
            node_id=node_id,
            tool_name=tool_name,
            input=dict(input_data or {}),
            continuation=dict(continuation or {}),
            source_inbound_seq=source_inbound_seq,
            parent_task_id=parent_task_id,
            status=TaskStatus.pending,
            cancel_requested=False,
            worker_id=None,
            created_at=now,
            updated_at=now,
            lease_expires_at=None,
            result={},
        )
        self.tasks[task.task_id] = task
        self._task_order.append(task.task_id)
        self._event_task_snapshot("task_created", task)
        return task

    def _cancel_session_tasks_locked(self, session_id: str) -> None:
        now = _now()
        for task in self.tasks.values():
            if task.session_id != session_id or self._task_terminal(task):
                continue
            if task.status == TaskStatus.pending:
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = now
                task.lease_expires_at = None
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.running and not task.cancel_requested:
                task.cancel_requested = True
                task.updated_at = now
                self._event_task_snapshot("task_cancel_requested", task)

    def _create_entry_task_for_inbound_locked(self, *, inbound_seq: int, session_id: str, payload: dict[str, Any]) -> Task | None:
        text = str(payload.get("text") or "").strip()
        has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
        if not text and not has_attachments:
            return None
        workflow_id = str(payload.get("workflow_id") or "").strip() or self._default_workflow_id()
        entry_node = self._entry_node_for_workflow(workflow_id)
        generation = self._current_session_generation_locked(session_id) or 1
        if not self.session_generations.get(session_id):
            self.session_generations[session_id] = generation
        self._cancelled_sessions.discard(session_id)
        # 收集当前活跃 task 摘要，注入给入口节点 AI 判断
        active_tasks_summary = self._active_tasks_summary_locked(session_id)
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
        return self._create_task_locked(
            session_id=session_id,
            session_generation=generation,
            workflow_id=workflow_id,
            kind=TaskKind.node,
            node_id=entry_node,
            input_data={
                "instruction": text,
                "context_ref": "",
                "resume_data": {},
                "workflow_id": workflow_id,
                "active_tasks_summary": active_tasks_summary,
                "attachments": attachments or [],
            },
            continuation={"resume_stack": []},
            source_inbound_seq=inbound_seq,
            parent_task_id=None,
        )

    def _create_resume_task_from_frame_locked(
        self,
        *,
        session_id: str,
        session_generation: int,
        frame: dict[str, Any],
        resume_stack: list[dict[str, Any]],
        resume_data: dict[str, Any],
        parent_task_id: str | None,
    ) -> Task:
        workflow_id = str(frame.get("workflow_id") or self._default_workflow_id()).strip() or self._default_workflow_id()
        source_inbound_seq = self._to_positive_int(frame.get("source_inbound_seq"))
        return self._create_task_locked(
            session_id=session_id,
            session_generation=session_generation,
            workflow_id=workflow_id,
            kind=TaskKind.node,
            node_id=str(frame.get("node_id") or "").strip() or self._entry_node_for_workflow(workflow_id),
            input_data={
                "instruction": str(frame.get("instruction") or "").strip(),
                "context_ref": str(frame.get("context_ref") or "").strip(),
                "resume_data": dict(resume_data or {}),
                "workflow_id": workflow_id,
            },
            continuation={"resume_stack": list(resume_stack or [])},
            source_inbound_seq=source_inbound_seq,
            parent_task_id=parent_task_id,
        )

    def _active_tasks_summary_locked(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session 中活跃（pending/running）task 的摘要列表。"""
        result: list[dict[str, Any]] = []
        for task in self.tasks.values():
            if task.session_id != session_id:
                continue
            if self._task_terminal(task):
                continue
            result.append({
                "task_id": task.task_id,
                "kind": task.kind.value,
                "node_id": task.node_id or "",
                "tool_name": task.tool_name or "",
                "status": task.status.value,
                "instruction": str(task.input.get("instruction") or "")[:200],
                "created_at": task.created_at.isoformat() if task.created_at else "",
            })
        return result

    def cancel_active_tasks(self, session_id: str, *, exclude_task_id: str = "") -> dict[str, Any]:
        """取消指定 session 中所有活跃 task，但保留 exclude_task_id 所在的调用链。"""
        sid = (session_id or "").strip()
        if not sid:
            return {"ok": False, "error": "empty session_id"}
        with self._lock:
            # 收集当前调用链上的 task id，不取消它们
            keep_ids: set[str] = set()
            tid = (exclude_task_id or "").strip()
            while tid:
                keep_ids.add(tid)
                t = self.tasks.get(tid)
                if t is None:
                    break
                tid = (t.parent_task_id or "").strip()

            now = _now()
            count = 0
            for task in self.tasks.values():
                if task.session_id != sid or self._task_terminal(task) or task.task_id in keep_ids:
                    continue
                if task.status == TaskStatus.pending:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.running and not task.cancel_requested:
                    task.cancel_requested = True
                    task.updated_at = now
                    self._event_task_snapshot("task_cancel_requested", task)
                    count += 1
            return {"ok": True, "cancelled_count": count}

    # ---- 引擎心跳 ----

    def mark_engine_seen(self, *, worker_id: str) -> None:
        with self._lock:
            self._engine_last_seen_at = _now()
            self._engine_last_worker_id = worker_id

    def engine_seen_snapshot(self) -> tuple[datetime | None, str | None]:
        with self._lock:
            return self._engine_last_seen_at, self._engine_last_worker_id

    # ---- 工具热重载信号 ----

    def bump_tools_reload(self) -> int:
        with self._lock:
            self._tools_reload_seq += 1
            return self._tools_reload_seq

    def tools_reload_seq(self) -> int:
        with self._lock:
            return self._tools_reload_seq

    # ---- 事件回放 ----

    def rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            et = e.get("type")
            session_id = str(e.get("session_id") or "")
            payload = e.get("payload") or {}

            try:
                seq = int(e.get("seq", 0))
            except Exception:
                seq = 0

            if et == "session_created":
                self._apply_session_created(session_id, payload)
            elif et == "inbound_message":
                conv = payload.get("conversation_key")
                if isinstance(conv, str) and session_id:
                    self.conversation_map[conv] = session_id
                self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "inbound_processed":
                self._apply_inbound_processed(payload)
            elif et == "outbound_message":
                self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "approval_requested":
                self._apply_approval_requested(payload)
            elif et == "approval_decided":
                self._apply_approval_decided(payload)
            elif et in {"task_created", "task_started", "task_completed", "task_cancelled", "task_cancel_requested", "task_requeued"}:
                self._apply_task_snapshot(payload)
            elif et == "cancel_requested":
                self._apply_cancel_requested(session_id, payload)

        self._advance_inbound_cursor()

    # ---- inbound 游标 ----

    def _advance_inbound_cursor(self) -> None:
        while self._inbound_cursor < len(self._inbound_order):
            seq = self._inbound_order[self._inbound_cursor]
            if seq in self._inbound_processed or seq in self._inbound_routed:
                self._inbound_cursor += 1
                continue
            return

    # ---- 事件 apply ----

    def _apply_session_created(self, session_id: str, payload: dict[str, Any]) -> None:
        try:
            created_at = datetime.fromisoformat(payload.get("created_at"))
        except Exception:
            created_at = _now()
        info = SessionInfo(
            session_id=str(session_id or ""),
            channel=str(payload.get("channel") or ""),
            conversation_key=str(payload.get("conversation_key") or ""),
            created_at=created_at,
            updated_at=created_at,
        )
        self.sessions[info.session_id] = info
        conv = info.conversation_key
        if conv:
            self.conversation_map[conv] = info.session_id

    def _apply_inbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not isinstance(seq, int) or seq <= 0 or not session_id:
            return
        if seq not in self._inbound_events:
            self._inbound_events[seq] = {"session_id": session_id, "payload": payload}
            self._inbound_order.append(seq)

    def _apply_inbound_processed(self, payload: dict[str, Any]) -> None:
        try:
            inbound_seq = int(payload.get("inbound_seq", 0))
        except Exception:
            return
        if inbound_seq > 0:
            self._inbound_processed.add(inbound_seq)

    def _apply_outbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not session_id or not isinstance(payload, dict):
            return
        src = payload.get("source_inbound_seq")
        try:
            inbound_seq = int(src) if src is not None else 0
        except Exception:
            inbound_seq = 0
        if inbound_seq > 0 and inbound_seq not in self._inbound_routed:
            self._inbound_routed[inbound_seq] = {
                "action": "reply",
                "session_id": session_id,
                "event_seq": int(seq or 0),
            }

    def _apply_approval_requested(self, payload: dict[str, Any]) -> None:
        try:
            a = Approval.model_validate(payload)
        except Exception:
            return
        self.approvals[a.approval_id] = a

    def _apply_approval_decided(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("approval_id")
        if approval_id in self.approvals:
            a = self.approvals[approval_id]
            decision = payload.get("decision")
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = payload.get("comment")
            a.decided_at = _now()

    # ---- 公开方法 ----

    def cancel_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self._lock:
            if sid not in self.sessions:
                return False
            generation = self._next_session_generation_locked(sid)
            self._cancelled_sessions.add(sid)
            self._cancel_session_tasks_locked(sid)
            self.eventlog.append(
                session_id=sid,
                component="supervisor",
                type_="cancel_requested",
                payload={"session_id": sid, "ts": _now().isoformat(), "session_generation": generation},
            )
            return True

    def is_cancelled(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._cancelled_sessions

    def clear_cancelled(self, session_id: str) -> None:
        with self._lock:
            self._cancelled_sessions.discard(session_id)

    def is_task_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get((task_id or "").strip())
            if task is None:
                return True
            current_gen = self._current_session_generation_locked(task.session_id)
            if current_gen and task.session_generation != current_gen:
                return True
            return task.cancel_requested or task.status == TaskStatus.cancelled

    def record_inbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "inbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            if session_id and session_id in self.sessions:
                self._create_entry_task_for_inbound_locked(inbound_seq=seq, session_id=session_id, payload=payload)

    def record_outbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "outbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not session_id or not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()

    # ---- inbound 队列（兼容保留） ----

    def assign_next_inbound(self, *, worker_id: str, lease_sec: float = 30.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        try:
            lease_val = float(lease_sec)
        except Exception:
            lease_val = 30.0
        lease_val = max(1.0, min(600.0, lease_val))
        now = _now()

        with self._lock:
            for seq, lease in list(self._inbound_leases.items()):
                if lease.expires_at <= now:
                    self._inbound_leases.pop(seq, None)

            self._advance_inbound_cursor()

            for i in range(self._inbound_cursor, len(self._inbound_order)):
                seq = self._inbound_order[i]
                if seq in self._inbound_processed or seq in self._inbound_routed:
                    if i == self._inbound_cursor:
                        self._inbound_cursor += 1
                    self._inbound_leases.pop(seq, None)
                    continue

                lease = self._inbound_leases.get(seq)
                if lease is not None and lease.expires_at > now:
                    continue

                evt = self._inbound_events.get(seq)
                if not isinstance(evt, dict):
                    continue

                session_id = str(evt.get("session_id") or "")
                payload = evt.get("payload")
                if not session_id or not isinstance(payload, dict):
                    continue

                self._inbound_leases[seq] = self._InboundLease(
                    worker_id=wid,
                    expires_at=now + timedelta(seconds=lease_val),
                )
                return {"inbound_seq": seq, "session_id": session_id, **payload}

            return None

    def ack_inbound(self, *, inbound_seq: int, worker_id: str) -> bool:
        wid = (worker_id or "").strip()
        if not wid:
            return False
        try:
            seq = int(inbound_seq)
        except Exception:
            seq = 0
        if seq <= 0:
            return False

        with self._lock:
            if seq in self._inbound_processed:
                return True
            if seq not in self._inbound_events:
                return False
            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="shell",
                type_="inbound_processed",
                payload={"inbound_seq": seq, "worker_id": wid, "ts": _now().isoformat()},
            )
            self._inbound_processed.add(seq)
            self._inbound_leases.pop(seq, None)
            self._advance_inbound_cursor()
            return True

    # ---- task 队列 ----

    def assign_next_task(self, *, worker_id: str, lease_sec: float = 120.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        try:
            lease_val = float(lease_sec)
        except Exception:
            lease_val = 120.0
        lease_val = max(1.0, min(3600.0, lease_val))
        now = _now()

        with self._lock:
            for task in self.tasks.values():
                if task.status == TaskStatus.running and task.lease_expires_at and task.lease_expires_at <= now:
                    if task.cancel_requested:
                        task.status = TaskStatus.cancelled
                        task.updated_at = now
                        task.lease_expires_at = None
                        self._event_task_snapshot("task_cancelled", task)
                    else:
                        task.status = TaskStatus.pending
                        task.worker_id = None
                        task.updated_at = now
                        task.lease_expires_at = None
                        self._event_task_snapshot("task_requeued", task)

            for task_id in self._task_order:
                task = self.tasks.get(task_id)
                if task is None or task.status != TaskStatus.pending:
                    continue
                current_gen = self._current_session_generation_locked(task.session_id)
                if (current_gen and task.session_generation != current_gen) or task.cancel_requested:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_cancelled", task)
                    continue
                task.status = TaskStatus.running
                task.worker_id = wid
                task.updated_at = now
                task.lease_expires_at = now + timedelta(seconds=lease_val)
                self._event_task_snapshot("task_started", task)
                return task.model_dump(mode="json")
            return None

    def complete_task(self, *, task_id: str, worker_id: str, result: dict[str, Any]) -> Task | None:
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        if not tid or not wid:
            return None
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            if self._task_terminal(task):
                return task
            if task.worker_id and task.worker_id != wid and task.lease_expires_at and task.lease_expires_at > _now():
                return None

            current_gen = self._current_session_generation_locked(task.session_id)
            if task.cancel_requested or (current_gen and task.session_generation != current_gen):
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = _now()
                task.lease_expires_at = None
                task.result = dict(result or {})
                self._event_task_snapshot("task_cancelled", task, component="engine")
                return task

            task.result = dict(result or {})
            task.updated_at = _now()
            task.lease_expires_at = None

            kind = str(task.result.get("kind") or "")
            outcome = str(task.result.get("outcome") or "")
            if kind == "final" and outcome == "cancelled":
                task.status = TaskStatus.cancelled
            elif kind == "final" and outcome == "failed":
                task.status = TaskStatus.failed
            else:
                task.status = TaskStatus.completed

            self._event_task_snapshot("task_completed", task, component="engine")
            self._route_completed_task_locked(task)
            return task

    def _route_completed_task_locked(self, task: Task) -> None:
        if task.kind == TaskKind.tool:
            self._route_tool_completion_locked(task)
            return

        result = dict(task.result or {})
        result_kind = str(result.get("kind") or "final")
        if result_kind == "yield_tool":
            self._route_node_yield_tool_locked(task, result)
        else:
            self._route_node_final_locked(task, result)

    def _route_node_yield_tool_locked(self, task: Task, result: dict[str, Any]) -> None:
        tool_calls = result.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return
        context_ref = str(result.get("context_ref") or "").strip()
        batch_id = str(uuid.uuid4())
        resume_stack = list(task.continuation.get("resume_stack") or [])
        for idx, raw_tc in enumerate(tool_calls):
            if not isinstance(raw_tc, dict):
                continue
            name = str(raw_tc.get("name") or "").strip()
            if not name:
                continue
            arguments = raw_tc.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            self._create_task_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                workflow_id=task.workflow_id,
                kind=TaskKind.tool,
                tool_name=name,
                input_data={
                    "arguments": arguments,
                    "call_id": str(raw_tc.get("id") or "").strip(),
                    "batch_id": batch_id,
                    "tool_index": idx,
                },
                continuation={
                    "resume_context_ref": context_ref,
                    "resume_node_id": str(task.node_id or ""),
                    "resume_instruction": str(task.input.get("instruction") or ""),
                    "resume_stack": resume_stack,
                    "source_inbound_seq": task.source_inbound_seq,
                    "batch_id": batch_id,
                },
                source_inbound_seq=task.source_inbound_seq,
                parent_task_id=task.task_id,
            )

    def _route_node_final_locked(self, task: Task, result: dict[str, Any]) -> None:
        outcome = str(result.get("outcome") or "").strip()
        text = str(result.get("text") or "").strip()
        summary = str(result.get("summary") or "").strip()
        resume_stack = list(task.continuation.get("resume_stack") or [])
        result_attachments = result.get("attachments") if isinstance(result.get("attachments"), list) else None

        if outcome == "reply":
            if text or result_attachments:
                self.append_outbound_message(
                    session_id=task.session_id,
                    text=text,
                    attachments=result_attachments,
                    source_inbound_seq=task.source_inbound_seq,
                )
            return

        if outcome == "cancelled":
            return

        wf = load_workflow(self.workspace_root, task.workflow_id)
        target = next_node(wf, str(task.node_id or ""), outcome) if wf is not None else ""

        ho_target = handoff_target(wf, str(task.node_id or ""), outcome) if wf is not None else ""
        if ho_target and not ho_target.startswith("$"):
            resume_stack.append({
                "node_id": str(task.node_id or ""),
                "workflow_id": task.workflow_id,
                "context_ref": str(result.get("context_ref") or "").strip(),
                "instruction": str(task.input.get("instruction") or ""),
                "source_inbound_seq": task.source_inbound_seq,
            })
            self._create_task_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                workflow_id=task.workflow_id,
                kind=TaskKind.node,
                node_id=ho_target,
                input_data={
                    "instruction": str(result.get("instruction") or text or "").strip(),
                    "context_ref": "",
                    "resume_data": {},
                    "workflow_id": task.workflow_id,
                },
                continuation={"resume_stack": resume_stack},
                source_inbound_seq=task.source_inbound_seq,
                parent_task_id=task.task_id,
            )
            return

        if resume_stack and target == str(resume_stack[-1].get("node_id") or ""):
            frame = resume_stack[-1]
            rest = resume_stack[:-1]
            self._create_resume_task_from_frame_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                frame=frame,
                resume_stack=rest,
                resume_data={
                    "type": "handoff_result",
                    "child_node_id": str(task.node_id or ""),
                    "child_outcome": outcome,
                    "summary": summary,
                    "text": text,
                    "attachments": result_attachments or [],
                },
                parent_task_id=task.task_id,
            )
            return

        if target == "$reply":
            if text or summary or result_attachments:
                self.append_outbound_message(
                    session_id=task.session_id,
                    text=text or summary,
                    attachments=result_attachments,
                    source_inbound_seq=task.source_inbound_seq,
                )
            return

        if target == "$end" or not target:
            if resume_stack:
                frame = resume_stack[-1]
                rest = resume_stack[:-1]
                self._create_resume_task_from_frame_locked(
                    session_id=task.session_id,
                    session_generation=task.session_generation,
                    frame=frame,
                    resume_stack=rest,
                    resume_data={
                        "type": "handoff_result",
                        "child_node_id": str(task.node_id or ""),
                        "child_outcome": outcome,
                        "summary": summary,
                        "text": text,
                        "attachments": result_attachments or [],
                    },
                    parent_task_id=task.task_id,
                )
            elif text or summary or result_attachments:
                self.append_outbound_message(
                    session_id=task.session_id,
                    text=text or summary,
                    attachments=result_attachments,
                    source_inbound_seq=task.source_inbound_seq,
                )
            return

        self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            workflow_id=task.workflow_id,
            kind=TaskKind.node,
            node_id=target,
            input_data={
                "instruction": str(result.get("instruction") or text or task.input.get("instruction") or "").strip(),
                "context_ref": "",
                "resume_data": {},
                "workflow_id": task.workflow_id,
            },
            continuation={"resume_stack": resume_stack},
            source_inbound_seq=task.source_inbound_seq,
            parent_task_id=task.task_id,
        )

    def _route_tool_completion_locked(self, task: Task) -> None:
        batch_id = str(task.input.get("batch_id") or task.continuation.get("batch_id") or "").strip()
        if not batch_id:
            return

        siblings: list[Task] = []
        for t in self.tasks.values():
            if t.session_id != task.session_id or t.session_generation != task.session_generation or t.kind != TaskKind.tool:
                continue
            tb = str(t.input.get("batch_id") or t.continuation.get("batch_id") or "").strip()
            if tb == batch_id:
                siblings.append(t)

        if not siblings:
            return
        if any(not self._task_terminal(t) for t in siblings):
            return

        resume_key = f"tool_batch:{batch_id}"
        for t in self.tasks.values():
            if t.session_id != task.session_id or t.session_generation != task.session_generation or t.kind != TaskKind.node:
                continue
            if str(t.input.get("resume_key") or "") == resume_key:
                return

        siblings.sort(key=lambda x: int(x.input.get("tool_index", 0) or 0))
        entries: list[dict[str, Any]] = []
        for t in siblings:
            entries.append({
                "name": str(t.tool_name or t.result.get("tool_name") or ""),
                "args": dict(t.input.get("arguments") or {}),
                "format": str(t.result.get("raw_format") or "json"),
                "raw_inline": str(t.result.get("raw_inline") or ""),
                "truncated": bool(t.result.get("truncated", False)),
                "ref": str(t.result.get("ref") or ""),
                "summary": str(t.result.get("summary") or ""),
                "attachments": list(t.result.get("attachments") or []) if isinstance(t.result.get("attachments"), list) else [],
            })

        resume_context_ref = str(task.continuation.get("resume_context_ref") or "").strip()
        resume_node_id = str(task.continuation.get("resume_node_id") or "").strip()
        resume_instruction = str(task.continuation.get("resume_instruction") or "").strip()
        resume_stack = list(task.continuation.get("resume_stack") or [])
        source_inbound_seq = self._to_positive_int(task.continuation.get("source_inbound_seq"))

        self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            workflow_id=task.workflow_id,
            kind=TaskKind.node,
            node_id=resume_node_id,
            input_data={
                "instruction": resume_instruction,
                "context_ref": resume_context_ref,
                "resume_key": resume_key,
                "resume_data": {"type": "tool_results", "tool_results": entries},
                "workflow_id": task.workflow_id,
            },
            continuation={"resume_stack": resume_stack},
            source_inbound_seq=source_inbound_seq,
            parent_task_id=task.parent_task_id or task.task_id,
        )

    def get_or_create_session(self, *, channel: str, conversation_key: str) -> str:
        with self._lock:
            if conversation_key in self.conversation_map:
                return self.conversation_map[conversation_key]

            session_id = str(uuid.uuid4())
            created_at = _now()

            self.sessions[session_id] = SessionInfo(
                session_id=session_id,
                channel=channel,
                conversation_key=conversation_key,
                created_at=created_at,
                updated_at=created_at,
            )
            self.conversation_map[conversation_key] = session_id

            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="session_created",
                payload={
                    "session_id": session_id,
                    "channel": channel,
                    "conversation_key": conversation_key,
                    "created_at": created_at.isoformat(),
                },
            )
            return session_id

    def append_outbound_message(
        self,
        *,
        session_id: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source_inbound_seq: int | None = None,
    ) -> dict[str, Any]:
        text_clean = str(text or "").strip()
        if not text_clean and not attachments:
            raise ValueError("empty text and no attachments")

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                v = int(source_inbound_seq)
            except Exception:
                v = 0
            if v > 0:
                src_seq = v

        with self._lock:
            if session_id not in self.sessions:
                raise KeyError("session not found")

            if src_seq is not None:
                inbound_evt = self._inbound_events.get(src_seq)
                if not isinstance(inbound_evt, dict):
                    raise ValueError(f"source_inbound_seq not found: {src_seq}")
                if str(inbound_evt.get("session_id") or "") != session_id:
                    raise ValueError("source_inbound_seq session mismatch")

                existing = self._inbound_routed.get(src_seq)
                if isinstance(existing, dict) and existing.get("action") == "reply":
                    return {"ok": True, "deduped": True, "route": existing}

            payload: dict[str, Any] = {"text": text_clean}
            if attachments:
                payload["attachments"] = list(attachments)
            if src_seq is not None:
                payload["source_inbound_seq"] = src_seq

            evt = self.eventlog.append(
                session_id=session_id,
                component="shell",
                type_="outbound_message",
                payload=payload,
            )

            if src_seq is not None and src_seq not in self._inbound_routed:
                self._inbound_routed[src_seq] = {
                    "action": "reply",
                    "session_id": session_id,
                    "event_seq": int(evt.get("seq", 0) or 0),
                }

            return {"ok": True, "deduped": False, "event_seq": int(evt.get("seq", 0) or 0)}

    def create_approval(self, *, session_id: str, operation: str, details: dict[str, Any]) -> Approval:
        now = _now()
        fingerprint_src = json.dumps({"operation": operation, "details": details}, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:12]

        approval = Approval(
            approval_id=str(uuid.uuid4()),
            session_id=session_id,
            operation=operation,
            details=details,
            status=ApprovalStatus.pending,
            fingerprint=fingerprint,
            requested_at=now,
            decided_at=None,
            decision=None,
            comment=None,
        )

        with self._lock:
            self.approvals[approval.approval_id] = approval

        self.eventlog.append(
            session_id=session_id,
            component="supervisor",
            type_="approval_requested",
            payload=approval.model_dump(mode="json"),
        )
        return approval

    def decide_approval(self, *, approval_id: str, decision: str, comment: str | None = None) -> Approval | None:
        with self._lock:
            if approval_id not in self.approvals:
                return None
            a = self.approvals[approval_id]
            a.decision = "allow" if decision == "allow" else "deny"
            a.status = ApprovalStatus.allowed if a.decision == "allow" else ApprovalStatus.denied
            a.comment = comment
            a.decided_at = _now()

        self.eventlog.append(
            session_id=a.session_id,
            component="supervisor",
            type_="approval_decided",
            payload={
                "approval_id": approval_id,
                "session_id": a.session_id,
                "decision": a.decision,
                "comment": comment,
                "ts": _now().isoformat(),
            },
        )
        return a

    def request_operation(self, *, session_id: str, op: str, parameters: dict[str, Any]) -> OpRequestOut:
        decision = self.policy.evaluate(op=op, parameters=parameters)
        if decision.safety_level == SafetyLevel.deny:
            return OpRequestOut(safety_level=SafetyLevel.deny, reason=decision.reason, approval_id=None)
        if decision.safety_level == SafetyLevel.auto:
            return OpRequestOut(safety_level=SafetyLevel.auto, reason=decision.reason, approval_id=None)
        approval = self.create_approval(session_id=session_id, operation=op, details=parameters)
        return OpRequestOut(safety_level=SafetyLevel.approval_required, reason=decision.reason, approval_id=approval.approval_id)

    def list_events(self, *, session_id: str, after_seq: int) -> list[dict[str, Any]]:
        return self.eventlog.list_events(session_id=session_id, after_seq=after_seq)

    def session_messages(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        tool_records: list[str] = []
        for e in self.eventlog.events:
            if e.get("session_id") != session_id:
                continue
            et = e.get("type")
            payload = e.get("payload") or {}
            if et == "inbound_message":
                tool_records.clear()
                text = payload.get("text")
                if isinstance(text, str):
                    inbound_atts = payload.get("attachments")
                    if isinstance(inbound_atts, list) and inbound_atts:
                        from engine.attachments import build_multimodal_content
                        msgs.append({"role": "user", "content": build_multimodal_content(text, inbound_atts)})
                    else:
                        msgs.append({"role": "user", "content": text})
            elif et == "handoff_progress":
                prog_msg = str(payload.get("message") or "")
                if prog_msg and "[tool]" in prog_msg:
                    tool_records.append(prog_msg)
            elif et == "outbound_message":
                text = payload.get("text")
                outbound_atts = payload.get("attachments")
                if isinstance(text, str) or (isinstance(outbound_atts, list) and outbound_atts):
                    text_str = str(text or "")
                    if tool_records:
                        prefix = "[实际执行的工具: " + "; ".join(tool_records) + "]\n"
                    else:
                        prefix = "[未调用任何工具，直接生成文本]\n"
                    if isinstance(outbound_atts, list) and outbound_atts:
                        from engine.attachments import build_multimodal_content
                        msgs.append({"role": "assistant", "content": build_multimodal_content(prefix + text_str, outbound_atts)})
                    else:
                        msgs.append({"role": "assistant", "content": prefix + text_str})
                    tool_records.clear()
        if limit > 0:
            msgs = msgs[-limit:]
        return msgs

    def admin_state(self) -> AdminStateOut:
        approval_counts = {s.value: 0 for s in ApprovalStatus}
        pending: list[Approval] = []
        for a in self.approvals.values():
            approval_counts[a.status.value] = approval_counts.get(a.status.value, 0) + 1
            if a.status == ApprovalStatus.pending:
                pending.append(a)

        task_counts = {s.value: 0 for s in TaskStatus}
        for t in self.tasks.values():
            task_counts[t.status.value] = task_counts.get(t.status.value, 0) + 1

        engine_last_seen_at, engine_worker_id = self.engine_seen_snapshot()

        return AdminStateOut(
            sessions=len(self.sessions),
            approvals=approval_counts,
            tasks=task_counts,
            pending_approvals=pending,
            engine_runtime={
                "worker_id": engine_worker_id,
                "last_seen_at": engine_last_seen_at,
                "workers": sorted({t.worker_id for t in self.tasks.values() if t.worker_id}),
            },
        )

    def write_boot_event(self) -> dict[str, Any]:
        prev = self.eventlog.last_boot_run_id()
        payload = {
            "previous_run_id": prev,
            "restarted": prev is not None,
            "ts": _now().isoformat(),
        }
        return self.eventlog.append(session_id=SYSTEM_SESSION_ID, component="supervisor", type_="boot", payload=payload)
