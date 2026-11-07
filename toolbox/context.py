from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


# [Audit A 2026-10] Why: the approval poll treated every transport error and every
# 5xx as "still pending" with no bound, so a permanently broken Supervisor could
# hold a worker in wait_for_approval forever. How: bound only the *consecutive*
# run of polls that produced no usable answer; a real "pending" reply (an actual
# human approval dialog) resets the streak. Purpose: an outage ends in a
# structured approval_unavailable after 120s, while normal human approval latency
# stays unbounded exactly as before.
_APPROVAL_FAILURE_BUDGET_SEC = 120.0
_APPROVAL_POLL_TIMEOUT_SEC = 5.0
_CANCEL_CHECK_TIMEOUT_SEC = 2.0
_RENEW_INTERVAL = 30.0  # renew task lease every 30s during approval wait
_RENEW_LEASE_SEC = 300.0  # request 5-minute lease to survive long waits

# Approval statuses that mean "the Supervisor could not deliver a decision".
# They are infrastructure failures and must never be reported as a human denial.
APPROVAL_FAILURE_STATUSES = ("approval_unavailable", "approval_lost", "approval_error")


def _monotonic() -> float:
    """Clock for the approval budget (replaced by a fake clock in tests)."""
    return time.monotonic()


@dataclass
class ToolContext:
    supervisor_url: str
    session_id: str
    run_id: str
    worker_id: str
    workspace_root: Path
    http: httpx.AsyncClient
    registry: Any
    task_id: str = ""
    session_generation: int = 0
    # [Fork/Merge 2026-05-17] Why: tools can execute inside an entry branch while
    # user-visible supervisor APIs are keyed by the parent session. How: carry the
    # parent separately and let route_session_id() choose it when present. Purpose:
    # approvals, tool events, and session-scoped tools do not target a temporary branch.
    parent_session_id: str = ""
    conversation_key: str = ""
    approval_poll_interval_sec: float = 0.5
    # [Audit A 2026-10] Consecutive-failure budget for approval polling. Counts
    # only polls without a usable Supervisor answer; a pending approval does not
    # consume it, so ordinary human approval waits are unaffected.
    approval_failure_budget_sec: float = _APPROVAL_FAILURE_BUDGET_SEC
    # [AutoC 2026-05-31] Why: request_guard runs inside a ToolContext after the
    # engine has selected a concrete tool call. How: carry the active provider
    # tool_call_id and node_id on the context. Purpose: policy approvals can be
    # merged into the matching ToolCallCard.
    tool_call_id: str = ""
    node_id: str = ""
    # [AutoC 2026-08-10] Session-level working directory for file/command tools.
    # Why: workspace_root is the Clonoth install dir (config, data, nodes);
    # workspace is the project dir the AI operates on (cwd, path resolution).
    # Defaults to workspace_root when unset. AI can change it via set_workspace.
    workspace: Path | None = None
    workspace_name: str = ""

    @property
    def effective_workspace(self) -> Path:
        """Return workspace if set, else workspace_root."""
        return self.workspace if self.workspace is not None else self.workspace_root

    def route_session_id(self) -> str:
        """Return the user-visible session for supervisor API calls."""
        # [Fork/Merge 2026-05-17] Why: session_id remains the runtime storage
        # session and may be branch_xxx. How: prefer parent_session_id and fall
        # back to session_id for old tasks and ordinary child tools. Purpose:
        # route outward-facing tool side effects to the durable conversation.
        return (self.parent_session_id or self.session_id or "").strip()

    async def emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        route_session_id = self.route_session_id()
        if self.parent_session_id and self.parent_session_id != self.session_id:
            payload.setdefault("parent_session_id", self.parent_session_id)
            payload.setdefault("branch_session_id", self.session_id)
        await self.http.post(
            f"{self.supervisor_url}/v1/sessions/{route_session_id}/events",
            json={"type": type_, "payload": payload},
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        route_session_id = self.route_session_id()
        r = await self.http.post(
            f"{self.supervisor_url}/v1/ops/request",
            json={
                "session_id": route_session_id,
                "op": op,
                "parameters": parameters,
                # [AutoC 2026-05-31] Why: approval_requested events need the tool
                # execution identity. How: send optional context fields only known
                # at runtime. Purpose: the supervisor can attach approval state to
                # the active tool card while preserving legacy empty values.
                "tool_call_id": self.tool_call_id or "",
                "node_id": self.node_id or getattr(self, "_node_id", "") or "",
                "task_id": self.task_id or "",
                "workspace": str(self.workspace) if self.workspace else None,
            },
        )
        r.raise_for_status()
        return r.json()

    async def _poll_cancelled(self) -> bool | None:
        """Ask the Supervisor whether this task/session is cancelled.

        Returns True (cancelled), False (running), or None when no answer could
        be obtained (transport error, timeout, unusable response).

        [Audit A 2026-10] Why: the old check swallowed every connection error and
        reported False, so an outage was indistinguishable from "not cancelled".
        How: keep the tri-state result internal; check_cancelled() still maps
        "unknown" to False for the existing polling callers, while
        wait_for_approval() can count an unverifiable poll as a failure. Purpose:
        an unchecked cancel is never mistaken for a verified running state, and
        it can no longer keep the approval wait alive forever.
        """
        # [硬取消] 显式 2s 超时，防止 supervisor 无响应时拖延 cancel 检测。
        # 工具层的 cancel 轮询间隔为 0.2s（execute_command / script tool），
        # 如果 HTTP 请求无超时，单次 check 可能阻塞远超轮询间隔。
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled",
                    timeout=_CANCEL_CHECK_TIMEOUT_SEC,
                )
            else:
                # [Fork/Merge 2026-05-17] Why: session-level cancellation is a
                # user-visible operation. How: query the parent route session when
                # present. Purpose: tools without task_id still honor parent cancel.
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.route_session_id()}/cancelled",
                    timeout=_CANCEL_CHECK_TIMEOUT_SEC,
                )
            if r.status_code == 200:
                return bool(r.json().get("cancelled", False))
        except Exception:
            return None
        return None

    async def check_cancelled(self) -> bool:
        """Return True only when the Supervisor confirms cancellation."""
        return (await self._poll_cancelled()) is True

    async def _poll_approval(
        self, approval_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """One approval poll. Returns ``(approval, terminal_error, failure_text)``.

        ``approval`` is the Supervisor payload when it answered with a decision or
        pending state; ``terminal_error`` is a structured, non-retryable payload;
        ``failure_text`` describes a retryable failure (transport, 5xx, bad body)
        that feeds the consecutive-failure budget.
        """
        try:
            r = await self.http.get(
                f"{self.supervisor_url}/v1/approvals/{approval_id}",
                timeout=_APPROVAL_POLL_TIMEOUT_SEC,
            )
        except Exception as exc:
            return None, None, f"{type(exc).__name__}: {exc}".strip()

        status = int(getattr(r, "status_code", 0) or 0)
        if status == 404:
            # [Audit A] Why: a vanished approval used to escape as a bare
            # HTTPStatusError from the tool call. How: report the terminal state.
            # Purpose: callers can tell "approval no longer exists" apart from a
            # denial and from an outage.
            return None, {
                "approval_id": approval_id,
                "status": "approval_lost",
                "http_status": 404,
                "error": f"approval {approval_id} no longer exists (HTTP 404)",
            }, ""
        if 400 <= status < 500:
            # [Audit A] Other 4xx are just as non-retryable: fail fast with an
            # explicit error instead of raising a naked HTTPStatusError.
            return None, {
                "approval_id": approval_id,
                "status": "approval_error",
                "http_status": status,
                "error": f"approval {approval_id} poll rejected (HTTP {status})",
            }, ""
        if not 200 <= status < 300:
            return None, None, f"HTTP {status}"

        try:
            payload = r.json()
        except Exception as exc:
            return None, None, f"invalid approval payload: {type(exc).__name__}"
        if not isinstance(payload, dict):
            return None, None, f"unexpected approval payload: {type(payload).__name__}"
        if payload.get("status") not in ("pending", "allowed", "denied", "cancelled"):
            return None, None, "invalid approval status"
        return payload, None, ""

    async def _approval_failure_budget(self) -> float:
        try:
            budget = float(getattr(self, "approval_failure_budget_sec", _APPROVAL_FAILURE_BUDGET_SEC))
        except (TypeError, ValueError):
            budget = _APPROVAL_FAILURE_BUDGET_SEC
        return budget if budget > 0 else _APPROVAL_FAILURE_BUDGET_SEC

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        """Wait for one approval decision.

        Returns the Supervisor payload once a decision exists, or a structured
        terminal payload:

        - ``{"status": "cancelled"}``           the task/session was cancelled
        - ``{"status": "approval_lost"}``       HTTP 404: approval no longer exists
        - ``{"status": "approval_error"}``      other 4xx: the poll was rejected
        - ``{"status": "approval_unavailable"}`` no usable answer for the whole
          consecutive-failure budget (Supervisor down / hanging)

        [Audit A 2026-10] Why: transport errors and 5xx were retried forever and
        the only escape was a decision that never came. How: keep polling through
        transient failures, but abort once a *consecutive* run of failures has
        lasted ``approval_failure_budget_sec``; any usable answer (including
        ``pending``) resets that run. Purpose: a broken Supervisor cannot hang a
        worker, while a slow human approver is still waited on indefinitely.
        """
        interval = float(poll_interval) if poll_interval is not None else float(self.approval_poll_interval_sec or 0.5)
        if interval <= 0:
            interval = 0.5
        budget = await self._approval_failure_budget()
        _last_renew = 0.0  # monotonic timestamp of last lease renewal
        _failure_since: float | None = None  # start of the current failure streak
        _last_failure = ""
        while True:
            _now_mono = _monotonic()
            if await self._poll_cancelled() is True:
                return {"approval_id": approval_id, "status": "cancelled"}

            approval, terminal, failure = await self._poll_approval(approval_id)
            if terminal is not None:
                return terminal
            if approval is not None:
                _failure_since = None  # the Supervisor answered: budget resets
                if approval.get("status") != "pending":
                    return approval
            else:
                if _failure_since is None:
                    _failure_since = _now_mono
                _last_failure = failure or _last_failure
                _failed_for = _now_mono - _failure_since
                if _failed_for >= budget:
                    # [Audit A] Structured outage, never a fake user denial.
                    return {
                        "approval_id": approval_id,
                        "status": "approval_unavailable",
                        "error": (
                            f"approval {approval_id} unavailable: no Supervisor response "
                            f"for {_failed_for:.0f}s ({_last_failure or 'no detail'})"
                        ),
                        "failure_budget_sec": budget,
                        "failed_for_sec": round(_failed_for, 3),
                    }

            # Renew task lease during approval wait to prevent zombie reaper timeout.
            # This is defense-in-depth alongside the heartbeat in engine/runner.py.
            if self.task_id and _now_mono - _last_renew >= _RENEW_INTERVAL:
                try:
                    await self.http.post(
                        f"{self.supervisor_url}/v1/tasks/{self.task_id}/renew_lease",
                        json={"worker_id": self.worker_id, "lease_sec": _RENEW_LEASE_SEC},
                        timeout=_APPROVAL_POLL_TIMEOUT_SEC,
                    )
                except Exception:
                    pass  # best-effort; heartbeat is the primary renewal mechanism
                _last_renew = _now_mono
            await asyncio.sleep(interval)
