"""重试 API：POST /v1/sessions/{session_id}/retry 的插件化迁移。

Why: 重试是会话的一个业务能力，不是 supervisor 的调度内核职责；它此前内联在
api.py 的 create_app 里，与核心的 session/task/approval 端点混在一起。How:
声明一个无 hook 点的 handler，__init__ 时通过 routes face 挂路由
（mount="sessions"，路径与 core 时代完全一致，前端契约不变）；handler 从
request.app.state.state 取 SupervisorState，行为与内联版逐行一致。Purpose:
验证"语义上从属于 core 路径的功能可以迁移为内置插件而不改变 API 面"，
plugin_manager 之后的第二个 routes face 生产使用者。

engine 进程没有 routes face，__init__ 直接跳过——本插件只在 supervisor
进程有行为。非 public：走与 core 端点相同的全局 /v1/ 鉴权中间件。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

PLUGIN_META = {
    "handler_class": "RetryApiPlugin",
    "hook_points": [],
    "priority": 100,
    "description": "消息重试 API：取消活跃任务、截断对话到目标消息、重新提交",
    "author": "core",
}


class RetryApiPlugin:
    """Register the retry endpoint on the supervisor routes face."""

    name = "retry_api"
    priority = 100

    def __init__(self, ctx: Any = None) -> None:
        routes = getattr(getattr(ctx, "contributions", None), "get", lambda _n: None)("routes")
        if routes is None:
            return  # engine process: no HTTP surface

        from fastapi import APIRouter

        router = APIRouter()
        router.add_api_route(
            "/{session_id}/retry",
            retry_session_inbound,
            methods=["POST"],
        )
        routes.register(router, mount="sessions", description="消息重试")


async def retry_session_inbound(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """重试用户消息：取消活跃任务，截断对话到该消息之前，重新提交。

    [2026-08-20] 凭证两种: message_id(JSONL 消息 UUID, 通用) / source_inbound_seq(次选)。
    不依赖内存 _inbound_events / tasks, supervisor 重启后仍可重试任意历史消息。
    [plugin-admin 2026-08-23] 从 api.py 内联端点迁移为 routes face 插件；
    行为逐行保持一致，仅状态来源从闭包 app.state.state 改为 request.app.state.state。
    """
    from fastapi import HTTPException

    st = request.app.state.state
    source_seq = int(body.get("source_inbound_seq", 0) or 0)
    message_id = str(body.get("message_id") or "").strip()
    if source_seq <= 0 and not message_id:
        raise HTTPException(status_code=400, detail="source_inbound_seq or message_id required")

    with st._lock:
        route_session_id = st._route_session_id_for_session_locked(session_id)
        if not route_session_id:
            route_session_id = session_id
        if route_session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

    original_payload = None
    if source_seq > 0:
        with st._lock:
            inbound_data = st._inbound_events.get(source_seq)
            if isinstance(inbound_data, dict):
                original_payload = dict(inbound_data.get("payload") or {})

    from pathlib import Path

    from engine.conversation_store import ConversationStore

    store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
    messages = list(store.load(route_session_id))
    with st._lock:
        branch_ids = list(st._entry_branch_ids_for_parent_locked(route_session_id))
    if branch_ids:
        parent_msg_ids = set(m.id for m in messages)
        for bid in branch_ids:
            try:
                for bm in store.load(bid):
                    if bm.id not in parent_msg_ids:
                        messages.append(bm)
            except Exception:
                pass
        messages.sort(key=lambda m: m.created_at or "")

    target_msg_idx = None
    target_source_task_id = ""
    for i, m in enumerate(messages):
        if m.role != "user":
            continue
        matched = False
        if message_id and m.id == message_id:
            matched = True
            if source_seq <= 0 and isinstance(m.meta, dict):
                try:
                    source_seq = int(m.meta.get("source_inbound_seq") or 0)
                except (ValueError, TypeError):
                    pass
        elif source_seq > 0 and isinstance(m.meta, dict):
            try:
                matched = int(m.meta.get("source_inbound_seq") or -1) == source_seq
            except (ValueError, TypeError):
                pass
        if matched:
            target_msg_idx = i
            target_source_task_id = m.source_task_id or ""
            break

    if original_payload is None:
        if target_msg_idx is None:
            raise HTTPException(status_code=404, detail="inbound not found in memory or JSONL")
        _target_msg = messages[target_msg_idx]
        _content = _target_msg.content
        _text = _content if isinstance(_content, str) else ""
        if not _text and isinstance(_content, list):
            _nl = chr(10)
            _text = _nl.join(
                str(p.get("text", "")) for p in _content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        original_payload = {"text": _text}
        if isinstance(_target_msg.meta, dict) and isinstance(_target_msg.meta.get("attachments"), list):
            original_payload["attachments"] = list(_target_msg.meta["attachments"])

    new_text = body.get("new_text")
    if new_text is not None:
        original_payload["text"] = str(new_text)

    truncate_idx = len(messages)
    if target_msg_idx is not None:
        truncate_idx = target_msg_idx
    elif target_source_task_id:
        for i, m in enumerate(messages):
            if m.source_task_id == target_source_task_id:
                truncate_idx = i
                break

    st.cancel_active_tasks(route_session_id)

    main_messages = list(store.load(route_session_id))
    main_truncate_idx = len(main_messages)
    if target_msg_idx is not None and truncate_idx < len(messages):
        cutoff_msg_id = messages[truncate_idx].id
        for i, m in enumerate(main_messages):
            if m.id == cutoff_msg_id:
                main_truncate_idx = i
                break
    elif target_source_task_id:
        for i, m in enumerate(main_messages):
            if m.source_task_id == target_source_task_id:
                main_truncate_idx = i
                break

    truncated_count = len(main_messages) - main_truncate_idx
    if main_truncate_idx < len(main_messages):
        store.replace_all(route_session_id, main_messages[:main_truncate_idx])

    for bid in branch_ids:
        try:
            store.delete(bid)
        except Exception:
            pass

    _payload = {k: v for k, v in original_payload.items() if k != "session_id"}
    if source_seq > 0:
        _payload["_retry_of"] = source_seq
    evt = st.eventlog.append(
        session_id=route_session_id,
        component="shell",
        type_="inbound_message",
        payload=_payload,
    )
    st.record_inbound_message_event(evt)
    new_seq = int(evt.get("seq", 0) or 0)

    return {
        "ok": True,
        "new_inbound_seq": new_seq,
        "source_inbound_seq": source_seq,
        "truncated_messages": truncated_count,
    }
