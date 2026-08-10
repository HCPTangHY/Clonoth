from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from .config_store import ConfigStore
from .process_manager import ProcessManager
from .remote_workers import (
    RemoteCallNotFound,
    RemoteToolNotFound,
    RemoteWorkerBusy,
    RemoteWorkerManager,
    RemoteWorkerUnavailable,
)
from .state import SupervisorState
from .types import (
    AdminStateOut,
    AppConfigPublic,
    Approval,
    ApprovalDecisionIn,
    ApprovalRequestIn,
    ApprovalStatus,
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
    ProviderUpdateIn,
    ActiveProviderIn,
    FallbacksUpdateIn,
    OpRequestIn,
    OpRequestOut,
    OutboundMessageIn,
    OutboundMessageOut,
    RestartIn,
    RestartOut,
    RemoteCallCancelIn,
    RemoteCallCancelOut,
    RemoteCallCreateIn,
    RemoteCallCreateOut,
    RemoteCallResultOut,
    RemoteToolsOut,
    RemoteWorkerInfo,
    Task,
    TaskCompleteIn,
    TaskKind,
    TaskStatus,
)
from .admin_api import create_admin_router
from .admin_api import get_admin_token, get_web_auth, init_web_auth, verify_admin_token
from engine.model import resolve_provider
from engine.node import load_node


logger = logging.getLogger(__name__)

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provider_public_value(cfg: Any, key: str) -> str:
    if isinstance(cfg, dict):
        return str(cfg.get(key) or "")
    return str(getattr(cfg, key, "") or "")


