from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


from .eventlog import EventLog, SYSTEM_SESSION_ID
from .policy import PolicyEngine
from .types import (
    AdminStateOut,
    Approval,
    ApprovalStatus,
    OpRequestOut,
    SafetyLevel,
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
    """Supervisor 核心状态。管理会话、inbound 队列、审批。"""

    def __init__(self, *, eventlog: EventLog, policy: PolicyEngine) -> None:
        self.eventlog = eventlog
        self.policy = policy
        self.started_at = _now()

        self._lock = threading.Lock()

        self.sessions: dict[str, SessionInfo] = {}
        self.conversation_map: dict[str, str] = {}  # conversation_key -> session_id

        self.approvals: dict[str, Approval] = {}

        self._engine_last_seen_at: datetime | None = None
        self._engine_last_worker_id: str | None = None

        self._inbound_order: list[int] = []
        self._inbound_events: dict[int, dict[str, Any]] = {}  # inbound_seq -> {session_id, payload}
        self._inbound_processed: set[int] = set()
        self._inbound_routed: dict[int, dict[str, Any]] = {}
        self._inbound_cursor: int = 0

        @dataclass
        class _InboundLease:
            worker_id: str
            expires_at: datetime

        self._InboundLease = _InboundLease
        self._inbound_leases: dict[int, _InboundLease] = {}

        self.rebuild_from_events(eventlog.events)

    # ---- 引擎心跳 ----

    def mark_engine_seen(self, *, worker_id: str) -> None:
        with self._lock:
            self._engine_last_seen_at = _now()
            self._engine_last_worker_id = worker_id

    def engine_seen_snapshot(self) -> tuple[datetime | None, str | None]:
        with self._lock:
            return self._engine_last_seen_at, self._engine_last_worker_id

    # ---- 事件回放 ----

    def rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            et = e.get("type")
            session_id = e.get("session_id")
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
                self._apply_inbound_message(seq=seq, session_id=str(session_id or ""), payload=payload)
            elif et == "inbound_processed":
                self._apply_inbound_processed(payload)
            elif et == "outbound_message":
                self._apply_outbound_message(seq=seq, session_id=str(session_id or ""), payload=payload)
            elif et == "approval_requested":
                self._apply_approval_requested(payload)
            elif et == "approval_decided":
                self._apply_approval_decided(payload)

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
        if not isinstance(seq, int) or seq <= 0:
            return
        if not session_id:
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
            seq = inbound_seq
            self._inbound_processed.add(seq)

    def _apply_outbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        """记录 outbound，用于 inbound 去重。"""
        if not session_id or not isinstance(payload, dict):
            return

        src = payload.get("source_inbound_seq")
        try:
            inbound_seq = int(src) if src is not None else 0
        except Exception:
            inbound_seq = 0

        if inbound_seq > 0:
            evt = self._inbound_events.get(inbound_seq)
            if isinstance(evt, dict):
                sid = str(evt.get("session_id") or "")
                if sid and sid != session_id:
                    return
            if inbound_seq not in self._inbound_routed:
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
        source_inbound_seq: int | None = None,
    ) -> dict[str, Any]:
        text_clean = str(text or "").strip()
        if not text_clean:
            raise ValueError("empty text")

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
        for e in self.eventlog.events:
            if e.get("session_id") != session_id:
                continue
            et = e.get("type")
            payload = e.get("payload") or {}
            if et == "inbound_message":
                text = payload.get("text")
                if isinstance(text, str):
                    msgs.append({"role": "user", "content": text})
            elif et == "outbound_message":
                text = payload.get("text")
                if isinstance(text, str):
                    msgs.append({"role": "assistant", "content": text})
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

        engine_last_seen_at, engine_worker_id = self.engine_seen_snapshot()

        return AdminStateOut(
            sessions=len(self.sessions),
            approvals=approval_counts,
            pending_approvals=pending,
            engine_runtime={
                "worker_id": engine_worker_id,
                "last_seen_at": engine_last_seen_at,
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
