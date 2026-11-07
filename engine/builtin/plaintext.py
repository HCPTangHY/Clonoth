from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from engine.conversation_store import MessageType
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.loop_state import _persist_ctx, _short
from engine.inference.message_model import MessageMeta, set_message_meta
from engine.protocol import ACTION_FAIL, ACTION_FINISH, ACTION_PREEMPTED, TaskAction


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "PlaintextRetryHandler",
    "hook_points": [
        ("before_response", "handle"),
    ],
    "priority": 0,
}


class PlaintextRetryHandler:
    """Handle model responses that contain no tool calls."""

    name = "plaintext_retry"
    priority = 0

    async def handle(self, ctx: Any) -> Any | None:
        """Apply legacy hybrid and tool-only plaintext behavior.

        Why: plaintext handling was hard-coded after each LLM response. How: use
        the loop state and ProviderResponse in HookContext to retry, fail, or
        create an implicit finish action. Purpose: preserve output-mode behavior
        while moving it to before_response.
        """
        ls = ctx.extra.get("loop_state")
        resp = ctx.response
        if ls is None or resp is None:
            return None

        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            return None

        from .preempt import is_preempt_deferred_for_output_reject, is_user_entry_task

        if getattr(ls.node, "output_mode", "tool_only") == "hybrid" and (
            is_user_entry_task(ls) or is_preempt_deferred_for_output_reject(ls)
        ):
            return hook_result(action=_build_implicit_finish(ctx, ls, resp, text))

        if not ls.preempt_after_step and ls.preempt_inject_info is None:
            try:
                _pi = await ls.rctx.check_preempted()
            except Exception:
                _pi = {}
            if isinstance(_pi, dict) and _pi.get("preempted"):
                if _pi.get("message") or _pi.get("attachments"):
                    ls.preempt_inject_info = _pi
                else:
                    ls.preempt_after_step = True

        if ls.preempt_inject_info is not None and getattr(ls.node, "output_mode", "tool_only") == "hybrid":
            from engine.inference.ai_step import _shadow_write

            _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
            provider_name = (getattr(ls.rctx, "response_info", {}) or {}).get("provider") or getattr(ls.provider, "name", "") or "unknown"
            provider_meta = getattr(resp, "provider_meta", None)
            reasoning_text = getattr(resp, "reasoning", None) or ""
            draft_meta = MessageMeta(
                provider=provider_name,
                tool_mode=getattr(ls.node, "tool_mode", "fake-native"),
                message_type="assistant",
                timestamp=datetime.now(timezone.utc).isoformat(),
                llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
                metadata={provider_name: provider_meta} if provider_meta else {},
                tool_call_ids=[],
                reasoning=reasoning_text,
                usage=dict(ls.last_usage) if ls.last_usage else {},
            )
            set_message_meta(_assistant_msg, draft_meta)
            ls.messages.append(_assistant_msg)
            _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
            })
            ls.use_stream = ls.streaming
            return hook_result(modified=True)

        if ls.preempt_after_step:
            ctx_ref = _persist_ctx(ls, ctx.step + 1)
            return hook_result(action=TaskAction(
                action=ACTION_PREEMPTED,
                node_id=ls.node.id,
                context_ref=ctx_ref,
            ))

        if getattr(ls.node, "output_mode", "tool_only") == "hybrid":
            return hook_result(action=_build_implicit_finish(ctx, ls, resp, text))

        ls.plaintext_retry_count += 1
        if ls.plaintext_retry_count <= ls.plaintext_retry_max:
            from engine.inference.ai_step import _shadow_write

            _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
            provider_name = (getattr(ls.rctx, "response_info", {}) or {}).get("provider") or getattr(ls.provider, "name", "") or "unknown"
            provider_meta = getattr(resp, "provider_meta", None)
            reasoning_text = getattr(resp, "reasoning", None) or ""
            draft_meta = MessageMeta(
                provider=provider_name,
                tool_mode=getattr(ls.node, "tool_mode", "fake-native"),
                message_type="assistant",
                timestamp=datetime.now(timezone.utc).isoformat(),
                llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
                metadata={provider_name: provider_meta} if provider_meta else {},
                tool_call_ids=[],
                reasoning=reasoning_text,
                usage=dict(ls.last_usage) if ls.last_usage else {},
            )
            set_message_meta(_assistant_msg, draft_meta)
            _assistant_msg.setdefault("_meta", {}).update({
                "rejected_draft": True,
                "rejection_source": "plaintext_retry",
                "rejection_code": "plaintext_not_allowed",
                "source_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
            })
            ls.messages.append(_assistant_msg)
            _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

            retry_hint = ls.formatter.build_retry_hint()
            _retry_msg = {
                "role": "user",
                "content": retry_hint,
                "_retry_hint": True,
                "message_type": "retry_hint",
                "_meta": {
                    "message_type": "retry_hint",
                    "is_rejection": True,
                    "rejection_source": "plaintext_retry",
                    "rejection_code": "plaintext_not_allowed",
                    "source_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
                },
            }
            ls.messages.append(_retry_msg)
            _shadow_write(ls, _retry_msg, "retry_hint")
            ls.use_stream = ls.streaming
            return hook_result(modified=True)

        ctx_ref = _persist_ctx(ls, ctx.step + 1)
        return hook_result(action=TaskAction(
            action=ACTION_FAIL,
            node_id=ls.node.id,
            error=f"The model did not use the finish tool; bare text is not accepted by the kernel as a valid ending. Raw text: {_short(text, 200)}",
            context_ref=ctx_ref,
        ))


def _build_implicit_finish(ctx: Any, ls: Any, resp: Any, text: str) -> TaskAction:
    """Build the same implicit finish action used by hybrid output mode."""
    from engine.inference.ai_step import _shadow_write

    assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
    provider_name = (getattr(ls.rctx, "response_info", {}) or {}).get("provider") or getattr(ls.provider, "name", "") or "unknown"
    provider_meta = getattr(resp, "provider_meta", None)
    reasoning_text = getattr(resp, "reasoning", None) or ""
    implicit_meta = MessageMeta(
        provider=provider_name,
        tool_mode=getattr(ls.node, "tool_mode", "fake-native"),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        metadata={provider_name: provider_meta} if provider_meta else {},
        tool_call_ids=[],
        reasoning=reasoning_text,
        usage=dict(ls.last_usage) if ls.last_usage else {},
    )
    set_message_meta(assistant_msg, implicit_meta)
    ls.messages.append(assistant_msg)
    _shadow_write(ls, assistant_msg, MessageType.ASSISTANT)

    ctx_ref = _persist_ctx(ls, ctx.step + 1)
    return TaskAction(
        action=ACTION_FINISH,
        node_id=ls.node.id,
        result={
            "text": text,
            "attachments": [],  # [AutoC 2026-08-06] 隐式 finish 不自动带附件，附件通过 intermediate_reply 显式传递
            "implicit_finish": True,
        },
        context_ref=ctx_ref,
    )
