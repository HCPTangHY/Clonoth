"""LLM 调用（含重试逻辑）。

从 ai_step.py 抽出。依赖 _LoopState、_StreamBuffer。
"""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .stream_buffer import _StreamBuffer
from ..attachments import prepare_messages_for_llm
from ..protocol import TaskAction, ACTION_CANCELLED, ACTION_FAIL
from .loop_state import _LoopState, _persist_ctx, _short
from ..hooks import HookContext, hook_registry
# Phase 1: Signal System — 引入信号总线，用于 LLM 调用的可观测性
# 在 while True 循环前发射 llm.call.start，在 return resp 前发射 llm.call.end，
# 在重试路径发射 llm.retry，在不可重试失败时发射 llm.error。
from engine.signals import Signal, get_bus
from engine.signals.types import make_span_id


# ---------------------------------------------------------------------------
#  可重试状态码
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# [fix 2026-05-07] Why: some OpenAI-compatible proxy layers return upstream
# key-pool exhaustion as HTTP 400/402/403 instead of 429, and some provider
# adapters use status_code=0 for transport exceptions. How: keep the strict HTTP
# status whitelist, but add a separate combined semantic check for proxy resource
# exhaustion. Purpose: retry recoverable upstream/key-pool failures without
# retrying ordinary invalid-request 4xx responses that share the proxy prefix.
_RESOURCE_LIMIT_CLIENT_STATUS_CODES = frozenset({400, 402, 403})
_PROVIDER_FAILURE_MARKERS = (
    "provider response failed",
)
_RESOURCE_LIMIT_ERROR_MARKERS = (
    "quota exhausted",
    "credits exhausted",
    # [fix 2026-05-07] Why: bare "insufficient" or "credits" can also appear in
    # schema/field/permission errors. How: only match explicit resource-limit
    # phrases. Purpose: retry real quota/credit exhaustion without retrying
    # malformed 400 requests.
    "insufficient quota",
    "insufficient_quota",
    "insufficient credits",
    "insufficient credit",
    "insufficient balance",
    "too many requests",
    "rate limit",
    "rate_limit",
)


def _has_resource_limit_marker(error: str | None) -> bool:
    """Return whether an error is an upstream resource-limit failure."""
    # [fix 2026-05-07] Why: resource-limit 4xx may be returned directly by a
    # provider or wrapped by a proxy. How: match explicit quota/rate-limit phrases
    # without requiring a generic provider-failure prefix. Purpose: keep quota
    # exhaustion retryable while preventing schema/permission errors from retrying.
    err = (error or "").lower()
    return any(marker in err for marker in _RESOURCE_LIMIT_ERROR_MARKERS)


# [fix 2026-05-28] Why: some upstream providers and gateways return HTTP 200
# but embed server-side errors (429, 503, overloaded, capacity) inside
# the SSE stream body. The provider parser sets ok=False but keeps status_code=200.
# How: define explicit transient server-error markers that indicate recoverable
# failures, separate from content-policy refusals (safeguard, refused, etc.).
# Purpose: retry genuine server-side transient errors that arrive via HTTP 200
# streams, while leaving content-policy refusals for fallback_provider to handle
# via a different provider (retrying the same provider for safeguard is futile).
_STREAM_SERVER_ERROR_MARKERS = (
    "429",
    "503",
    "rate_limit",
    "rate limit",
    "overloaded",
    "capacity",
    "too many requests",
    "server_error",
    "internal_error",
    "service_unavailable",
)


def _has_stream_server_error(error: str | None) -> bool:
    """Return whether an error text indicates a transient server-side error
    embedded in an HTTP 200 stream (e.g. SSE error objects with 429/503)."""
    err = (error or "").lower()
    return any(marker in err for marker in _STREAM_SERVER_ERROR_MARKERS)


def _is_retryable_error(resp) -> bool:
    """判定 ProviderResponse 是否属于可重试的临时性错误。"""
    if resp.ok:
        return False
    status_code = resp.status_code
    if status_code is not None:
        if status_code <= 0:
            return True
        if status_code in _RETRYABLE_STATUS_CODES:
            return True
        if status_code in _RESOURCE_LIMIT_CLIENT_STATUS_CODES:
            return _has_resource_limit_marker(getattr(resp, "error", None))
        # [fix 2026-05-28] Why: SSE streams can return HTTP 200 but contain
        # server errors (429/503/overloaded) in the event data. The provider
        # parser sets ok=False but preserves status_code=200. How: check error
        # text for transient server-error markers. Purpose: retry genuine
        # server-side transient errors while leaving content-policy refusals
        # (safeguard blocks) for fallback_provider to handle via a different
        # provider rather than retrying the same one.
        if status_code == 200:
            return _has_stream_server_error(getattr(resp, "error", None))
        return False
    return True


