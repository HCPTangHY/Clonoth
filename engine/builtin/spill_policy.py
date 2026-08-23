from __future__ import annotations

from typing import Any

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from ..tool_step import (
    artifact_enabled,
    get_tool_inline_limit,
    get_tool_step_inline_budget,
    truncate_tool_result,
    write_artifact,
)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "SpillPolicy",
    "hook_points": [
        ("after_tool_call", "handle"),
    ],
    "priority": 50,
    "description": "Bound tool-result inline text: per-tool limits, per-step shared budget, artifact spill.",
}


class SpillPolicy:
    """Apply the post-execution spill policy for synchronous real tool results.

    Why: how large a tool result may enter the prompt is policy, not loop
    mechanics. How: run after each synchronous tool execution, resolve the
    per-tool inline limit, shrink it against the shared per-step budget, write
    the full text to a task artifact when it exceeds the limit, and return a
    bounded preview plus truncation metadata through result_override. Purpose:
    move the spill strategy out of ai_step.py so alternative policies can be
    mounted without touching the inference loop.

    The per-step budget arithmetic is read here but written by the loop: the
    handler only reads step_inline_state["used"] to compute this call's limit,
    and the loop adds the final inline length after applying the override.
    Single-writer accounting keeps concurrent handlers from double counting.
    """

    name = "spill_policy"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        raw = ctx.extra.get("raw_inline")
        if not isinstance(raw, str):
            return None
        ls = ctx.extra.get("loop_state")
        runtime_cfg = getattr(ls, "runtime_cfg", None)
        if runtime_cfg is None:
            return None
        tool_name = str(ctx.extra.get("tool_name") or "")

        state = ctx.extra.get("step_inline_state")
        used = int(state.get("used", 0)) if isinstance(state, dict) else 0
        budget = get_tool_step_inline_budget(runtime_cfg)

        # Why: several individually valid tool outputs can still exceed the model
        # context when combined in one round. How: shrink this call's limit once
        # earlier results have consumed the shared budget, forcing media-sized
        # output for overflow. Purpose: the combined tool-result messages stay
        # bounded (moved verbatim from ai_step._execute_real_tools).
        limit = get_tool_inline_limit(tool_name, runtime_cfg)
        if used >= budget:
            limit = min(limit, get_tool_inline_limit("media", runtime_cfg))
        elif used + limit > budget:
            limit = max(1, budget - used)

        # Why: hard truncation must not discard the omitted content while a task
        # is still running. How: write the full text to a task artifact before
        # truncating. Purpose: the bounded prompt text keeps a read_file or grep
        # recovery path.
        ref = ""
        if artifact_enabled(runtime_cfg) and len(raw) > limit:
            rctx = getattr(ls, "rctx", None)
            ref = write_artifact(
                getattr(rctx, "workspace_root", None),
                getattr(rctx, "task_id", ""),
                ctx.step,
                int(ctx.extra.get("call_index") or 0),
                tool_name,
                str(getattr(ctx.tool_call, "id", "") or ""),
                raw,
            )

        raw_inline, truncated = truncate_tool_result(tool_name, raw, limit, ref, config=runtime_cfg)

        return hook_result(
            modified=truncated or bool(ref),
            channels={"result_override": {
                "raw_inline": raw_inline,
                "truncated": truncated,
                "ref": ref,
            }},
        )
