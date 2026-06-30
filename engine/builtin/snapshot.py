from __future__ import annotations

from typing import Any
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.loop_state import _persist_ctx


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "ContextSnapshotSaver",
    "hook_points": [
        ("on_task_end", "handle"),
        ("on_task_error", "handle"),
    ],
    "priority": 0,
}


class ContextSnapshotSaver:
    """Save loop context snapshots for task end or error hook points."""

    name = "context_snapshot_saver"
    priority = 0

    async def handle(self, ctx: Any) -> Any | None:
        """Persist the loop state and store the context ref in ctx.extra.

        Why: snapshot persistence is currently embedded in finish, fail,
        preempt, and compact paths. How: when a caller provides loop_state and a
        step_count, delegate to _persist_ctx and expose the returned ref. Purpose:
        provide the on_task_end/on_task_error handler without changing the many
        existing terminal paths until a safer follow-up refactor.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None
        step_count = int(ctx.extra.get("step_count", ctx.step) or ctx.step)
        ctx.extra["context_ref"] = _persist_ctx(ls, step_count)
        # Why: _persist_ctx can legitimately return an empty string in child-session
        # or main ConversationStore mode. How: record an explicit flag as well as
        # the returned ref. Purpose: callers can distinguish “snapshot attempted”
        # from “handler did not run” without duplicating persistence work.
        ctx.extra["snapshot_saved"] = True
        return hook_result(modified=True)
