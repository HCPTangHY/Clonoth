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
    Event,
    HandoffEventIn,
    HealthOut,
    InboundAckIn,
    InboundAckOut,
    InboundMessageIn,
    InboundMessageOut,
    InboundWorkItem,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
    OpRequestIn,
    OpRequestOut,
    OutboundMessageIn,
    OutboundMessageOut,
    RestartIn,
    RestartOut,
)


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
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn) -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        out = cs.update_openai(body)

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

        st.record_inbound_message_event(evt)

        return InboundMessageOut(session_id=session_id, accepted=True)

    @app.get("/v1/inbound/next", response_model=InboundWorkItem)
    async def inbound_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(30.0, ge=1.0, le=600.0),
    ) -> InboundWorkItem:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
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
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e) or "conflict")

        return OutboundMessageOut(ok=True)

    @app.get("/v1/sessions/{session_id}/events", response_model=list[Event])

    @app.get("/v1/events", response_model=list[Event])
    async def global_events(
        after_seq: int = Query(0, ge=0),
        types: str = Query("", description="comma-separated event types to filter"),
    ) -> list[Event]:
        """全局事件接口。返回所有 session 中 seq > after_seq 的事件。"""
        st: SupervisorState = app.state.state
        evts = st.eventlog.list_all_events(after_seq=after_seq)
        type_filter = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        out: list[Event] = []
        for e in evts:
            if type_filter and str(e.get("type")) not in type_filter:
                continue
            try:
                out.append(Event(
                    schema_version=int(e.get("schema_version", 1)),
                    seq=int(e.get("seq", 0)),
                    event_id=str(e.get("event_id")),
                    ts=datetime.fromisoformat(str(e.get("ts"))),
                    run_id=str(e.get("run_id")),
                    session_id=str(e.get("session_id")),
                    component=str(e.get("component")),
                    type=str(e.get("type")),
                    payload=dict(e.get("payload") or {}),
                ))
            except Exception:
                continue
        return out

    async def session_events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)

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

    @app.post("/v1/sessions/{session_id}/events")
    async def session_event(session_id: str, ev: HandoffEventIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        transient = ev.type in {"stream_delta", "stream_end"}
        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_=ev.type,
            payload=dict(ev.payload or {}),
            transient=transient,
        )
        if ev.type == "outbound_message":
            st.record_outbound_message_event(evt)
        return {"ok": True}

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.post("/v1/sessions/{session_id}/cancel")
    async def session_cancel(session_id: str) -> dict[str, Any]:
        """取消 session 中正在执行的任务。"""
        st: SupervisorState = app.state.state
        ok = st.cancel_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/cancelled")
    async def session_cancelled(session_id: str) -> dict[str, Any]:
        """查询 session 是否被标记为取消。"""
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_cancelled(session_id)}

    @app.post("/v1/sessions/{session_id}/cancel/clear")
    async def session_cancel_clear(session_id: str) -> dict[str, Any]:
        """清除 session 的取消标记。engine 开始处理新任务时调用。"""
        st: SupervisorState = app.state.state
        st.clear_cancelled(session_id)
        return {"ok": True}

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

        if inp.target == "engine":
            if pm is None:
                raise HTTPException(status_code=409, detail="process manager not enabled")

            pm.restart_engine()

            st.eventlog.append(
                session_id="__system__",
                component="supervisor",
                type_="restart_completed",
                payload={"target": inp.target, "ts": _now().isoformat()},
            )
            return RestartOut(scheduled=True, target=inp.target)

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