async def prepare_request_history(
    messages: list[dict[str, Any]], *, node: Any, provider: Any, rctx: Any, step: int = 0,
) -> tuple[list[dict[str, Any]], TaskAction | None]:
    """Prepare an isolated internal history, without selecting an API format.

    Primary calls, fallbacks and compacted retries share attachment resolution
    and request hooks. Provider metadata and structured tool calls stay intact.
    """
    history = deepcopy([
        {key: value for key, value in message.items() if key != "response_info"}
        for message in messages if not message.get("_ephemeral")
    ])
    # Request plugins may freeze derived data in message metadata, not in the
    # user's body. Stage all changes and commit only after the hook succeeds.
    source_by_id = {m["id"]: m for m in messages if m.get("id")}
    store = getattr(rctx, "conversation_store", None)
    session_id = getattr(rctx, "child_session_id", "") or getattr(rctx, "session_id", "")
    stored = None
    pending: dict[str, dict[str, Any]] = {}
    freeze_errors: list[Exception] = []

    def freeze_message_metadata(message, key, factory):
        nonlocal stored
        message_id = message.get("id")
        source = source_by_id.get(message_id)
        if (source is not None and (store is None or not session_id)
                and key in (source.get("_meta") or {})):
            return deepcopy(source["_meta"][key])  # A retained legacy snapshot needs no new write.
        if source is None or store is None or not session_id:
            return None  # No durable identity: do not invent text/ordinal identities.
        if not isinstance(key, str) or not key:
            raise ValueError("metadata key must be nonempty")
        if stored is None:
            try:
                stored = store.load(session_id)
            except Exception as exc:
                freeze_errors.append(exc)
                raise
        row = next((m for m in stored if m.id == message_id), None)
        if row is None:
            return None  # A compacted-away or inherited foreign row is not writable.
        meta = {**row.meta, **pending.get(message_id, {})}
        if key in meta:
            return deepcopy(meta[key])
        try:
            value = factory()
            import json
            json.dumps(value, allow_nan=False)  # Only durable JSON metadata is accepted.
        except Exception as exc:
            freeze_errors.append(exc)
            raise
        pending.setdefault(message_id, {})[key] = deepcopy(value)
        return deepcopy(value)

    workspace_root = getattr(rctx, "workspace_root", None)
    if workspace_root is not None:
        history = prepare_messages_for_llm(history, workspace_root)
    ctx = HookContext(
        messages=history, tools=[], node=node, provider=provider, rctx=rctx, step=step,
        extra={"workspace_root": workspace_root, "workspace": getattr(rctx, "workspace", None),
               "freeze_message_metadata": freeze_message_metadata},
    )
    result = await hook_registry.afire("before_llm_call", ctx)
    if freeze_errors:
        raise RuntimeError("request metadata snapshot failed") from freeze_errors[0]
    if pending and result.action is None:
        # Same-session writes are serialized by the task lifecycle, as for other
        # ConversationStore mutations. Reload so hooks cannot overwrite new rows.
        latest = store.load(session_id)
        rows = {m.id: m for m in latest}
        if not pending.keys() <= rows.keys():
            raise RuntimeError("message disappeared while freezing request metadata")
        for message_id, meta in pending.items():
            rows[message_id].meta = {**rows[message_id].meta, **meta}
        store.replace_all(session_id, latest)
        for message_id, meta in pending.items():
            source = source_by_id[message_id]
            source["_meta"] = {**source.get("_meta", {}), **deepcopy(meta)}
    return ctx.messages, result.action


def _has_injectable_preempt(info: dict[str, Any] | None) -> bool:
    """Return whether a preempt payload carries content that must be injected.

    Why: a preempt may arrive with attachments only, so testing the message text
    alone silently downgraded it to a plain stop request. How: accept text,
    attachments and the supervisor's attachment_count field. Purpose: every
    injectable preempt reaches the next prompt.
    """
    payload = info if isinstance(info, dict) else {}
    if str(payload.get("message") or "").strip():
        return True
    if payload.get("attachments"):
        return True
    try:
        return int(payload.get("attachment_count") or 0) > 0
    except (TypeError, ValueError):
        return False


