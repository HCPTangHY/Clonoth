from __future__ import annotations

import logging
from typing import Any

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result

logger = logging.getLogger(__name__)

_REJECT_MESSAGE_TEMPLATE = (
    "\u274c REJECTED: {tool}() cannot be called alongside other tools "
    "(except intermediate_reply). Execute your other tools first, wait for their "
    "results, then call {tool}() alone in a separate turn."
)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "FinishGuardHandler",
    "hook_points": [
        ("before_tool_call", "handle"),
    ],
    "priority": 100,
}


_EXCLUSIVE_REJECT_TEMPLATE = (
    "\u274c REJECTED: {tool}() must be called alone in its own turn. "
    "Execute your other tools first, wait for their results, then call {tool}() separately."
)


class FinishGuardHandler:
    """Reject terminal finish()/ask() and exclusive control tools when colocated."""

    name = "finish_guard"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        """Apply the terminal finish/ask and exclusive compact_context/switch_node colocation guard.

        Why: finish and ask terminate the task, so other same-turn tool results
        would never be read by the model; compact_context and switch_node exit
        the step immediately in _handle_pseudo_tool, which would skip other
        same-turn tools and leave unpaired tool_calls. How: prefer ai_step's
        already-filtered legacy pseudo/real call lists when present, and
        otherwise fall back to raw ctx.tool_calls for isolated tests.
        """
        terminal_tools = {"finish", "ask"}
        exclusive_tools = {"compact_context", "switch_node"}
        reply_tools = {"intermediate_reply", "reply"}
        pseudo_calls = ctx.extra.get("pseudo_calls")
        real_tool_calls = ctx.extra.get("real_tool_calls")

        if pseudo_calls is not None or real_tool_calls is not None:
            pseudo_list = list(pseudo_calls or [])
            real_list = list(real_tool_calls or [])
            unauth_count = int(ctx.extra.get("unauthorized_count") or 0)
            total_calls = len(pseudo_list) + len(real_list) + unauth_count
            exclusive_name = next(
                (_tool_name(call) for call in pseudo_list if _tool_name(call) in exclusive_tools),
                "",
            )
            if exclusive_name and total_calls > 1:
                logger.warning(
                    "Rejected %s + other tools in same turn (node=%s, step=%d, tools=%s)",
                    exclusive_name,
                    getattr(ctx.node, "id", ""),
                    ctx.step,
                    [_tool_name(call) for call in (ctx.tool_calls or [])],
                )
                return hook_result(
                    block=True,
                    reason=f"{exclusive_name}_colocated",
                    error_message=_EXCLUSIVE_REJECT_TEMPLATE.format(tool=exclusive_name),
                )
            terminal_name = next(
                (_tool_name(call) for call in pseudo_list if _tool_name(call) in terminal_tools),
                "",
            )
            has_terminal = bool(terminal_name)
            has_non_reply_others = bool(real_list) or (unauth_count > 0) or any(
                _tool_name(call) not in (*terminal_tools, *reply_tools) for call in pseudo_list
            )
        else:
            calls = list(ctx.tool_calls or [])
            exclusive_name = next(
                (_tool_name(call) for call in calls if _tool_name(call) in exclusive_tools),
                "",
            )
            if exclusive_name and len(calls) > 1:
                logger.warning(
                    "Rejected %s + other tools in same turn (node=%s, step=%d, tools=%s)",
                    exclusive_name,
                    getattr(ctx.node, "id", ""),
                    ctx.step,
                    [_tool_name(call) for call in calls],
                )
                return hook_result(
                    block=True,
                    reason=f"{exclusive_name}_colocated",
                    error_message=_EXCLUSIVE_REJECT_TEMPLATE.format(tool=exclusive_name),
                )
            terminal_name = next(
                (_tool_name(call) for call in calls if _tool_name(call) in terminal_tools),
                "",
            )
            has_terminal = bool(terminal_name)
            has_non_reply_others = any(
                _tool_name(call) not in (*terminal_tools, *reply_tools) for call in calls
            )

        if not (has_terminal and has_non_reply_others):
            return None

        # [AutoC 2026-05-31] Why: ask is terminal like finish, so the same
        # colocated-tool rejection must name the offending terminal tool. How: use
        # a shared template populated from the detected pseudo call. Purpose: keep
        # model-facing retry guidance precise for both finish and ask.
        logger.warning(
            "Rejected %s + other tools in same turn (node=%s, step=%d, tools=%s)",
            terminal_name or "terminal",
            getattr(ctx.node, "id", ""),
            ctx.step,
            [_tool_name(call) for call in (ctx.tool_calls or [])],
        )
        return hook_result(
            block=True,
            reason=f"{terminal_name or 'terminal'}_colocated",
            error_message=_REJECT_MESSAGE_TEMPLATE.format(tool=terminal_name or "finish"),
        )


def _tool_name(call: Any) -> str:
    """Read a tool-call name from object or dict shapes.

    Why: ai_step and tests may pass Provider ToolCall objects, ParsedToolCall
    objects, or dicts. How: support attribute and dict access. Purpose: keep the
    handler decoupled from one formatter implementation.
    """
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")
