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

        if ls.preempt_after_step:
            ctx_ref = _persist_ctx(ls, ctx.step + 1)
            return hook_result(action=TaskAction(
                action=ACTION_PREEMPTED,
                node_id=ls.node.id,
                context_ref=ctx_ref,
                summary="任务被软打断，上下文已保存。",
            ))

        if getattr(ls.node, "output_mode", "tool_only") == "hybrid":
            return hook_result(action=_build_implicit_finish(ctx, ls, resp, text))

        ls.plaintext_retry_count += 1
        if ls.plaintext_retry_count <= ls.plaintext_retry_max:
            retry_hint = ls.formatter.build_retry_hint()
            ls.messages.append({
                "role": "user",
                "content": retry_hint,
                "_retry_hint": True,
            })
            ls.use_stream = ls.streaming
            return hook_result(modified=True)

        ctx_ref = _persist_ctx(ls, ctx.step + 1)
        return hook_result(action=TaskAction(
            action=ACTION_FAIL,
            node_id=ls.node.id,
            error=f"模型未使用 finish 工具，裸文本不被内核认可为合法结束。原始文本: {_short(text, 200)}",
            context_ref=ctx_ref,
            summary="plaintext_without_finish",
        ))


def _build_implicit_finish(ctx: Any, ls: Any, resp: Any, text: str) -> TaskAction:
    """Build the same implicit finish action used by hybrid output mode."""
    from engine.inference.ai_step import _shadow_write

    assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
    provider_name = getattr(ls.provider, "name", "") or "unknown"
    implicit_meta = MessageMeta(
        provider=provider_name,
        tool_mode=getattr(ls.node, "tool_mode", "fake-native"),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
        tool_call_ids=[],
        reasoning="",
        has_reasoning=False,
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
            "attachments": list(ls.tool_produced_attachments),
            "implicit_finish": True,
        },
        context_ref=ctx_ref,
        summary=_short(text, 240),
    )