async def call_provider_once(
    ls: _LoopState, step: int, provider: Any, *, messages: list[dict[str, Any]] | None = None,
):
    """Run one provider request through the normal request pipeline.

    Why: a response-replacement hook needs a second attempt without owning
    formatting, request ids, cancellation and preempt handling. How: call the
    shared retry path on a local loop-state view whose provider is the candidate
    and whose retry budget is zero, then propagate scheduling flags back.
    Purpose: the capability stays generic — no provider names, no recursion into
    after_llm_call, and the configured provider is untouched.
    """
    call = replace(
        ls,
        provider=provider,
        messages=ls.messages if messages is None else messages,
        use_stream=ls.use_stream,
        retry_max=0,
    )
    # Timing belongs to this attempt, never to the primary or a prior fallback.
    call._reasoning_started_iso = ""
    call._reasoning_ended_iso = ""
    # A replacement attempt must not start another reactive-compaction attempt.
    call._reactive_compact_done = True
    try:
        response = await _call_llm_with_retry(call, step)
        if getattr(response, "ok", False):
            ls._reasoning_started_iso = call._reasoning_started_iso
            ls._reasoning_ended_iso = call._reasoning_ended_iso
        return response
    finally:
        ls.preempt_after_step = call.preempt_after_step
        ls.preempt_inject_info = call.preempt_inject_info


# ---------------------------------------------------------------------------
#  LLM 调用（含重试）
# ---------------------------------------------------------------------------

