from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from .config_store import ConfigStore
from .process_manager import ProcessManager
from .state import SupervisorState
from .types import (
    AdminStateOut,
    AppConfigPublic,
    Approval,
    ApprovalDecisionIn,
    ApprovalRequestIn,
    ConfigReloadOut,
    CreateTaskIn,
    Event,
    HealthOut,
    InboundMessageIn,
    InboundMessageOut,
    InboundWorkItem,
    InboundAckIn,
    InboundAckOut,
    OutboundMessageIn,
    OutboundMessageOut,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
    OpRequestIn,
    OpRequestOut,
    RestartIn,
    RestartOut,
    Task,
    TaskCompleteIn,
    TaskEventIn,
    TaskStatus,
)

from .upgrade import create_upgrade_marker


def _now() -> datetime:
    return datetime.now(timezone.utc)
def create_app(
    *,
    state: SupervisorState,
    process_manager: ProcessManager | None,
    config_store: ConfigStore,
) -> FastAPI:
    app = FastAPI(title="Clonoth Supervisor", version="0.1.0")
    app.state.state = state
    app.state.process_manager = process_manager
    app.state.config_store = config_store

    @app.get("/v1/health", response_model=HealthOut)
    async def health() -> HealthOut:
        st: SupervisorState = app.state.state
        uptime = (_now() - st.started_at).total_seconds()
        return HealthOut(run_id=st.eventlog.run_id, started_at=st.started_at, uptime_seconds=uptime)

    # ----------------------------
    # Config APIs (YAML-backed)
    # ----------------------------

    @app.get("/v1/config", response_model=AppConfigPublic)
    async def get_config() -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_public()

    @app.get("/v1/config/openai", response_model=OpenAIConfigPublic)
    async def get_openai_config_public() -> OpenAIConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_public()

    @app.get("/v1/config/openai/secret", response_model=OpenAIConfigSecret)
    async def get_openai_config_secret() -> OpenAIConfigSecret:
        # 注意：当前 MVP 仅绑定 127.0.0.1；此接口会返回 api_key。
        # 后续如需更强安全，可加入 internal token。
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn) -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        out = cs.update_openai(body)

        # 记录配置更新事件（不写入明文 api_key）
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_updated",
            payload={
                "provider": out.provider,
                "openai": out.openai.model_dump(mode="json"),
                "ts": _now().isoformat(),
            },
        )
        return out

    @app.post("/v1/config/reload", response_model=ConfigReloadOut)
    async def reload_config() -> ConfigReloadOut:
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        cs.reload()
        out = cs.get_public()
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_reloaded",
            payload={"ts": _now().isoformat()},
        )
        return ConfigReloadOut(ok=True, config=out)

    @app.post("/v1/inbound", response_model=InboundMessageOut)
    async def inbound(msg: InboundMessageIn) -> InboundMessageOut:
        st: SupervisorState = app.state.state
        session_id = st.get_or_create_session(channel=msg.channel, conversation_key=msg.conversation_key)

        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_="inbound_message",
            payload=msg.model_dump(),
        )

        # update in-memory inbound queue for orchestrator worker
        st.record_inbound_message_event(evt)

        return InboundMessageOut(session_id=session_id, accepted=True)

    @app.get("/v1/inbound/next", response_model=InboundWorkItem)
    async def inbound_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(30.0, ge=1.0, le=600.0),
    ) -> InboundWorkItem:
        st: SupervisorState = app.state.state
        item = st.assign_next_inbound(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return InboundWorkItem.model_validate(item)

    @app.post("/v1/inbound/{inbound_seq}/ack", response_model=InboundAckOut)
    async def inbound_ack(inbound_seq: int, body: InboundAckIn) -> InboundAckOut:
        st: SupervisorState = app.state.state
        ok = st.ack_inbound(inbound_seq=int(inbound_seq), worker_id=body.worker_id)
        if not ok:
            raise HTTPException(status_code=404, detail="inbound item not found")
        return InboundAckOut(ok=True)

    @app.post("/v1/sessions/{session_id}/outbound", response_model=OutboundMessageOut)
    async def session_outbound(session_id: str, body: OutboundMessageIn) -> OutboundMessageOut:
        st: SupervisorState = app.state.state
        try:
            st.append_outbound_message(
                session_id=session_id,
                text=str(body.text or ""),
                source_inbound_seq=body.source_inbound_seq,
                source_task_id=body.source_task_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            # route conflict (same inbound already routed to task)
            raise HTTPException(status_code=409, detail=str(e) or "conflict")

        return OutboundMessageOut(ok=True)

    @app.get("/v1/sessions/{session_id}/events", response_model=list[Event])
    async def session_events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)

        # Event.ts 在 log 中是 ISO string，这里转为 datetime 给 Pydantic
        out: list[Event] = []
        for e in evts:
            try:
                out.append(
                    Event(
                        schema_version=int(e.get("schema_version", 1)),
                        seq=int(e.get("seq", 0)),
                        event_id=str(e.get("event_id")),
                        ts=datetime.fromisoformat(str(e.get("ts"))),
                        run_id=str(e.get("run_id")),
                        session_id=str(e.get("session_id")),
                        component=str(e.get("component")),
                        type=str(e.get("type")),
                        payload=dict(e.get("payload") or {}),
                    )
                )
            except Exception:
                continue
        return out

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.post("/v1/tasks", response_model=Task)
    async def create_task(inp: CreateTaskIn) -> Task:
        st: SupervisorState = app.state.state
        try:
            return st.create_task(
                session_id=inp.session_id,
                instruction=inp.instruction,
                workflow_id=inp.workflow_id,
                priority=inp.priority,
                context=inp.context,
                source_inbound_seq=inp.source_inbound_seq,
                use_context=inp.use_context,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e) or "conflict")

    @app.get("/v1/tasks/next", response_model=Task)
    async def next_task(worker_id: str = Query(..., min_length=1)) -> Task:
        st: SupervisorState = app.state.state
        # kernel worker heartbeat (best-effort)
        st.mark_kernel_seen(worker_id=worker_id)
        t = st.assign_next_task(worker_id=worker_id)
        if t is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return t

    @app.get("/v1/task_results/next", response_model=Task)
    async def next_task_result(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(30.0, ge=1.0, le=600.0),
    ) -> Task:
        """Get a completed task that needs Shell post-processing.

        Shell will call LLM to generate the final user-facing reply, then append an
        outbound_message with source_task_id.
        """

        st: SupervisorState = app.state.state
        t = st.assign_next_task_result(worker_id=worker_id, lease_sec=float(lease_sec))
        if t is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return t

    @app.post("/v1/tasks/{task_id}/events")
    async def task_event(task_id: str, ev: TaskEventIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        if task_id not in st.tasks:
            raise HTTPException(status_code=404, detail="task not found")

        t = st.tasks[task_id]
        evt = st.eventlog.append(
            session_id=t.session_id,
            component="kernel",
            type_=ev.type,
            payload={"task_id": task_id, **(ev.payload or {})},
        )

        # Update derived state for task reply dedup (Kernel may emit outbound_message here).
        if ev.type == "outbound_message":
            st.record_outbound_message_event(evt)
        return {"ok": True}

    @app.post("/v1/tasks/{task_id}/complete", response_model=Task)
    async def task_complete(task_id: str, body: TaskCompleteIn) -> Task:
        st: SupervisorState = app.state.state
        t = st.complete_task(task_id=task_id, status=body.status, result=body.result)
        if t is None:
            raise HTTPException(status_code=404, detail="task not found")
        return t

    @app.post("/v1/approvals/request", response_model=Approval)
    async def approval_request(inp: ApprovalRequestIn) -> Approval:
        st: SupervisorState = app.state.state
        return st.create_approval(session_id=inp.session_id, operation=inp.operation, details=inp.details)

    @app.get("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_get(approval_id: str) -> Approval:
        st: SupervisorState = app.state.state
        if approval_id not in st.approvals:
            raise HTTPException(status_code=404, detail="approval not found")
        return st.approvals[approval_id]

    @app.post("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_decide(approval_id: str, body: ApprovalDecisionIn) -> Approval:
        st: SupervisorState = app.state.state
        a = st.decide_approval(approval_id=approval_id, decision=body.decision, comment=body.comment)
        if a is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return a

    @app.post("/v1/ops/request", response_model=OpRequestOut)
    async def ops_request(inp: OpRequestIn) -> OpRequestOut:
        st: SupervisorState = app.state.state
        return st.request_operation(session_id=inp.session_id, op=inp.op, parameters=inp.parameters)

    @app.get("/v1/admin/state", response_model=AdminStateOut)
    async def admin_state() -> AdminStateOut:
        st: SupervisorState = app.state.state
        return st.admin_state()

    @app.post("/v1/admin/restart", response_model=RestartOut)
    async def admin_restart(inp: RestartIn) -> RestartOut:
        pm: ProcessManager | None = app.state.process_manager
        st: SupervisorState = app.state.state

        workspace_root = Path(__file__).resolve().parents[1]

        # 记录事件（即使 pm 不存在也能追踪）
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="restart_requested",
            payload={
                "target": inp.target,
                "reason": inp.reason,
                "approval_id": inp.approval_id,
                "ts": _now().isoformat(),
            },
        )

        if inp.target in {"shell", "kernel"}:
            if pm is None:
                raise HTTPException(status_code=409, detail="process manager not enabled")

            # If this restart is part of a self-evolution approval flow, create an upgrade marker
            # so the watchdog can auto-rollback if the new version fails.
            create_upgrade_marker(
                workspace_root=workspace_root,
                state=st,
                process_manager=pm,
                target=inp.target,
                reason=inp.reason,
                approval_id=inp.approval_id,
            )

            if inp.target == "shell":
                pm.restart_shell()
            else:
                pm.restart_kernel()

            st.eventlog.append(
                session_id="__system__",
                component="supervisor",
                type_="restart_completed",
                payload={"target": inp.target, "ts": _now().isoformat()},
            )
            return RestartOut(scheduled=True, target=inp.target)

        # all: stop children then execv self
        create_upgrade_marker(
            workspace_root=workspace_root,
            state=st,
            process_manager=pm,
            target="all",
            reason=inp.reason,
            approval_id=inp.approval_id,
        )
        def _do_execv() -> None:
            try:
                if pm is not None:
                    pm.stop_all()
            finally:
                time.sleep(0.2)
                os.execv(sys.executable, [sys.executable, *sys.argv])

        th = threading.Thread(target=_do_execv, daemon=True)
        th.start()
        return RestartOut(scheduled=True, target="all")

    return app