def _resolve_session_active_node_effective(
    *,
    st: SupervisorState,
    cs: ConfigStore,
    session_id: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    """Return active_node data with backend-authoritative provider resolution."""
    result = dict(base)
    node_id = str(result.get("node_id") or result.get("target_node_id") or "").strip()
    if not node_id:
        return result

    try:
        node = load_node(Path(st.workspace_root), node_id)
    except Exception:
        result.update({
            "effective_provider": "",
            "effective_model": "",
            "effective_base_url": "",
        })
        return result

    providers_payload = cs.get_providers_public()
    provider_blocks = (
        providers_payload.get("providers", {}) if isinstance(providers_payload, dict)
        else getattr(providers_payload, "providers", {})
    )
    provider_configs: dict[str, dict[str, str]] = {}
    for name, cfg in provider_blocks.items():
        provider_configs[str(name)] = {
            "model": _provider_public_value(cfg, "model"),
            "base_url": _provider_public_value(cfg, "base_url"),
        }

    active_provider = str(
        providers_payload.get("active_provider", "") if isinstance(providers_payload, dict)
        else getattr(providers_payload, "active_provider", "")
    ) or "openai"
    default_model = (
        provider_configs.get(active_provider, {}).get("model")
        or provider_configs.get("openai", {}).get("model")
        or ""
    )
    route_session_id = str(result.get("session_id") or session_id or "").strip()
    override = st.get_session_provider_override(route_session_id) or {}
    rp = resolve_provider(
        Path(st.workspace_root),
        node,
        default_model,
        session_override=override,
        provider_configs=provider_configs,
        # [AutoC 2026-06-16] Why: /active_node must mirror the engine runner's
        # provider selection. How: use config.yaml provider as the default channel
        # for nodes without node.provider. Purpose: the header shows the same
        # provider/model that the next task will use.
        default_provider_type=active_provider,
    )
    result.update({
        "effective_provider": rp.provider_type,
        "effective_model": rp.model,
        "effective_base_url": rp.base_url or "",
        "node_provider": node.provider or "",
        "node_model": node.model or "",
    })
    return result


def _conversation_message_to_compact_dict(message: Any) -> dict[str, Any]:
    """Convert ConversationStore rows to the dict shape expected by compact."""
    # [2026-06-06] Why: the API reads persisted Message objects, while the shared
    # compact planner receives the in-memory dict shape used by the engine. How:
    # preserve role/content/tool fields and mirror source_task_id into _meta.
    # Purpose: API-triggered compaction sees the same task boundaries as engine
    # manual and automatic compaction.
    if isinstance(message, dict):
        raw = dict(message)
    else:
        to_dict = getattr(message, "to_dict", None)
        raw = dict(to_dict()) if callable(to_dict) else {
            "role": getattr(message, "role", "unknown"),
            "content": getattr(message, "content", ""),
            "message_type": getattr(message, "message_type", ""),
            "meta": getattr(message, "meta", {}),
            "source_task_id": getattr(message, "source_task_id", ""),
            "tool_calls": getattr(message, "tool_calls", []),
            "tool_call_id": getattr(message, "tool_call_id", ""),
            "name": getattr(message, "name", ""),
            "ephemeral": getattr(message, "ephemeral", False),
        }

    compact_message: dict[str, Any] = {
        "role": str(raw.get("role") or "unknown"),
        "content": raw.get("content", ""),
    }
    raw_meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else raw.get("meta")
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    source_task_id = str(raw.get("source_task_id") or "").strip()
    if source_task_id:
        meta.setdefault("source_task_id", source_task_id)
    message_type = str(raw.get("message_type") or "").strip()
    if message_type:
        compact_message["message_type"] = message_type
        meta.setdefault("message_type", message_type)
    if meta:
        compact_message["_meta"] = meta
    if raw.get("ephemeral") or raw.get("_dynamic"):
        compact_message["_dynamic"] = True
    if isinstance(raw.get("tool_calls"), list) and raw.get("tool_calls"):
        compact_message["tool_calls"] = raw.get("tool_calls")
    if raw.get("tool_call_id"):
        compact_message["tool_call_id"] = str(raw.get("tool_call_id") or "")
    if raw.get("name"):
        compact_message["name"] = str(raw.get("name") or "")
    return compact_message


# [WS events 2026-05-17] Why: WebSocket clients should keep long-lived event
# streams through proxies. How: send an application-level ping at this cadence
# when no EventLog row is available. Purpose: avoid idle timeout without changing
# the EventLog schema.
_WS_HEARTBEAT_SEC = 30.0

# [2026-06-03] Why: clients may send an optional initial message for backward
# compat; the server consumes and ignores it. How: brief timeout, then proceed.
_WS_INITIAL_MESSAGE_TIMEOUT_SEC = 0.5

_WS_MAX_EVENT_BYTES = 65_536  # 64 KiB soft cap for individual WS events
_WORKER_WS_MAX_FRAME_BYTES = 2 * 1024 * 1024


async def _send_worker_ws_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send one remote-worker protocol JSON frame without changing payload fields."""
    # [AutoC 2026-08-01] Keep worker protocol frames separate from UI event frames.
    # Why: _send_ws_json intentionally truncates large event payloads for browsers,
    # but workers must receive exact tool_call and cancel messages. How: serialize
    # with a hard frame-size guard and no field rewriting. Purpose: remote tool calls
    # stay protocol-correct while still limiting accidental oversized frames.
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > _WORKER_WS_MAX_FRAME_BYTES:
        raise ValueError("worker websocket frame too large")
    await websocket.send_text(text)


async def _send_ws_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send one JSON object over a WebSocket as UTF-8 text.

    [2026-06-03] Why: tool_call_end events from read_file can exceed 100 KiB,
    causing browsers to close the socket with code 1009 (Message Too Big).
    How: pre-serialize, check size, and truncate large result payloads before
    sending. Purpose: keep the WS stream alive for all clients."""
    # [WS events 2026-05-17] Why: EventLog payloads are plain dicts but may later
    # contain values FastAPI's send_json cannot serialize by default. How: use the
    # same explicit json.dumps path for events and ping frames. Purpose: make the
    # wire shape predictable and resilient to harmless non-string values.
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > _WS_MAX_EVENT_BYTES:
        # Truncate the result field in tool_call_end events to stay under the cap.
        inner = payload.get("payload")
        if isinstance(inner, dict) and "result" in inner:
            inner["result"] = str(inner["result"])[:2000] + "... [truncated for WS]"
            text = json.dumps(payload, ensure_ascii=False, default=str)
    await websocket.send_text(text)


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
    app.state.remote_workers = RemoteWorkerManager(
        workspace_root=state.workspace_root,
        eventlog=state.eventlog,
        send_json=_send_worker_ws_json,
        on_tools_changed=state.bump_tools_reload,
    )

    @app.on_event("startup")
    async def _remote_workers_startup() -> None:
        # [AutoC 2026-08-01] Start the Supervisor-side stale heartbeat reaper.
        # Why: a worker may disappear without a clean WebSocket close. How: run the
        # manager's background loop with FastAPI startup. Purpose: stale workers are
        # removed and their calls fail instead of waiting forever.
        mgr: RemoteWorkerManager = app.state.remote_workers
        await mgr.start()

    @app.on_event("shutdown")
    async def _remote_workers_shutdown() -> None:
        # [AutoC 2026-08-01] Stop remote worker runtime state on application shutdown.
        # Why: background tasks and pending calls belong to this app instance. How:
        # cancel the manager's reaper and mark connected workers offline. Purpose:
        # tests and process restarts do not leak async tasks.
        mgr: RemoteWorkerManager = app.state.remote_workers
        await mgr.stop()

    @app.get("/v1/health", response_model=HealthOut)
    async def health() -> HealthOut:
        st: SupervisorState = app.state.state
        uptime = (_now() - st.started_at).total_seconds()
        return HealthOut(
            run_id=st.eventlog.run_id, started_at=st.started_at,
            uptime_seconds=uptime,
            workspace_root=str(st.workspace_root),
        )

    @app.get("/v1/config", response_model=AppConfigPublic)
    async def get_config() -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_public()

    @app.get("/v1/config/openai", response_model=OpenAIConfigPublic)
    async def get_openai_config_public() -> OpenAIConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_public()

    @app.get("/v1/config/openai/secret", response_model=OpenAIConfigSecret)
    async def get_openai_config_secret(request: Request) -> OpenAIConfigSecret:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn, request: Request) -> AppConfigPublic:
        verify_admin_token(request)
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
    async def reload_config(request: Request) -> ConfigReloadOut:
        verify_admin_token(request)
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

    # ================================================================
    #  Multi-provider config API
    # ================================================================

    # [2026-06-07] Why: PUT/DELETE/set-active responses were missing the
    # registered provider list, causing the frontend dropdown to disappear
    # after any mutation. How: extract a helper that appends registered to
    # every provider response. Purpose: the add-channel dropdown persists.
    def _with_registered(result: dict[str, Any]) -> dict[str, Any]:
        from providers import registry as provider_registry
        result["registered"] = provider_registry.list()
        return result

    @app.get("/v1/config/providers")
    async def get_providers(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return _with_registered(cs.get_providers_public())

    @app.put("/v1/config/providers/{name}")
    async def upsert_provider(name: str, body: ProviderUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        from providers import registry as provider_registry
        if name not in provider_registry.list():
            raise HTTPException(status_code=400, detail=f"Unknown provider '{name}'. Available: {provider_registry.list()}")
        cs: ConfigStore = app.state.config_store
        return _with_registered(cs.upsert_provider(name, base_url=body.base_url, api_key=body.api_key, model=body.model))

    @app.delete("/v1/config/providers/{name}")
    async def delete_provider(name: str, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return _with_registered(cs.delete_provider(name))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/v1/config/active-provider")
    async def set_active_provider(body: ActiveProviderIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return _with_registered(cs.set_active_provider(body.provider))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/v1/config/fallbacks")
    async def update_fallbacks(body: FallbacksUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.update_fallbacks(body.fallbacks)

    @app.post("/v1/attachments/upload")
    async def upload_attachment(
        file: UploadFile = File(...),
        conversation_key: str = Query("default"),
    ) -> dict[str, Any]:
        """Upload a file attachment. Returns path relative to workspace root."""
        st: SupervisorState = app.state.state
        safe_key = conversation_key.replace(":", "_").replace("/", "_").replace("..", "_")
        att_dir = st.workspace_root / "data" / "attachments" / safe_key
        att_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename or "file").suffix or ""
        unique_name = f"{int(time.time())}_{os.urandom(6).hex()}{ext}"
        save_path = att_dir / unique_name

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 50MB)")
        save_path.write_bytes(content)

        rel_path = str(save_path.relative_to(st.workspace_root))
        mime_type = file.content_type or "application/octet-stream"
        return {
            "path": rel_path,
            "name": file.filename or unique_name,
            "size": len(content),
            "mime_type": mime_type,
            "type": "image" if mime_type.startswith("image/") else "file",
        }

    @app.get("/v1/attachments/file")
    async def attachment_file(path: str = Query(..., description="Path under data/attachments/ or legacy image under data/temp/")) -> FileResponse:
        """Serve a stored attachment file for the web frontend."""
        st: SupervisorState = app.state.state
        rel_path = str(path or "").replace("\\", "/").lstrip("/").strip()
        image_exts = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
        if rel_path.startswith("data/attachments/"):
            root = (st.workspace_root / "data" / "attachments").resolve()
        elif rel_path.startswith("data/temp/") and Path(rel_path).suffix.lower() in image_exts:
            # [AutoC 2026-06-17] Legacy image tools persisted generated files under
            # data/temp before attachment normalization copied them into
            # data/attachments. Allow image files only; other temp files remain
            # blocked. Purpose: old refreshed history can render generated pictures.
            root = (st.workspace_root / "data" / "temp").resolve()
        else:
            raise HTTPException(status_code=403, detail="attachment path not allowed")
        target = (st.workspace_root / rel_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=403, detail="attachment path not allowed")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="attachment not found")
        return FileResponse(str(target))

    @app.post("/v1/inbound", response_model=InboundMessageOut)
    async def inbound(msg: InboundMessageIn, request: Request) -> InboundMessageOut:
        st: SupervisorState = app.state.state
        direct_session_id = str(msg.session_id or "").strip()
        if direct_session_id:
            # [AutoC 2026-06-18] Direct session inbound is reserved for authenticated
            # web operators. Why: session_id bypasses the normal channel + conversation
            # key lookup and can address internal sessions. How: require Admin Token and
            # verify the session still exists. Purpose: the System session browser can
            # send into a selected session without exposing a public session-id write API.
            verify_admin_token(request)
            with st._lock:
                if direct_session_id not in st.sessions:
                    raise HTTPException(status_code=404, detail="session not found")
            session_id = direct_session_id
        else:
            session_id = st.get_or_create_session(channel=msg.channel, conversation_key=msg.conversation_key)

        # [2026-06-06] Why: engine dispatch creates agent sessions via /v1/inbound
        # as independent top-level sessions, so session_children() cannot find them
        # and the frontend sidebar loses child nodes after refresh. How: when
        # dispatch_origin carries a parent_session_id, register the newly created
        # agent session as a child of the resolved parent. Purpose: /children API
        # and sidebar child-node tree stay consistent.
        if msg.dispatch_origin and isinstance(msg.dispatch_origin, dict):
            _dispatch_parent = str(msg.dispatch_origin.get("parent_session_id") or "").strip()
            if _dispatch_parent and session_id != _dispatch_parent:
                with st._lock:
                    # Resolve entry branch to durable parent
                    _resolved_parent = st._route_session_id_for_session_locked(_dispatch_parent)
                    if not _resolved_parent:
                        _resolved_parent = _dispatch_parent
                    st.parent_children.setdefault(_resolved_parent, set()).add(session_id)
                    # Ensure parent_session_id, is_child, node_id are set on the
                    # registry entry. Unlike get_or_create_child_session which
                    # creates via on_child_session_created, inbound dispatch reuses
                    # sessions created by get_or_create_session. Patch the existing
                    # registry entry in place to avoid overwriting channel/ck.
                    _entry_node = str(msg.entry_node_id or "").strip()
                    _ctx_mode = str(msg.dispatch_context_mode or "").strip()
                    _reg = st._session_store._registry.get(session_id)
                    if isinstance(_reg, dict):
                        _reg.setdefault("is_child", True)
                        _reg["parent_session_id"] = _resolved_parent
                        if _entry_node:
                            _reg["node_id"] = _entry_node
                        if _ctx_mode:
                            _reg["context_mode"] = _ctx_mode
                        st._session_store._flush()

        # [2026-05-28] 异步 dispatch 统一走 inbound：透传新增的 dispatch 字段到 payload。
        # 为什么：model_dump() 已包含这些字段，但 record_inbound_message_event 依赖
        #   payload dict 来传递给 _create_entry_task_for_inbound_locked。
        # 怎么改：无需额外处理，Pydantic model_dump 已包含新字段。
        # 目的：确保 dispatch_origin/dispatch_context_mode/dispatch_fork_from_session
        #   能通过 event payload 传递到 task 创建逻辑。
        # [2026-06-16] Why: welcome-page model selection must be persisted before
        # the entry task is created so the worker reads the correct provider on the
        # first run. How: apply the optional provider_override right after session
        # creation and before record_inbound_message_event. Purpose: no race between
        # setting the override and the task picking it up.
        if msg.provider_override and isinstance(msg.provider_override, dict):
            st.set_session_provider_override(session_id, msg.provider_override)

        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_="inbound_message",
            payload=msg.model_dump(),
        )
        st.record_inbound_message_event(evt)
        inbound_seq = int(evt.get("seq", 0) or 0)
        return InboundMessageOut(session_id=session_id, inbound_seq=inbound_seq, accepted=True)

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

    @app.get("/v1/tasks/next", response_model=Task)
    async def task_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(120.0, ge=1.0, le=3600.0),
    ) -> Task:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
        item = st.assign_next_task(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return Task.model_validate(item)

    @app.post("/v1/tasks/{task_id}/complete")
    async def task_complete(task_id: str, body: TaskCompleteIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        task = st.complete_task(task_id=task_id, worker_id=body.worker_id, result=dict(body.result or {}))
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"ok": True, "task_id": task.task_id, "status": task.status.value}

    @app.get("/v1/tasks/{task_id}/cancelled")
    async def task_cancelled(task_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_task_cancelled(task_id)}

    @app.post("/v1/tasks/{task_id}/preempt")
    async def task_preempt(task_id: str, request: Request) -> dict[str, Any]:
        """Bot 调用：标记单个 task 为 preempt_requested。"""
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = str(body.get("message", "") or "")
        raw_atts = body.get("attachments", [])
        atts = raw_atts if isinstance(raw_atts, list) else []
        st: SupervisorState = app.state.state
        ok = st.preempt_task(task_id, message=msg, attachments=atts)
        if not ok:
            raise HTTPException(status_code=404, detail="task not found or not active")
        return {"ok": True, "task_id": task_id}

    @app.get("/v1/tasks/{task_id}/preempted")
    async def task_preempted(task_id: str) -> dict[str, Any]:
        """Engine 查询：task 是否被请求 preempt。"""
        st: SupervisorState = app.state.state
        return st.is_task_preempted(task_id)

    @app.post("/v1/tasks/{task_id}/preempt_consumed")
    async def task_preempt_consumed(task_id: str) -> dict[str, Any]:
        """Engine 读取完 preempt message 后调用，清空 message 防止重复注入。"""
        st: SupervisorState = app.state.state
        result = st.consume_preempt_message(task_id)
        return {"ok": True, **result}

    @app.post("/v1/sessions/{session_id}/async_tool_result")
    async def session_async_tool_result(session_id: str, request: Request) -> dict[str, Any]:
        """Engine 调用：异步工具完成后注入结果到 session。

        复用子节点三级回退：preempt running → 标记 suspended → 创建 inbound。
        """
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = body.get("message", "")
        atts = body.get("attachment_paths", [])
        source_task_id = str(body.get("task_id") or "").strip()
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        result = st.inject_async_result(session_id, text=msg, attachments=atts, source_task_id=source_task_id)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
        return result

    @app.get("/v1/sessions/{session_id}/running_tasks")
    async def session_running_tasks(session_id: str) -> dict[str, Any]:
        """Bot 查询当前 session 中 running/pending 状态的 task 列表。
        自动收割 lease 过期超过 grace period 的僵尸 task。
        跳过 session 中存在 pending approval 的 task（等审批不算僵尸）。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        now = _now()
        _GRACE = timedelta(seconds=180)
        tasks: list[dict[str, Any]] = []
        with st._lock:
            # [Fork/Merge 2026-05-12] running_tasks 查询主 session 时也返回入口分支任务。
            # 原因：adapter 以后需要在多个并发 branch 中选择显式 preempt 目标。
            # 做法：把主 session 与 parent→branches 索引合并为查询集合。
            # 目的：端点仍以主 session_id 调用，但能观察所有活跃分支。
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            # 检查该 session 或任一分支是否有 pending approval
            _has_pending_approval = any(
                a.status == ApprovalStatus.pending and a.session_id in session_ids
                for a in st.approvals.values()
            )
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                # 收割僵尸：running + lease 过期超过 grace period
                # 但如果 session 有 pending approval，跳过回收（等审批是合法阻塞）
                # fix: lease_expires_at 为 None 时也视为僵尸，避免无 lease 的 running 任务永远无法被收割
                if (task.status == TaskStatus.running
                        and (not task.lease_expires_at or task.lease_expires_at + _GRACE < now)
                        and not _has_pending_approval):
                    task.status = TaskStatus.failed
                    task.updated_at = now
                    task.lease_expires_at = None
                    task.result = {"action": "fail", "error": "lease expired (zombie reaped)"}
                    # 写事件，使 events.jsonl 与内存状态一致
                    st.eventlog.append(
                        session_id=task.session_id,
                        component="supervisor",
                        type_="task_completed",
                        payload=task.model_dump(mode="json"),
                    )
                    # [Fork/Merge 2026-05-12] 僵尸回收是 fail 终态，也必须走统一路由。
                    # 原因：入口分支被回收时需要 merge 回主 session，并输出错误事件。
                    # 做法：复用 task_router 的 fail 路由。目的：避免 reaped branch 永久悬挂。
                    st._route_completed_task_locked(task)
                    continue
                _is_async = bool(task.input.get("_async_dispatch"))
                _is_system = bool(task.input.get("_system_task"))
                _is_scheduled = bool(task.input.get("schedule_id"))
                branch_session_id = str(task.input.get("branch_session_id") or "")
                if not branch_session_id and task.session_id != session_id:
                    branch_session_id = task.session_id
                tasks.append({
                    "task_id": task.task_id,
                    "node_id": task.node_id or "",
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat() if task.created_at else "",
                    "caller_task_id": task.caller_task_id or "",
                    "is_user_entry": bool(not task.caller_task_id and not _is_async and not _is_system and not _is_scheduled),
                    "source_inbound_seq": task.source_inbound_seq,
                    "branch_session_id": branch_session_id,
                    "parent_session_id": str(task.input.get("parent_session_id") or (session_id if branch_session_id else "")),
                })
        return {"tasks": tasks}

    # [2026-05-28] 全局按 node_id 查找活跃任务（跨 session）。
    # 为什么：dispatch 到持久节点的任务运行在独立 session 上，调用方不知道目标 session_id。
    # 怎么改：新增端点，遍历所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：支持 preempt_task 跨 session 查找持久节点任务。
    @app.get("/v1/tasks/active-by-node/{node_id}")
    async def global_task_by_node(node_id: str) -> dict[str, Any]:
        """[2026-05-28] 全局查找指定 node_id 的活跃任务（running/pending）。

        遍历所有任务，不限定 session。用于跨 session 定位持久节点任务。
        """
        st: SupervisorState = app.state.state
        with st._lock:
            for task in st.tasks.values():
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "status": task.status.value,
                }
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

    # [2026-05-28] 按 node_id 查找 session 中活跃任务。
    # 为什么：preempt_task 原本只接受 task_id（UUID），调用者需知道精确 ID 才能操作。
    # 怎么改：新增端点，遍历 session 内所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：允许 engine 侧用 node_id（如 "bob"）定位子节点任务再执行 preempt。
    @app.get("/v1/sessions/{session_id}/tasks/by-node/{node_id}")
    async def session_task_by_node(session_id: str, node_id: str) -> dict[str, Any]:
        """按 node_id 查找 session 中活跃（running/pending）的 task。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        with st._lock:
            # 与 running_tasks 端点一致，查询主 session 及其入口分支
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {"task_id": task.task_id, "status": task.status.value}
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

    @app.post("/v1/tasks/{task_id}/renew_lease")
    async def renew_lease(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        lease_sec = float(body.get("lease_sec", 120.0))
        ok = st.renew_lease(task_id, worker_id, lease_sec)
        return {"ok": ok}

    @app.post("/v1/engine/register")
    async def engine_register(body: dict[str, Any]) -> dict[str, Any]:
        """Engine worker registers itself with a generation ID on startup.

        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        """
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        generation_id = str(body.get("generation_id") or "").strip()
        if not worker_id or not generation_id:
            raise HTTPException(status_code=400, detail="worker_id and generation_id required")
        result = st.register_engine(worker_id, generation_id)
        return result

    @app.get("/v1/tools/reload-seq")
    async def tools_reload_seq() -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"seq": st.tools_reload_seq()}

    @app.post("/v1/tools/reload")
    async def tools_reload_trigger(request: Request) -> dict[str, Any]:
        # [2026-06-16] Require Admin Token for the web-only tools reload write endpoint.
        # Why: this operation changes runtime tool state and was previously callable without auth.
        # How: accept FastAPI Request and reuse verify_admin_token before touching SupervisorState.
        # Purpose: block unauthenticated browser or network callers while preserving endpoint behavior for authenticated web calls.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        seq = st.bump_tools_reload()
        return {"ok": True, "seq": seq}

    @app.post("/v1/sessions/{session_id}/outbound", response_model=OutboundMessageOut)
    async def session_outbound(session_id: str, body: OutboundMessageIn) -> OutboundMessageOut:
        st: SupervisorState = app.state.state
        try:
            st.append_outbound_message(
                session_id=session_id,
                text=str(body.text or ""),
                attachments=body.attachments,
                source_inbound_seq=body.source_inbound_seq,
                llm_request_id=body.llm_request_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e) or "conflict")
        return OutboundMessageOut(ok=True)

    @app.get("/v1/sessions/{session_id}/events", response_model=list[Event])
    async def session_events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)
        out: list[Event] = []
        for e in evts:
            # Why: session event payloads can include large task snapshots. How:
            # stop conversion once the requested page size is reached. Purpose:
            # keep per-session polling from serializing the whole memory window.
            if len(out) >= limit:
                break
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

    @app.websocket("/v1/sessions/{session_id}/ws")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        """Stream durable EventLog rows for one session over WebSocket."""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            await websocket.close(code=4004, reason="session not found")
            return
        await websocket.accept()

        # [2026-06-03] Why: replay is removed. WS is a pure live-forward stream.
        # Clients rebuild historical state via GET /v1/sessions/{id}/history.
        # How: consume the optional initial message for backward compat, then go
        # straight to the live event loop. Purpose: eliminate catch-up replay that
        # caused stale events (e.g. old approval_requested) to be re-delivered.
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            return
        except Exception:
            pass

        queue = st.eventlog.subscribe(session_id)
        sent_seq = 0
        receive_task: asyncio.Task | None = None
        try:
            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-17] Why: clients may send harmless control
                    # frames after the initial last_seq. How: consume and ignore the
                    # text, then wait for the next client frame. Purpose: a normal
                    # client message does not terminate the event stream.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            elif receive_task is not None and receive_task.done():
                with contextlib.suppress(Exception):
                    receive_task.result()
            st.eventlog.unsubscribe(session_id, queue)

    @app.websocket("/v1/ws/worker")
    async def worker_ws(websocket: WebSocket) -> None:
        """Remote worker protocol WebSocket endpoint."""
        mgr: RemoteWorkerManager = app.state.remote_workers
        token = ""
        auth_header = str(websocket.headers.get("authorization") or "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = str(websocket.query_params.get("token") or "").strip()

        await websocket.accept()
        connection_id = ""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            hello = json.loads(raw)
            if not isinstance(hello, dict):
                hello = {}
        except Exception:
            await _send_worker_ws_json(websocket, {"type": "hello_ack", "accepted": False, "error": "invalid hello"})
            await websocket.close(code=4003, reason="invalid hello")
            return

        ack = await mgr.register_worker(websocket=websocket, token=token, hello=hello)
        await _send_worker_ws_json(websocket, ack)
        if not ack.get("accepted"):
            await websocket.close(code=4003, reason=str(ack.get("error") or "worker rejected")[:120])
            return
        connection_id = str(ack.get("connection_id") or "")

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("invalid remote worker json: connection_id=%s", connection_id)
                    continue
                if isinstance(message, dict):
                    await mgr.handle_worker_message(connection_id, message)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("remote worker websocket failed: connection_id=%s", connection_id)
        finally:
            if connection_id:
                await mgr.unregister_worker(connection_id, reason="websocket_closed")

    @app.get("/v1/remote/tools", response_model=RemoteToolsOut)
    async def remote_tools() -> RemoteToolsOut:
        mgr: RemoteWorkerManager = app.state.remote_workers
        return RemoteToolsOut(tools=await mgr.list_tools_async())

    @app.post("/v1/remote/calls", response_model=RemoteCallCreateOut)
    async def remote_call_create(body: RemoteCallCreateIn) -> RemoteCallCreateOut:
        mgr: RemoteWorkerManager = app.state.remote_workers
        try:
            call = await mgr.create_call(
                registered_name=body.registered_name,
                arguments=body.arguments,
                context=body.context,
                timeout_sec=body.timeout_sec,
            )
        except RemoteToolNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RemoteWorkerBusy as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RemoteWorkerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return RemoteCallCreateOut(call_id=call.call_id, worker_id=call.worker_id, status=call.status)

    @app.get("/v1/remote/calls/{call_id}/result", response_model=RemoteCallResultOut)
    async def remote_call_result(call_id: str, wait_sec: float = Query(0.0, ge=0.0, le=30.0)) -> Response | RemoteCallResultOut:
        mgr: RemoteWorkerManager = app.state.remote_workers
        try:
            result = await mgr.wait_call_result(call_id, wait_sec)
        except RemoteCallNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if result is None:
            status = await mgr.call_status(call_id)
            return Response(
                content=json.dumps({"ok": True, "call_id": call_id, "worker_id": status.get("worker_id"), "status": status.get("status", "running")}, ensure_ascii=False),
                status_code=202,
                media_type="application/json",
            )
        return RemoteCallResultOut.model_validate(result)

    @app.post("/v1/remote/calls/{call_id}/cancel", response_model=RemoteCallCancelOut)
    async def remote_call_cancel(call_id: str, body: RemoteCallCancelIn) -> RemoteCallCancelOut:
        mgr: RemoteWorkerManager = app.state.remote_workers
        reason = str(body.reason or "task cancelled")
        try:
            await mgr.cancel_call(call_id, reason)
            status = await mgr.call_status(call_id)
        except RemoteCallNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RemoteWorkerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return RemoteCallCancelOut(call_id=call_id, status=str(status.get("status", "cancel_requested")))

    @app.get("/v1/admin/remote/workers", response_model=list[RemoteWorkerInfo])
    async def admin_remote_workers(request: Request) -> list[RemoteWorkerInfo]:
        verify_admin_token(request)
        mgr: RemoteWorkerManager = app.state.remote_workers
        return [RemoteWorkerInfo.model_validate(item) for item in await mgr.list_workers()]

    @app.websocket("/v1/ws")
    async def global_ws(websocket: WebSocket) -> None:
        """Stream durable EventLog rows for all sessions over WebSocket."""
        st: SupervisorState = app.state.state
        await websocket.accept()

        # [2026-06-03] Why: replay is removed. WS is a pure live-forward stream.
        # Web frontend rebuilds state via loadSessionHistoryIntoStore(); SDK uses
        # _init_seq() to fast-forward before connecting. No client depends on WS
        # catch-up replay. How: consume the optional initial message for backward
        # compat, then go straight to the live loop. Purpose: eliminate full-history
        # replay that caused 109KB tool_call_end events to blow up browsers and
        # stale approval_requested events to trigger 404 auto-approve errors.
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            return
        except Exception:
            pass

        queue = st.eventlog.subscribe_global()
        sent_seq = 0
        receive_task: asyncio.Task | None = None
        try:
            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-19] Why: clients may send control frames
                    # after the initial last_seq. How: consume and ignore each text
                    # frame, then wait for another. Purpose: keep global streaming
                    # behavior aligned with the per-session WebSocket endpoint.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            elif receive_task is not None and receive_task.done():
                # [2026-06-03] Why: a finished receive_task whose exception was never
                # retrieved causes 'Task exception was never retrieved' warnings.
                # How: consume the result/exception so Python GC does not warn.
                # Purpose: clean shutdown of the global WS without log noise.
                with contextlib.suppress(Exception):
                    receive_task.result()
            st.eventlog.unsubscribe_global(queue)

    @app.get("/v1/events", response_model=list[Event])
    async def global_events(
        after_seq: int = Query(0, ge=0),
        types: str = Query("", description="comma-separated event types to filter"),
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.eventlog.list_all_events(after_seq=after_seq)
        type_filter = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        out: list[Event] = []
        for e in evts:
            if type_filter and str(e.get("type")) not in type_filter:
                continue
            if len(out) >= limit:
                break
            try:
                # Why: /v1/events may contain large task snapshots even with the
                # in-memory ring bounded. How: honor the explicit page limit
                # during conversion instead of materializing every cached Event.
                # Purpose: prevent polling/debug requests from serializing many
                # megabytes when the caller asks for a small page.
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

    @app.post("/v1/sessions/{session_id}/events")
    async def session_event(session_id: str, ev: HandoffEventIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        transient = ev.type in {"stream_delta", "stream_end", "tool_call_delta"}
        # [tool-stream 2026-05-19] tool_call_delta 是实时展示事件，不写入 JSONL。
        # 原因：参数片段可能很碎，持久化会膨胀事件日志且与 stream_delta 语义一致。
        # 做法：把它加入 supervisor transient 类型集合。
        # 目的：WebSocket 继续实时广播，但磁盘事件日志只保留稳定状态事件。
        if ev.type == "context_usage":
            transient = True
        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_=ev.type,
            payload=dict(ev.payload or {}),
            transient=transient,
        )
        if ev.type == "outbound_message":
            st.record_outbound_message_event(evt)
        if ev.type == "context_usage":
            st.update_context_usage(session_id, dict(ev.payload or {}))
        # [AutoC 2026-06-03] Why: scheduler stale-task reaper checks task.updated_at
        # but the engine heartbeat (renew_lease) only refreshes lease_expires_at,
        # causing live tasks to be falsely reaped after 10 min of no state mutation.
        # How: extract task_id from event payload and touch updated_at on running
        # tasks. Purpose: any engine activity (stream_delta, tool events, replies)
        # resets the stale timer so only truly dead tasks get reaped.
        _evt_task_id = str((ev.payload or {}).get("task_id") or "").strip()
        if _evt_task_id:
            with st._lock:
                _evt_task = st.tasks.get(_evt_task_id)
                if _evt_task is not None and _evt_task.status == TaskStatus.running:
                    _evt_task.updated_at = datetime.now(timezone.utc)
                    # [AutoC 2026-06-09] Activity heartbeat: only real
                    # production events update last_activity_at (not lease).
                    _evt_task.last_activity_at = datetime.now(timezone.utc)
                    # [AutoC 2026-06-04] Why: GET /v1/admin/tasks/active must
                    # return the current activity of each task without relying on
                    # EventLog or frontend WS inference. How: update current_phase
                    # and current_detail on the live Task object whenever a
                    # transient event arrives. Purpose: modal shows real-time
                    # task state on first open, not just after WS events.
                    _ev_type = ev.type
                    _payload = ev.payload or {}
                    if _ev_type == "stream_delta":
                        _evt_task.current_phase = "thinking" if _payload.get("type") == "thinking" else "generating"
                        _evt_task.current_detail = ""
                    elif _ev_type == "stream_end":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
                    elif _ev_type == "tool_call_start":
                        _evt_task.current_phase = "tool_call"
                        _evt_task.current_detail = str(_payload.get("tool_name") or "")
                    elif _ev_type == "tool_call_end":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
                    elif _ev_type == "approval_requested":
                        _evt_task.current_phase = "awaiting_approval"
                        _evt_task.current_detail = str(_payload.get("tool_name") or "")
                    elif _ev_type == "approval_decided":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
        return {"ok": True}

    @app.get("/v1/sessions")
    async def list_sessions(
        channel: str = Query("", description="Filter by channel (e.g. 'web')"),
        limit: int = Query(50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by channel."""
        st: SupervisorState = app.state.state
        results = []
        # [2026-06-06] Why: the frontend needs parent_session_id to distinguish
        # dispatch child sessions from top-level conversations without relying
        # on conversation_key string prefixes. How: look up the session registry
        # for parent_session_id and include it in the response. Purpose: sidebar
        # filtering uses structured metadata instead of fragile naming conventions.
        registry = getattr(st._session_store, "_registry", {})
        for sid, si in st.sessions.items():
            if channel and si.channel != channel:
                continue
            raw = registry.get(sid) if isinstance(registry, dict) else None
            parent_sid = str(raw.get("parent_session_id") or "").strip() if isinstance(raw, dict) else ""
            results.append({
                "session_id": si.session_id,
                "conversation_key": si.conversation_key,
                "channel": si.channel,
                "parent_session_id": parent_sid,
                "created_at": si.created_at.isoformat() if si.created_at else "",
                "updated_at": si.updated_at.isoformat() if si.updated_at else "",
            })
        # Sort by updated_at desc, most recent first
        results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return results[:limit]

    @app.post("/v1/sessions/get_or_create")
    async def get_or_create_session(
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Get or create a session by channel + conversation_key. No task created."""
        st: SupervisorState = app.state.state
        channel = str(body.get("channel") or "").strip()
        conv_key = str(body.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=400, detail="conversation_key is required")
        if not channel:
            channel = conv_key.split(":", 1)[0] if ":" in conv_key else "unknown"
        with st._lock:
            session_id = st.get_or_create_session(channel=channel, conversation_key=conv_key)
        return {"session_id": session_id, "conversation_key": conv_key}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
        """Delete a session and its conversation store."""
        # [2026-06-16] Require Admin Token before deleting session data.
        # Why: deletion removes Supervisor state and durable conversation files.
        # How: add Request injection and verify it before any mutation.
        # Purpose: keep this web-facing destructive endpoint admin-only.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        si = st.sessions[session_id]
        # Remove from sessions and conversation_map
        with st._lock:
            del st.sessions[session_id]
            conv_key = si.conversation_key
            if conv_key and st.conversation_map.get(conv_key) == session_id:
                del st.conversation_map[conv_key]
        # Delete ConversationStore JSONL
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            conv_store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
            conv_store.delete(session_id)
        except Exception:
            pass
        # Clean up node contexts
        try:
            from engine.context_store import cleanup_session_contexts
            cleanup_session_contexts(st.workspace_root, session_id)
        except Exception:
            pass
        # Mark as reset in sessions.json
        try:
            st._session_store.on_session_reset(session_id)
        except Exception:
            pass
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.get("/v1/sessions/{session_id}/history")
    async def session_history(
        session_id: str,
        limit: int = Query(0, ge=0, le=10000, description="Max messages to return (0=all). Applied after all processing as a tail slice."),
        offset: int = Query(0, ge=0, le=100000, description="Offset from the end. 0=latest page. offset=50,limit=50 returns messages 50-99 from the end."),
        task_id: str = Query("", description="Filter messages by source_task_id"),
    ) -> dict[str, Any]:
        """Structured message history from ConversationStore (for web frontend).

        Returns {messages: [...], total: N, has_more: bool}.
        When limit>0, returns a tail-slice window: the last `limit` messages
        starting from `offset` from the end.
        """
        st: SupervisorState = app.state.state
        all_messages = st.session_history_structured(session_id=session_id, limit=0, task_id=task_id.strip() or None)
        total = len(all_messages)
        if limit > 0:
            # offset=0, limit=50 → last 50 messages (all_messages[-50:])
            # offset=50, limit=50 → messages [-100:-50]
            end_index = total - offset
            start_index = max(0, end_index - limit)
            if end_index <= 0:
                return {"messages": [], "total": total, "has_more": False}
            page = all_messages[start_index:end_index]
            return {"messages": page, "total": total, "has_more": start_index > 0}
        return {"messages": all_messages, "total": total, "has_more": False}

    @app.get("/v1/sessions/{session_id}/pending_approvals")
    async def session_pending_approvals(session_id: str) -> list[dict[str, Any]]:
        """Pending approvals for a session (including entry branches)."""
        st: SupervisorState = app.state.state
        with st._lock:
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            results = []
            for a in st.approvals.values():
                if a.status == ApprovalStatus.pending and a.session_id in session_ids:
                    results.append(a.model_dump(mode='json'))
        return results

    @app.get("/v1/sessions/{session_id}/children")
    async def session_children(session_id: str) -> list[dict[str, Any]]:
        """Child sessions for a parent session, used by the web frontend refresh path."""
        # [2026-06-03] Why: frontend childNodes is memory-only and cannot be rebuilt
        # from /history. How: expose the supervisor's durable child-session registry
        # through a small read-only endpoint. Purpose: page refresh restores child
        # status rows and can navigate to each child session's own /history stream.
        st: SupervisorState = app.state.state
        return st.session_children(session_id=session_id)

    @app.post("/v1/sessions/{session_id}/cancel")
    async def session_cancel(session_id: str, request: Request) -> dict[str, Any]:
        # [2026-06-16] Require Admin Token for session cancellation.
        # Why: cancelling a session interrupts active work.
        # How: validate the request authorization header before calling cancel_session.
        # Purpose: prevent unauthenticated callers from stopping web tasks.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        ok = st.cancel_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/cancelled")
    async def session_cancelled(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_cancelled(session_id)}

    @app.post("/v1/sessions/{session_id}/cancel/clear")
    async def session_cancel_clear(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        st.clear_cancelled(session_id)
        return {"ok": True}

    @app.post("/v1/conversations/reset")
    async def conversation_reset(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Reset a conversation, forcing next message to create a new session."""
        # [2026-06-16] Require Admin Token for conversation reset.
        # Why: reset changes the session mapping used by the web UI.
        # How: add Request injection and verify it before reading the reset target.
        # Purpose: avoid unauthenticated resets that would disrupt chat continuity.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        conv_key = str(body.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=400, detail="conversation_key required")
        result = st.reset_conversation(conversation_key=conv_key)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "not found"))
        # emit context_reset 事件，通知 bot 侧重置高水位
        old_sid = result.get("old_session_id", "")
        if old_sid:
            st.eventlog.append(
                session_id=old_sid,
                component="supervisor",
                type_="context_reset",
                payload={"conversation_key": conv_key, "reason": "clear"},
            )
        # Also clean up node_contexts for old session
        if old_sid:
            from engine.context_store import cleanup_session_contexts
            try:
                cleaned = cleanup_session_contexts(st.workspace_root, old_sid)
                result["context_files_cleaned"] = cleaned
            except Exception:
                pass
        return result

    @app.post("/v1/tasks/{task_id}/cancel")
    async def task_cancel(task_id: str, request: Request) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链。"""
        # [2026-06-16] Require Admin Token for direct task cancellation.
        # Why: this endpoint can stop a running task by id.
        # How: verify the incoming request before mutating task state.
        # Purpose: keep high-risk task interruption restricted to authenticated admins.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.cancel_single_task(task_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "cancel failed"))
        return result

    @app.post("/v1/sessions/{session_id}/cancel_active_tasks")
    async def session_cancel_active_tasks(
        session_id: str,
        exclude_task_id: str = Query(""),
        # [2026-05-28] 可选 node_id 过滤：只取消指定节点的活跃任务。
        # 为什么：有时只想取消某个子节点的任务，而非 session 内全部。
        # 怎么改：新增 query param，透传到 cancel_active_tasks 方法。
        # 目的：更细粒度的任务取消控制。
        node_id: str = Query(""),
    ) -> dict[str, Any]:
        """取消 session 中所有活跃 task。供 AI 工具调用。"""
        st: SupervisorState = app.state.state
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: this endpoint can still be called with
            # a branch id from ToolContext in older workers. How: normalize entry
            # branches to the parent before cancellation. Purpose: sibling branches
            # under the same user conversation are included.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.cancel_active_tasks(
            route_session_id, exclude_task_id=exclude_task_id,
            node_id=node_id or None,
        )

    @app.post("/v1/sessions/{session_id}/retry")
    async def retry_session_inbound(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """重试用户消息：取消活跃任务，截断对话到该消息之前，重新提交。"""
        st: SupervisorState = app.state.state
        source_seq = int(body.get("source_inbound_seq", 0) or 0)
        if source_seq <= 0:
            raise HTTPException(status_code=400, detail="source_inbound_seq required")

        with st._lock:
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if not route_session_id:
                route_session_id = session_id
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")

            inbound_data = st._inbound_events.get(source_seq)
            if not inbound_data:
                raise HTTPException(status_code=404, detail="inbound not found")
            original_payload = dict(inbound_data.get("payload") or {})

            # 编辑后重试：用新文本覆盖原始 payload
            new_text = body.get("new_text")
            if new_text is not None:
                original_payload["text"] = str(new_text)

            # 按 source_inbound_seq 找到对应的 task，用于定位截断位置
            target_task_id = ""
            for t in st.tasks.values():
                if t.source_inbound_seq == source_seq:
                    target_task_id = t.task_id
                    break

        # 取消所有活跃任务
        st.cancel_active_tasks(route_session_id)

        # 截断 ConversationStore：删除该 task 产生的消息及之后所有内容
        from pathlib import Path
        from engine.conversation_store import ConversationStore
        store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
        messages = store.load(route_session_id)

        truncate_idx = len(messages)
        if target_task_id:
            for i, m in enumerate(messages):
                if m.source_task_id == target_task_id:
                    truncate_idx = i
                    break

        truncated_count = len(messages) - truncate_idx
        if truncate_idx < len(messages):
            store.replace_all(route_session_id, messages[:truncate_idx])

        # 清理活跃的入口分支
        with st._lock:
            branch_ids = list(st._entry_branch_ids_for_parent_locked(route_session_id))
        for bid in branch_ids:
            try:
                store.delete(bid)
            except Exception:
                pass

        # 以原始 payload 重新提交 inbound
        evt = st.eventlog.append(
            session_id=route_session_id,
            component="shell",
            type_="inbound_message",
            payload={
                **{k: v for k, v in original_payload.items() if k != "session_id"},
                "_retry_of": source_seq,
            },
        )
        st.record_inbound_message_event(evt)
        new_seq = int(evt.get("seq", 0) or 0)

        return {
            "ok": True,
            "new_inbound_seq": new_seq,
            "source_inbound_seq": source_seq,
            "truncated_messages": truncated_count,
        }

    @app.post("/v1/sessions/{session_id}/switch_node")
    async def session_switch_node(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """AI 或外部调用：设置/清除 session 级入口节点覆盖。"""
        st: SupervisorState = app.state.state
        cs: ConfigStore = app.state.config_store
        target = str(body.get("target_node_id") or "").strip()
        result = st.switch_session_node(session_id, target)
        if result.get("ok") is False:
            return result
        route_session_id = str(result.get("session_id") or session_id or "").strip()
        base = st.get_session_active_node(route_session_id)
        enriched = _resolve_session_active_node_effective(st=st, cs=cs, session_id=route_session_id, base=base)
        return {**result, **enriched, "target_node_id": enriched.get("node_id", result.get("target_node_id", ""))}

    @app.get("/v1/sessions/{session_id}/active_node")
    async def session_active_node(session_id: str) -> dict[str, Any]:
        """查询 session 当前实际使用的入口节点，并返回后端解析后的模型渠道。"""
        st: SupervisorState = app.state.state
        cs: ConfigStore = app.state.config_store
        base = st.get_session_active_node(session_id)
        return _resolve_session_active_node_effective(st=st, cs=cs, session_id=session_id, base=base)

    @app.get("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_get(session_id: str, request: Request) -> dict[str, Any]:
        """查询 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.get_session_provider_override(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.put("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_put(
        session_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """设置 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        # [AutoC 2026-06-01] Why: provider overrides are intentionally generic
        # JSON dictionaries, but the endpoint must reject non-object bodies before
        # they reach SessionInfo. How: FastAPI parses the body and this guard keeps
        # only dict values. Purpose: provider adapters can add fields later while
        # malformed requests get a clear 400 response.
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="provider_override must be a JSON object")
        result = st.set_session_provider_override(session_id, body)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.delete("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_delete(session_id: str, request: Request) -> dict[str, Any]:
        """清除 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.clear_session_provider_override(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.get("/v1/sessions/{session_id}/workspace")
    async def session_workspace_get(session_id: str, request: Request) -> dict[str, Any]:
        """查询 session 级工作区配置 {name, path}。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.get_session_workspace(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.put("/v1/sessions/{session_id}/workspace")
    async def session_workspace_put(
        session_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """设置 session 级工作区配置 {name, path}。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="workspace must be a JSON object")
        result = st.set_session_workspace(session_id, body)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.delete("/v1/sessions/{session_id}/workspace")
    async def session_workspace_delete(session_id: str, request: Request) -> dict[str, Any]:
        """清除 session 级工作区配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.clear_session_workspace(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.get("/v1/sessions/{session_id}/context_window")
    async def session_context_window(session_id: str) -> dict[str, Any]:
        """获取 session 当前上下文窗口的 token 用量信息。"""
        st: SupervisorState = app.state.state
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: context_usage events are emitted on the
            # parent route session while branch sessions are temporary storage.
            # How: normalize entry branches to the parent for this read endpoint.
            # Purpose: get_context_window reports real session usage.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.get_session_context_usage(route_session_id)

    @app.post("/v1/sessions/{session_id}/compact")
    async def session_compact(session_id: str, request: Request, body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        """手动触发上下文压缩。"""
        # [2026-06-16] Require Admin Token for manual context compaction.
        # Why: compaction injects a system operation into the selected session.
        # How: place Request before the optional Body parameter and verify it first.
        # Purpose: protect this web-only write operation without changing its payload shape.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        raw_body = body or {}
        try:
            raw_keep_recent = int(raw_body.get("keep_recent", 6))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="keep_recent must be an integer")
        keep_recent = max(1, min(20, raw_keep_recent))

        with st._lock:
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            session_generation = st._current_session_generation_locked(route_session_id) or 1

        from engine.compact_flow import prepare_compaction
        from engine.conversation_store import ConversationStore

        # [2026-06-06] Why: the API must not inject a synthetic inbound prompt to
        # make the engine call compact_context. How: read the durable session
        # history directly, convert it to compact input dicts, and ask the shared
        # planner for the compactor payload. Purpose: API, pseudo tool, and
        # automatic compact all use one planning implementation.
        store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
        messages = [_conversation_message_to_compact_dict(msg) for msg in store.load(route_session_id)]
        plan = await prepare_compaction(
            messages=messages,
            target_session_id=route_session_id,
            node_id="api",
            keep_recent=keep_recent,
        )
        if plan.status != "dispatch":
            return {
                "status": plan.status,
                "session_id": route_session_id,
                "task_id": "",
                "keep_recent": keep_recent,
                "reason": plan.reason,
            }

        dispatch_input = dict(plan.dispatch_input)
        # [2026-06-06] Why: an API-created compactor task has no suspended caller
        # task for task_router to inspect. How: mark the durable target session on
        # the compactor task input itself. Purpose: standalone compact results can
        # be recognized and written back through the same result router.
        dispatch_input["_compact_apply_to_session_id"] = route_session_id

        with st._lock:
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
            session_generation = st._current_session_generation_locked(route_session_id) or session_generation or 1
            child_session_id, _ = st.get_or_create_child_session(
                route_session_id, "system.compactor", "", "fresh",
            )
            dispatch_input["child_session_id"] = child_session_id
            dispatch_input["context_mode"] = "fresh"
            dispatch_input["use_context"] = False
            task = st._create_task_locked(
                session_id=route_session_id,
                session_generation=session_generation,
                kind=TaskKind.node,
                node_id="system.compactor",
                input_data=dispatch_input,
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )

        return {
            "status": plan.status,
            "session_id": route_session_id,
            "task_id": task.task_id,
            "keep_recent": keep_recent,
        }


    @app.post("/v1/approvals/request", response_model=Approval)
    async def approval_request(inp: ApprovalRequestIn) -> Approval:
        st: SupervisorState = app.state.state
        # [AutoC 2026-05-31] Why: direct approval API callers may know which tool
        # produced the request. How: forward optional identity fields accepted by
        # ApprovalRequestIn. Purpose: both direct and policy-created approvals can
        # render inside ToolCallCard when possible.
        return st.create_approval(
            session_id=inp.session_id,
            operation=inp.operation,
            details=inp.details,
            tool_call_id=inp.tool_call_id,
            node_id=inp.node_id,
            task_id=inp.task_id,
        )

    @app.get("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_get(approval_id: str) -> Approval:
        st: SupervisorState = app.state.state
        if approval_id not in st.approvals:
            raise HTTPException(status_code=404, detail="approval not found")
        return st.approvals[approval_id]

    @app.post("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_decide(approval_id: str, body: ApprovalDecisionIn, request: Request) -> Approval:
        # [2026-06-16] Require Admin Token for approval decisions.
        # Why: allow and deny decisions authorize or block privileged tool execution.
        # How: verify the request before reading or updating the pending approval.
        # Purpose: ensure only authenticated admins can decide web approvals.
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        a = st.decide_approval(approval_id=approval_id, decision=body.decision, comment=body.comment)
        if a is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return a

    @app.post("/v1/ops/request", response_model=OpRequestOut)
    async def ops_request(inp: OpRequestIn) -> OpRequestOut:
        st: SupervisorState = app.state.state
        # [AutoC 2026-05-31] Why: ops/request is the policy path used by tools.
        # How: pass optional tool_call_id/node_id/task_id through to the supervisor
        # state layer. Purpose: approval_requested events can update the active tool
        # card instead of creating a detached approval card.
        return st.request_operation(
            session_id=inp.session_id,
            op=inp.op,
            parameters=inp.parameters,
            tool_call_id=inp.tool_call_id,
            node_id=inp.node_id,
            task_id=inp.task_id,
            workspace=inp.workspace,
        )

    @app.get("/v1/admin/state", response_model=AdminStateOut)
    async def admin_state(request: Request) -> AdminStateOut:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        return st.admin_state()

    @app.get("/v1/admin/tasks/active")
    async def admin_active_tasks(request: Request) -> list[dict[str, Any]]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        active_statuses = {TaskStatus.running, TaskStatus.pending, TaskStatus.suspended}
        result: list[dict[str, Any]] = []
        with st._lock:
            for t in st.tasks.values():
                if t.status not in active_statuses:
                    continue
                # [AutoC 2026-06-04] Why: the System dashboard modal needs task
                # details, but full Task objects include large input/result and
                # continuation payloads. How: return only identifying metadata and
                # timestamps while holding the state lock during iteration. Purpose:
                # operators can inspect active work without leaking heavy payloads or
                # racing concurrent task updates.
                # [AutoC 2026-06-04] Why: operators need enough context to
                # identify a task in the modal, but returning full Task.input can be
                # large or sensitive. How: extract only text/instruction, normalize it
                # to a trimmed string, and cap it at 200 characters. Purpose: the
                # active-task API supports a safe human-readable input preview.
                input_text = str(t.input.get("text") or t.input.get("instruction") or "").strip()
                result.append({
                    "task_id": t.task_id,
                    "session_id": t.session_id,
                    "node_id": t.node_id,
                    "status": t.status.value,
                    "kind": t.kind.value,
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                    "worker_id": t.worker_id,
                    "caller_task_id": t.caller_task_id,
                    "input_summary": input_text[:200] if input_text else "",
                    "cancel_requested": t.cancel_requested,
                    "current_phase": t.current_phase,
                    "current_detail": t.current_detail,
                    "last_activity_at": t.last_activity_at.isoformat() if t.last_activity_at else None,
                })
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    @app.post("/v1/admin/restart", response_model=RestartOut)
    async def admin_restart(inp: RestartIn, request: Request) -> RestartOut:
        verify_admin_token(request)
        pm: ProcessManager | None = app.state.process_manager
        st: SupervisorState = app.state.state

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

        # 2026.4.28: restart engine 暂时禁用，统一走 restart all
        # 原因：engine 子进程终止时会向 supervisor 泄露信号（SIGINT/SIGTERM），
        # 导致 uvicorn 触发优雅退出，supervisor 跟着一起死。
        # 根因是 uvicorn.run() 会覆盖手动设置的信号 handler，
        # 目前未找到稳定的隔离方案，暂时所有重启统一走 restart all。
        if inp.target == "engine_DISABLED":
            # 重新加载 .env，确保修改后的环境变量在 supervisor 进程中生效
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)
            except Exception:
                pass
            # 先注入 outbound 让 handle_agent 正常闭合（log embed 有终态）
            if inp.session_id:
                # 找到当前 session 活跃任务的 source_inbound_seq，
                # 确保 Bot 端 poller 能匹配到 trigger 并正确关闭 status_msg
                _restart_src_seq = None
                for _rt in st.tasks.values():
                    if (_rt.session_id == inp.session_id
                            and _rt.status in (TaskStatus.running, TaskStatus.pending)
                            and _rt.source_inbound_seq):
                        _restart_src_seq = _rt.source_inbound_seq
                        break
                _restart_outbound_payload: dict[str, Any] = {"text": "✅ 已触发 Engine 重启，正在执行..."}
                if _restart_src_seq:
                    _restart_outbound_payload["source_inbound_seq"] = _restart_src_seq
                st.eventlog.append(
                    session_id=inp.session_id,
                    component="supervisor",
                    type_="outbound_message",
                    payload=_restart_outbound_payload,
                )
            # Deferred engine restart: return HTTP 200 first, then kill+restart.
            # This ensures the tool call in the dying engine receives its response
            # and can shadow_write the tool_result before being terminated.
            _restart_session_id = inp.session_id
            _restart_target = inp.target

            def _deferred_engine_restart() -> None:
                time.sleep(1)  # let HTTP response reach engine first
                pm._restarting_engine = True  # 抑制信号 handler 退出
                try:
                    pm.stop_engine()
                    time.sleep(1)  # 等待延迟信号消散
                    pm.start_engine()
                    # Brief health check: verify engine process is alive
                    time.sleep(0.5)
                    _alive = any(p.popen.poll() is None for p in pm.engines)
                    if not _alive:
                        st.eventlog.append(
                            session_id=_restart_session_id or "__system__",
                            component="supervisor",
                            type_="outbound_message",
                            payload={"text": "❌ Engine 重启失败：进程启动后立即退出。"},
                        )
                        return
                except Exception as exc:
                    pm._restarting_engine = False
                    st.eventlog.append(
                        session_id=_restart_session_id or "__system__",
                        component="supervisor",
                        type_="outbound_message",
                        payload={"text": f"❌ Engine 重启失败：{exc}"},
                    )
                    return
                # ---- 清理旧 engine 遗留的孤儿 task ----
                _orphan_count = st.cancel_orphaned_tasks()
                if _orphan_count:
                    st.eventlog.append(
                        session_id="__system__",
                        component="supervisor",
                        type_="orphan_cleanup",
                        payload={
                            "count": _orphan_count,
                            "trigger": "engine_restart",
                            "ts": _now().isoformat(),
                        },
                    )
                st.eventlog.append(
                    session_id="__system__",
                    component="supervisor",
                    type_="restart_completed",
                    payload={"target": _restart_target, "ts": _now().isoformat()},
                )
                # Defer restart notification — will be injected in register_engine()
                # after orphan cleanup, so the task won't be reaped.
                if _restart_session_id:
                    st._pending_restart_notify = _restart_session_id
                pm._restarting_engine = False  # 清除信号抑制

            threading.Thread(target=_deferred_engine_restart, daemon=True, name="restart-engine").start()
            return RestartOut(scheduled=True, target=_restart_target)

        # 先停 engine，再延迟退出让 HTTP 响应发出去。
        if inp.session_id:
            _pending_path = Path(st.workspace_root) / "data" / "restart_pending.json"
            _si = st.sessions.get(inp.session_id)
            try:
                _pending_path.write_text(json.dumps({
                    "session_id": inp.session_id,
                    "target": "all",
                    "conversation_key": _si.conversation_key if _si else "",
                    "channel": _si.channel if _si else "",
                    "ts": _now().isoformat(),
                }), encoding="utf-8")
            except Exception:
                pass
            # 找 source_inbound_seq
            _all_restart_src_seq = None
            for _art in st.tasks.values():
                if (_art.session_id == inp.session_id
                        and _art.status in (TaskStatus.running, TaskStatus.pending)
                        and _art.source_inbound_seq):
                    _all_restart_src_seq = _art.source_inbound_seq
                    break
            _all_restart_payload: dict[str, Any] = {"text": "✅ 已触发全量重启，系统即将重启..."}
            if _all_restart_src_seq:
                _all_restart_payload["source_inbound_seq"] = _all_restart_src_seq
            st.eventlog.append(
                session_id=inp.session_id,
                component="supervisor",
                type_="outbound_message",
                payload=_all_restart_payload,
            )

        # [AutoC 2026-06-11] Why: 全量重启会杀掉 supervisor 进程，WS 断开后
        # bot 漏掉 engine_registered 事件导致残留 typing/日志/status_msg。
        # How: 在 WS 还活着时发 engine_restarting 事件让 bot 提前清理。
        # Purpose: 即使 engine_registered 漏掉也不影响清理。
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="engine_restarting",
            payload={"target": "all", "ts": _now().isoformat()},
        )

        def _deferred_exit() -> None:
            # [2026-06-07] Why: os._exit(75) skips socket shutdown, atexit
            # handlers, and GC — leaving the port in TIME_WAIT and causing
            # the restart loop. How: stop child processes first, then signal
            # uvicorn to exit gracefully via should_exit. The main thread's
            # run_until_complete returns, main() sees _restart_pending and
            # calls sys.exit(75) which runs atexit (port file cleanup) and
            # closes the server socket properly. Purpose: the restarted
            # process can bind the port immediately.
            time.sleep(1)
            if pm is not None:
                try:
                    pm.stop_engine()
                except Exception:
                    pass
                for _eng in getattr(pm, 'engines', []):
                    try:
                        _eng.popen.wait(timeout=5)
                    except Exception:
                        pass
            _server = getattr(app.state, '_uvicorn_server', None)
            if _server is not None:
                _server.should_exit = True

        if pm is not None:
            pm._restart_pending = True
        threading.Thread(target=_deferred_exit, daemon=True, name="restart-all").start()
        return RestartOut(scheduled=True, target="all")

    # ---- 异步委派 API ----
    @app.post("/v1/tasks/dispatch-async")
    async def dispatch_async(request: Request) -> dict[str, Any]:
        """异步委派子节点：创建子任务后立即返回 task_id，父任务不挂起。"""
        st: SupervisorState = app.state.state
        body = await request.json()

        session_id = str(body.get("session_id") or "").strip()
        session_generation = int(body.get("session_generation", 1))
        node_id = str(body.get("node_id") or "").strip()
        instruction = str(body.get("instruction") or "").strip()
        context_mode = str(body.get("context_mode") or "accumulate").strip()
        context_key = str(body.get("context_key") or "").strip() or None
        source_inbound_seq = body.get("source_inbound_seq")
        caller_node_id = str(body.get("caller_node_id") or "").strip()
        # [Fork/Merge 2026-05-17] Why: newer engine workers include the parent
        # route session when an async dispatch is requested from a branch. How:
        # read it as a fallback for branch index recovery. Purpose: async children
        # are anchored to the durable conversation even if branch indexes are stale.
        parent_session_id = str(body.get("parent_session_id") or "").strip()
        # [2026-04-22] 读取父节点传来的附件列表，透传到 input_data 供 runner.py 消费
        attachments = body.get("attachments")

        if not session_id or not node_id:
            raise HTTPException(status_code=400, detail="session_id and node_id required")
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        input_data: dict[str, Any] = {
            "instruction": instruction,
            "_async_dispatch": True,
            "_caller_node_id": caller_node_id,
        }
        # [2026-04-22] 将附件列表注入 input_data，runner.py L594 已支持读取 input_data["attachments"]
        if attachments and isinstance(attachments, list):
            input_data["attachments"] = attachments
        if context_key:
            input_data["_context_key"] = context_key

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                src_seq = int(source_inbound_seq)
            except (ValueError, TypeError):
                pass

        with st._lock:
            # [2026-05-14] 异步子任务应挂到 parent session 而非 caller 的 branch。
            # 问题：caller 跑在 entry branch 上，caller finish 后 branch 被清理，
            # 导致异步子任务被连带 cancelled。
            # 修复：如果 session_id 是 entry branch，追溯到 parent session。
            # 子任务完成后 _inject_async_dispatch_result_locked 会往 parent 注入
            # inbound 并 fork 新 branch 处理结果，路径不受影响。
            st._ensure_entry_branch_indexes_locked()
            _task_session_id = session_id
            _task_generation = session_generation
            _parent_of_branch = st.entry_branch_parents.get(session_id)
            if not _parent_of_branch and parent_session_id:
                # [Fork/Merge 2026-05-17] Why: a restarted supervisor may have to
                # infer branch ancestry from the request payload or sessions.json.
                # How: trust parent_session_id only when the supplied session is an
                # entry branch for that parent. Purpose: avoid misrouting ordinary
                # child sessions while recovering branch async dispatch routing.
                if st._is_entry_branch_session_locked(session_id, parent_session_id=parent_session_id):
                    _parent_of_branch = parent_session_id
            if _parent_of_branch:
                _task_session_id = _parent_of_branch
                _task_generation = st._current_session_generation_locked(_task_session_id) or 1

            # Child Session 隔离（Phase B）：async dispatch 也走 child session
            _child_sid, _is_new = st.get_or_create_child_session(
                _task_session_id, node_id, context_key or "", context_mode,
            )
            input_data["child_session_id"] = _child_sid
            input_data["context_mode"] = context_mode
            input_data["use_context"] = False
            if context_mode == "fork":
                input_data["fork_from_session_id"] = _task_session_id
            # 审计报告 Step 1（2026-04-16）：删除 async dispatch 的 accumulate fallback。
            # engine/runner.py:514 在 child_session_id 非空时会无条件清空 context_ref，
            # 此 fallback 注入永远不会被消费，属于兼容期死代码。

            task = st._create_task_locked(
                session_id=_task_session_id,
                session_generation=_task_generation,
                kind=TaskKind.node,
                node_id=node_id,
                input_data=input_data,
                continuation={},
                source_inbound_seq=src_seq,
                caller_task_id=None,
            )

        return {"ok": True, "task_id": task.task_id}
    admin_router = create_admin_router(workspace_root=state.workspace_root)
    app.include_router(admin_router, prefix="/v1/admin/config")

    # ---- Web JWT auth 初始化 ----
    _data_dir = state.workspace_root / "data"
    init_web_auth(_data_dir)

    # 认证校验端点：前端用来验证 token/JWT 是否正确
    @app.get("/v1/admin/auth/check")
    async def admin_auth_check(request: Request) -> dict[str, Any]:
        try:
            verify_admin_token(request)
        except HTTPException:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"ok": True}

    # Web JWT 设置状态查询（无需认证，前端决定显示登录还是设置页面）
    @app.get("/v1/admin/auth/status")
    async def admin_auth_status() -> dict[str, Any]:
        wa = get_web_auth()
        if not wa or not wa.available:
            return {"mode": "token", "setup_completed": False, "jwt_available": False}
        return {
            "mode": "jwt" if wa.setup_completed else "setup",
            "setup_completed": wa.setup_completed,
            "jwt_available": True,
        }

    # Web JWT 初始化设置（只能调用一次）
    @app.post("/v1/admin/auth/setup")
    async def admin_auth_setup(request: Request) -> dict[str, Any]:
        wa = get_web_auth()
        if not wa or not wa.available:
            raise HTTPException(status_code=501, detail="JWT not available (missing bcrypt/PyJWT)")
        if wa.setup_completed:
            raise HTTPException(status_code=409, detail="Setup already completed")
        body = await request.json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        expire_hours = int(body.get("jwt_expire_hours", 720))
        result = wa.setup(username, password, jwt_expire_hours=expire_hours)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "setup failed"))
        return result

    # Web JWT 登录
    @app.post("/v1/admin/auth/login")
    async def admin_auth_login(request: Request) -> dict[str, Any]:
        wa = get_web_auth()
        if not wa or not wa.available or not wa.setup_completed:
            raise HTTPException(status_code=501, detail="JWT auth not configured")
        body = await request.json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        result = wa.login(username, password)
        if not result.get("ok"):
            raise HTTPException(status_code=401, detail=result.get("error", "login failed"))
        return result

    # Web JWT 修改密码（需认证）
    @app.post("/v1/admin/auth/change-password")
    async def admin_auth_change_password(request: Request) -> dict[str, Any]:
        verify_admin_token(request)  # Require valid session
        wa = get_web_auth()
        if not wa or not wa.available or not wa.setup_completed:
            raise HTTPException(status_code=501, detail="JWT auth not configured")
        body = await request.json()
        username = str(body.get("username") or "").strip()
        new_password = str(body.get("new_password") or "")
        if not username or not new_password:
            raise HTTPException(status_code=400, detail="username and new_password required")
        if not wa.change_password(username, new_password):
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}

    from starlette.responses import RedirectResponse as _RR

    # [2026-05-16] Web chat frontend
    web_dist = state.workspace_root / "adapters" / "web" / "frontend" / "dist"
    web_mounted = False
    if web_dist.is_dir():
        # [2026-06-22] 反代 Host 头为 localhost 时，StaticFiles 的 307 /web→/web/
        # 会生成 http://localhost/web/ 导致浏览器跳转错误。
        # 用相对路径重定向绕过，不依赖 Host 头。
        @app.get("/web")
        async def _web_trailing_slash() -> _RR:
            return _RR("./web/")

        app.mount("/web", StaticFiles(directory=str(web_dist), html=True), name="web")
        web_mounted = True
        print(f"[web] 前端地址: http://{{host}}:{{port}}/web/", flush=True)

    # Legacy admin assets are optional. If adapters/admin is absent, keep /admin
    # as a stable entry point by redirecting it to the current web frontend.
    admin_dir = state.workspace_root / "adapters" / "admin"
    if admin_dir.is_dir():
        @app.get("/admin")
        async def _admin_trailing_slash() -> _RR:
            return _RR("./admin/")

        app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")
        print(f"[admin] 管理界面地址: http://{{host}}:{{port}}/admin/", flush=True)
    elif web_mounted:
        @app.get("/admin")
        @app.get("/admin/")
        async def _admin_redirect_to_web() -> _RR:
            return _RR("/web/")

    # 启动时打印 token 并写入共享文件供 engine 读取
    token = get_admin_token()
    _token_file = state.workspace_root / "data" / ".admin_token"
    _token_file.parent.mkdir(parents=True, exist_ok=True)
    _token_file.write_text(token, encoding="utf-8")
    print(f"[admin] 管理 Token: {token}", flush=True)
    print(f"[admin] Token 已写入: {_token_file}", flush=True)

    return app