async def _call_llm_with_retry(ls: _LoopState, step: int):
    """LLM 调用（含重试）。

    返回值：
      - ProviderResponse: 正常完成
      - TaskAction(CANCELLED): 被取消
      - None: 思考阶段被 preempt 截断，partial message 已丢弃
    """
    tools_arg = ls.openai_tools if ls.openai_tools else None
    tool_mode = getattr(ls.formatter, "mode", None) or getattr(ls.node, "tool_mode", "native")
    llm_messages, action = await prepare_request_history(
        ls.messages, node=ls.node, provider=ls.provider, rctx=ls.rctx, step=step,
    )
    if action is not None:
        return action
    ls.request_history = llm_messages

    resp = None
    _retry_attempt = 0

    # Phase 1: Signal System — 初始化信号总线，发射 llm.call.start
    # 不使用 span 上下文管理器，避免整个函数体缩进变动。
    # 手动在关键路径发射 start/end/retry/error 信号。
    _bus = get_bus()
    _span_id = make_span_id()
    _sig_payload = {"model": getattr(ls.provider, 'model', 'unknown'), "provider": type(ls.provider).__name__}
    _sig_t0 = time.monotonic()
    _bus.emit(Signal(name="llm.call.start", payload=_sig_payload, span_id=_span_id))

    while True:
        # Stream timing belongs to this provider attempt, not a previous step or
        # failed retry. Non-streaming/no-thinking responses have no precise timing.
        ls._reasoning_started_iso = ""
        ls._reasoning_ended_iso = ""
        # [AutoC 2026-06-04] Why: retries and multi-step tasks can issue several
        # provider requests under one task id. How: create a fresh request id at the
        # start of each actual provider call attempt, before any stream delta can be
        # emitted. Purpose: all realtime blocks from this attempt share one card key.
        request_id = ls.rctx.begin_llm_request()
        # Each transport attempt owns its timing, including retries and
        # non-streaming calls that cannot observe individual reasoning tokens.
        ls._reasoning_started_iso = ""
        ls._reasoning_ended_iso = ""
        text_buf: _StreamBuffer | None = None
        think_buf: _StreamBuffer | None = None

        # ---- 流式调用 ----
        if ls.use_stream:
            text_buf = _StreamBuffer(ls.rctx, ls.node.id, "text", request_id=request_id)
            think_buf = _StreamBuffer(ls.rctx, ls.node.id, "thinking", request_id=request_id)

            async def _emit_tool_delta(payload: dict[str, Any]) -> None:
                # [tool-stream 2026-05-19] 将 provider 的工具调用增量接入 RunContext 事件流。
                # 原因：provider 现在能实时解析 tool_call 参数，但 supervisor 只认识 emit_event。
                # 做法：复制 payload，补充 node_id/task_id 后以 tool_call_delta 事件发送。
                # 目的：tool_call 与 text/thinking 一样通过 supervisor 和前端 WebSocket 流动。
                # [AutoC 2026-06-17] Why: text/thinking buffers flush by time/size, while
                # tool deltas are emitted immediately. If a provider emits reasoning/text
                # and then a tool call within the buffer window, the Web UI receives the
                # tool block first and the preceding stream block only at stream end. How:
                # flush both stream buffers immediately before every provider tool delta.
                # Purpose: the visible block order follows provider order: thinking/text
                # before the tool that was decided after them.
                await text_buf.flush()
                await think_buf.flush()
                event_payload = dict(payload or {})
                event_payload.setdefault("node_id", ls.node.id)
                event_payload.setdefault("task_id", ls.rctx.task_id)
                event_payload.setdefault("llm_request_id", request_id)
                await ls.rctx.emit_event("tool_call_delta", event_payload)

            stream_task = asyncio.create_task(
                ls.provider.chat_stream(
                    messages=deepcopy(llm_messages),
                    tools=tools_arg,
                    tool_mode=tool_mode,
                    on_text=text_buf.push,
                    on_thinking=think_buf.push,
                    on_tool_delta=_emit_tool_delta,
                )
            )
            try:
                while True:
                    done, _ = await asyncio.wait({stream_task}, timeout=0.3)
                    if stream_task in done:
                        resp = stream_task.result()
                        break
                    if await ls.rctx.check_cancelled():
                        stream_task.cancel()
                        try:
                            await stream_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        await text_buf.flush()
                        await think_buf.flush()
                        if text_buf.flushed_any or think_buf.flushed_any:
                            # [refactor 2026-04-18] has_thinking → has_reasoning 事件字段对齐
                            await ls.rctx.emit_event("stream_end", {"node_id": ls.node.id, "llm_request_id": request_id, "has_text": text_buf.flushed_any, "has_reasoning": think_buf.flushed_any})
                        await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})
                        # Phase 1: Signal — 取消时也发射 end 信号
                        _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
                        _bus.emit(Signal(name="llm.call.end", payload={**_sig_payload, "elapsed_ms": _elapsed, "cancelled": True}, span_id=_span_id))
                        # [硬取消-场景2] 流式输出中取消：不将未完成的 assistant 消息存入 history。
                        # 调用方 ai_step.py 收到 TaskAction 后直接 return，
                        # 不执行 messages.append / _shadow_write，history 停留在上一轮完整状态。
                        return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id)
                    if (not ls.preempt_after_step and ls.preempt_inject_info is None
                            and not (ls.plugin_data.get("preempt") or {}).get("defer_for_output_reject")):
                        _pi_s = await ls.rctx.check_preempted()
                        if _pi_s.get("preempted"):
                            if _has_injectable_preempt(_pi_s):
                                ls.preempt_inject_info = _pi_s
                                # -------------------------------------------------------
                                # Preempt V3 需求1: 思考阶段截断
                                # 如果 LLM 尚未产出任何 text token（仍在 thinking 阶段
                                # 或尚未开始输出），立即截断流式输出，丢弃这条不完整的
                                # assistant message（不存历史），让主循环在下一轮迭代
                                # 由 _inject_preempt_message 注入 preempt 消息后重新推理。
                                # text 阶段已开始输出时，维持现行等待逻辑不变（等流结束）。
                                # 与 cancel 的区别：break 后不终止 task，回到主循环继续。
                                # -------------------------------------------------------
                                _in_thinking_phase = not text_buf.flushed_any and text_buf.is_empty
                                if _in_thinking_phase:
                                    stream_task.cancel()
                                    try:
                                        await stream_task
                                    except (asyncio.CancelledError, Exception):
                                        pass
                                    # 清理 buffer 状态：flush 残留的 thinking tokens
                                    await text_buf.flush()
                                    await think_buf.flush()
                                    if text_buf.flushed_any or think_buf.flushed_any:
                                        await ls.rctx.emit_event("stream_end", {
                                            "node_id": ls.node.id,
                                            "llm_request_id": request_id,
                                            "has_text": text_buf.flushed_any,
                                            "has_reasoning": think_buf.flushed_any,
                                        })
                                    await ls.rctx.emit_event("preempt_thinking_truncated", {
                                        "node_id": ls.node.id,
                                        "task_id": ls.rctx.task_id,
                                        "step": step,
                                    })
                                    # Signal System: 发射 llm.call.end 标记截断
                                    _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
                                    _bus.emit(Signal(name="llm.call.end", payload={
                                        **_sig_payload, "elapsed_ms": _elapsed,
                                        "preempt_truncated": True,
                                    }, span_id=_span_id))
                                    # 返回 None 通知 ai_step 跳过本次响应处理，
                                    # 直接 continue 到主循环顶部重新推理
                                    return None
                            else:
                                ls.preempt_after_step = True
            finally:
                # Worker cancellation must also stop a streaming alternate request.
                if not stream_task.done():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, Exception):
                        pass
            await text_buf.flush()
            await think_buf.flush()
            # [thinking-time 2026-06-01] Record precise reasoning timing on LoopState.
            # reasoning_started = first thinking token; reasoning_ended = first text token
            # (thinking always precedes text in all providers).
            if think_buf.flushed_any:
                ls._reasoning_started_iso = think_buf.first_push_iso
                if text_buf.first_push_at is not None:
                    ls._reasoning_ended_iso = text_buf.first_push_iso
                else:
                    # No text output — thinking lasted until stream end
                    import datetime as _dt_rc
                    ls._reasoning_ended_iso = _dt_rc.datetime.now(_dt_rc.timezone.utc).isoformat()
            if text_buf.flushed_any or think_buf.flushed_any:
                # [refactor 2026-04-18] has_thinking → has_reasoning 事件字段对齐
                await ls.rctx.emit_event("stream_end", {
                    "node_id": ls.node.id,
                    "llm_request_id": request_id,
                    "has_text": text_buf.flushed_any,
                    "has_reasoning": think_buf.flushed_any,
                })
        else:
            # ---- 非流式调用（可取消） ----
            llm_task = asyncio.create_task(
                ls.provider.chat(messages=deepcopy(llm_messages), tools=tools_arg, tool_mode=tool_mode)
            )
            try:
                while True:
                    done, _ = await asyncio.wait({llm_task}, timeout=0.3)
                    if llm_task in done:
                        resp = llm_task.result()
                        break
                    if await ls.rctx.check_cancelled():
                        await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})
                        _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
                        _bus.emit(Signal(name="llm.call.end", payload={**_sig_payload, "elapsed_ms": _elapsed, "cancelled": True}, span_id=_span_id))
                        return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id)
                    if (not ls.preempt_after_step and ls.preempt_inject_info is None
                            and not (ls.plugin_data.get("preempt") or {}).get("defer_for_output_reject")):
                        _pi_n = await ls.rctx.check_preempted()
                        if _pi_n.get("preempted"):
                            if _has_injectable_preempt(_pi_n):
                                ls.preempt_inject_info = _pi_n
                            else:
                                ls.preempt_after_step = True
            finally:
                # Also cancel the transport when the worker task itself is cancelled.
                if not llm_task.done():
                    llm_task.cancel()
                    try:
                        await llm_task
                    except (asyncio.CancelledError, Exception):
                        pass

        assert resp is not None

        # ---- 提取 token usage ----
        if resp.usage and isinstance(resp.usage.get("prompt_tokens"), int):
            ls.last_prompt_tokens = resp.usage["prompt_tokens"]
            ls.last_usage = dict(resp.usage)
            if ls.compact_threshold > 0 and resp.usage["prompt_tokens"] <= ls.compact_threshold:
                ls.compacted = False
            await ls.rctx.emit_event("context_usage", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                # [AutoC 2026-08-24] 附带 provider 名：前端消息卡片的元信息按钮
                # 需要实时显示渠道，历史路径有 JSONL 的 provider 字段，实时路径
                # 此前缺失。
                "provider": getattr(ls.provider, "name", "") or "",
                "usage": resp.usage,
            })

        # ---- 重试判定 ----
        _retry_reason = ""
        if not resp.ok and _is_retryable_error(resp):
            _retry_reason = resp.error or "unknown"
        elif resp.ok and not resp.tool_calls and not (resp.text or "").strip():
            _retry_reason = "empty_response"

        if _retry_reason and _retry_attempt < ls.retry_max:
            _retry_attempt += 1
            _delay = min(
                ls.retry_initial_delay * (ls.retry_backoff ** (_retry_attempt - 1)),
                ls.retry_max_delay,
            )
            await ls.rctx.emit_event("llm_retry", {
                "node_id": ls.node.id,
                "step": step,
                "attempt": _retry_attempt,
                "max_retries": ls.retry_max,
                "delay_sec": round(_delay, 2),
                "error": _retry_reason,
                "status_code": resp.status_code,
            })
            # Phase 1: Signal — 发射重试信号（与现有 emit_event 并行）
            _bus.emit(Signal(
                name="llm.retry",
                payload={**_sig_payload, "attempt": _retry_attempt, "error": _retry_reason,
                         "error_type": "retryable", "status_code": resp.status_code},
                span_id=_span_id,
            ))
            _waited = 0.0
            while _waited < _delay:
                _sleep_step = min(0.5, _delay - _waited)
                await asyncio.sleep(_sleep_step)
                _waited += _sleep_step
                if await ls.rctx.check_cancelled():
                    await ls.rctx.emit_event("cancel_acknowledged", {
                        "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
                    })
                    # Phase 1: Signal — 重试等待期间取消，发射 end 信号
                    _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
                    _bus.emit(Signal(name="llm.call.end", payload={**_sig_payload, "elapsed_ms": _elapsed, "cancelled": True}, span_id=_span_id))
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id)
                if (not ls.preempt_after_step and ls.preempt_inject_info is None
                        and not (ls.plugin_data.get("preempt") or {}).get("defer_for_output_reject")):
                    _pi_r = await ls.rctx.check_preempted()
                    if _pi_r.get("preempted"):
                        if _has_injectable_preempt(_pi_r):
                            ls.preempt_inject_info = _pi_r
                        else:
                            ls.preempt_after_step = True
            resp = None
            continue  # 重试

        # empty_response retries exhausted → mark as failed so
        # fallback_provider can pick it up instead of looping forever.
        if _retry_reason == "empty_response":
            resp.ok = False
            resp.error = "empty response from provider"

        # ---------------------------------------------------------------
        # P5a Reactive Compact: 检测 "request too long" 类错误时，剥离旧
        # tool_result 内容并重试一次。目的是在 413 / context_length_exceeded
        # 等不可重试错误触发时提供一层安全网，而不是直接报错给用户。
        # 只尝试一次（_reactive_compact_done 标记），避免无限循环。
        # ---------------------------------------------------------------
        if not resp.ok and not _is_retryable_error(resp):
            _err_text = (resp.error or "").lower()
            _is_too_long = (
                resp.status_code == 413
                or "too long" in _err_text
                or "too large" in _err_text
                or "context_length" in _err_text
                or "max_tokens" in _err_text
                or "maximum context" in _err_text
                or "token limit" in _err_text
            )
            if _is_too_long and not getattr(ls, '_reactive_compact_done', False):
                ls._reactive_compact_done = True  # 只尝试一次
                # 从尾部往前找 tool_result，保留最近 3 个，清除其余
                from engine.compact import microcompact_messages
                _, _rc_cleared = microcompact_messages(
                    ls.messages, gap_minutes=0, keep_recent=3, min_tool_results=1,
                )
                if _rc_cleared:
                    await ls.rctx.emit_event("reactive_compact", {
                        "node_id": ls.node.id, "step": step,
                        "cleared": _rc_cleared, "error": resp.error or "",
                    })
                    llm_messages, action = await prepare_request_history(
                        ls.messages, node=ls.node, provider=ls.provider, rctx=ls.rctx, step=step,
                    )
                    if action is not None:
                        return action
                    ls.request_history = llm_messages
                    resp = None
                    continue  # 重试

        break  # 成功或不可重试，退出重试循环

    # Phase 1: Signal — 循环结束，发射 llm.call.end 信号
    # 此时 resp 可能是成功响应，也可能是不可重试的失败响应
    _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
    _end_payload = {**_sig_payload, "elapsed_ms": _elapsed, "attempt": _retry_attempt + 1, "ok": resp.ok}
    if not resp.ok:
        _end_payload["error"] = resp.error or "unknown"
        # Phase 1: Signal — 不可重试错误，额外发射 llm.error 信号
        _bus.emit(Signal(
            name="llm.error",
            payload={**_sig_payload, "error": resp.error or "unknown", "retryable": False,
                     "status_code": resp.status_code},
            span_id=_span_id,
        ))
    _bus.emit(Signal(name="llm.call.end", payload=_end_payload, span_id=_span_id))

    return resp


def _build_failure_action(ls: _LoopState, resp, step: int, retry_attempt: int = 0) -> TaskAction:
    """LLM 调用失败（重试耗尽），构建 FAIL action。"""
    _fail_msg = resp.error or "LLM call failed"
    if retry_attempt > 0:
        _fail_msg = f"{_fail_msg} (retried {retry_attempt} times)"
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=_fail_msg,
        context_ref=ctx_ref,
    )
