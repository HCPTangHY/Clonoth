"""LLM 调用（含重试逻辑）。

从 ai_step.py 抽出。依赖 _LoopState、_StreamBuffer。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .stream_buffer import _StreamBuffer
from ..attachments import prepare_messages_for_llm
from ..protocol import TaskAction, ACTION_CANCELLED, ACTION_FAIL
from .loop_state import _LoopState, _persist_ctx, _short
# message_to_llm 反序列化：在发送 LLM 前做格式转换（role 修正 + 内部字段剥离）
from .tool_format import build_llm_messages, sanitize_control_tool_history
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


# [fix 2026-05-28] Why: some upstream providers (e.g. zoaholic proxies) return
# HTTP 200 but embed server-side errors (429, 503, overloaded, capacity) inside
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


def _build_messages_for_provider(
    messages: list[dict[str, Any]],
    formatter: Any,
    provider: Any,
) -> list[dict[str, Any]]:
    """Build the message view that should be handed to the provider.

    [2026-05-01] OpenAI Responses native tools need the L1 storage view: the
    assistant message still has Clonoth's structured ``tool_calls`` field and
    provider metadata.  FakeNativeToolFormatter would collapse that data into
    user text, and true NativeToolFormatter would strip provider metadata.
    Therefore the Responses provider bypasses L2 for both native and fake-native
    modes, then performs its own final API conversion.  Other providers keep
    the existing L2 formatter path.
    """
    provider_name = getattr(provider, "name", "") or ""
    formatter_mode = getattr(formatter, "mode", "") if formatter else ""
    # [2026-05-01] Responses conversion needs storage-level tool_calls and _meta.
    # Bypass both true native and fake-native L2; fake-native would text-collapse,
    # while true native would strip provider metadata needed for raw_output replay.
    # Gemini also needs _meta for raw_parts (thoughtSignature + functionCall round-trip)
    if provider_name in {"openai-responses", "gemini"} and formatter_mode in {"native", "fake-native"}:
        # Keep storage-level tool_calls/_meta for the provider, but match
        # build_llm_messages by dropping ephemeral retry/control messages.
        # [2026-05-07] 旁路 provider 也必须执行控制流历史清洗。
        # 原因：Gemini/Responses 为保留 provider meta 绕过 L2，若不在这里清理会继续回放 finish。
        # 做法：先用 L1 形态清洗 finish tool_call/tool_result，再保留 _meta 交给 provider。
        # 目的：不破坏普通工具配对，同时防止控制流伪工具进入原生 provider 历史。
        return [dict(msg) for msg in sanitize_control_tool_history(messages) if not msg.get("_ephemeral")]
    return build_llm_messages(messages, formatter) if formatter else messages


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
    # 反序列化方向：先用 build_llm_messages 做格式转换（修正跨模式 role、剥离 _meta 等内部字段），
    # 再用 prepare_messages_for_llm 处理图片 file:// → base64 解析。
    # build_llm_messages 会跳过 _ephemeral 消息（retry hint 等），但保留 _dynamic（动态上下文）。
    # 注意：不能修改 ls.messages 本身，它是运行时状态。
    _formatted = _build_messages_for_provider(ls.messages, ls.formatter, ls.provider)
    llm_messages = prepare_messages_for_llm(_formatted, ls.rctx.workspace_root)

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
        # [AutoC 2026-06-04] Why: retries and multi-step tasks can issue several
        # provider requests under one task id. How: create a fresh request id at the
        # start of each actual provider call attempt, before any stream delta can be
        # emitted. Purpose: all realtime blocks from this attempt share one card key.
        request_id = ls.rctx.begin_llm_request()
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
                    messages=llm_messages,
                    tools=tools_arg,
                    on_text=text_buf.push,
                    on_thinking=think_buf.push,
                    on_tool_delta=_emit_tool_delta,
                )
            )
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
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_s = await ls.rctx.check_preempted()
                    if _pi_s.get("preempted"):
                        if _pi_s.get("message"):
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
                ls.provider.chat(messages=llm_messages, tools=tools_arg)
            )
            while True:
                done, _ = await asyncio.wait({llm_task}, timeout=0.3)
                if llm_task in done:
                    resp = llm_task.result()
                    break
                if await ls.rctx.check_cancelled():
                    llm_task.cancel()
                    try:
                        await llm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})
                    # Phase 1: Signal — 取消时也发射 end 信号
                    _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
                    _bus.emit(Signal(name="llm.call.end", payload={**_sig_payload, "elapsed_ms": _elapsed, "cancelled": True}, span_id=_span_id))
                    # [硬取消-场景2] 非流式调用中取消：同上，不存盘未完成的 assistant 消息。
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_n = await ls.rctx.check_preempted()
                    if _pi_n.get("preempted"):
                        if _pi_n.get("message"):
                            ls.preempt_inject_info = _pi_n
                        else:
                            ls.preempt_after_step = True

        assert resp is not None

        # ---- 提取 token usage ----
        if resp.usage and isinstance(resp.usage.get("prompt_tokens"), int):
            ls.last_prompt_tokens = resp.usage["prompt_tokens"]
            ls.last_usage = dict(resp.usage)
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
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_r = await ls.rctx.check_preempted()
                    if _pi_r.get("preempted"):
                        if _pi_r.get("message"):
                            ls.preempt_inject_info = _pi_r
                        else:
                            ls.preempt_after_step = True
            resp = None
            continue  # 重试

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
                    # 重建 llm_messages：microcompact 原地修改了 ls.messages，
                    # 需要重新走 build_llm_messages + prepare 流程
                    _formatted = _build_messages_for_provider(ls.messages, ls.formatter, ls.provider)
                    llm_messages = prepare_messages_for_llm(_formatted, ls.rctx.workspace_root)
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
    _fail_msg = resp.error or "LLM 调用失败"
    if retry_attempt > 0:
        _fail_msg = f"{_fail_msg} (已重试 {retry_attempt} 次)"
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=_fail_msg,
        context_ref=ctx_ref,
        summary=_short(_fail_msg, 240),
    )
