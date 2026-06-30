from __future__ import annotations

import inspect
from typing import Any

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "ApprovalHandler",
    "hook_points": [
        ("before_tool_call", "handle"),
    ],
    "priority": 90,
}


class ApprovalHandler:
    """Delegate before-tool approval decisions to RunContext when available."""

    name = "approval"
    priority = 90

    async def handle(self, ctx: Any) -> Any | None:
        """Check whether the current tool call should be blocked for approval.

        Why: current guarded tools still perform approval inside ToolContext, but
        the hook system needs a forward-compatible place for RunContext-level
        approval checks. How: call a RunContext approval method if one exists and
        normalize common return shapes. Purpose: add the handler without changing
        current tool-layer approval behavior.
        """
        if ctx.tool_call is None or ctx.rctx is None:
            return None

        checker = _approval_checker(ctx.rctx)
        if checker is None:
            return None

        decision = await _call_checker(checker, ctx)
        return _decision_to_result(decision)


def _approval_checker(rctx: Any) -> Any | None:
    """Find a supported RunContext approval interface.

    Why: the existing RunContext does not yet expose this API, while future code
    or tests can. How: look for explicit method names in priority order. Purpose:
    avoid coupling the hook to supervisor HTTP details or duplicating tool-layer
    approval requests.
    """
    for name in ("check_tool_approval", "_check_approval", "check_approval", "before_tool_call_approval"):
        checker = getattr(rctx, name, None)
        if checker is not None:
            return checker
    return None


async def _call_checker(checker: Any, ctx: Any) -> Any:
    """Call a RunContext checker with broad compatibility."""
    kwargs = {
        "tool_call": ctx.tool_call,
        "tool_calls": ctx.tool_calls,
        "messages": ctx.messages,
        "tools": ctx.tools,
        "node": ctx.node,
        "provider": ctx.provider,
        "step": ctx.step,
        "response": ctx.response,
    }
    try:
        decision = checker(**kwargs)
    except TypeError:
        # Why: older or test-only checkers may accept only the tool call. How:
        # retry with a single positional argument. Purpose: keep the handler easy
        # to adopt without forcing one exact signature immediately.
        decision = checker(ctx.tool_call)
    if inspect.isawaitable(decision):
        return await decision
    return decision


def _looks_like_hook_result(value: Any) -> bool:
    """Return whether a value already has the hook-result runtime shape.

    Why: this relocated handler cannot depend on the concrete hook result class.
    How: use duck typing for the attributes read by HookRegistry. Purpose: preserve
    compatibility with real HookResult instances without reintroducing the import.
    """
    return all(hasattr(value, attr) for attr in ("block", "skip_step", "action", "modified"))


def _decision_to_result(decision: Any) -> Any | None:
    """Normalize approval checker return values into HookResult.

    Why: approval APIs may return booleans, dicts, or HookResult directly. How:
    treat explicit pending/required/denied shapes as blocking and all allowed or
    empty results as no intervention. Purpose: isolate ai_step from approval API
    shape changes during the hook migration.
    """
    if decision is None or decision is False:
        return None
    if _looks_like_hook_result(decision):
        return decision
    if decision is True:
        return hook_result(block=True, reason="等待审批")
    if isinstance(decision, dict):
        status = str(decision.get("status") or decision.get("safety_level") or "").lower()
        needs_approval = bool(
            decision.get("requires_approval")
            or decision.get("approval_required")
            or status in {"pending", "waiting", "approval_required"}
        )
        if needs_approval:
            return hook_result(
                block=True,
                reason="等待审批",
                error_message=str(decision.get("error_message") or decision.get("reason") or ""),
            )
        if status in {"denied", "deny", "rejected"}:
            return hook_result(
                block=True,
                reason=str(decision.get("reason") or "approval_denied"),
                error_message=str(decision.get("error_message") or decision.get("reason") or "Approval denied."),
            )
    return None
