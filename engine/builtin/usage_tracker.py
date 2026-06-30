from __future__ import annotations

from typing import Any
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "UsageTracker",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": 0,
}


class UsageTracker:
    """Accumulate provider token usage after each LLM call."""

    name = "usage_tracker"
    priority = 0

    async def handle(self, ctx: Any) -> Any | None:
        """Move response.usage into RunContext.total_usage.

        Why: ai_step.py should not own bookkeeping that can live in an after-call
        hook. How: read the ProviderResponse usage dict and add known token keys
        to rctx.total_usage. Purpose: keep task-level usage records identical
        while making usage tracking pluggable.
        """
        usage = getattr(ctx.response, "usage", None)
        if not usage or not isinstance(usage, dict) or ctx.rctx is None:
            return None

        modified = False
        for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if usage_key in usage:
                ctx.rctx.total_usage[usage_key] = ctx.rctx.total_usage.get(usage_key, 0) + usage[usage_key]
                modified = True
        if modified:
            return hook_result(modified=True)
        return None
