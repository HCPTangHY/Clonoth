from __future__ import annotations

from typing import Any

from engine.attachments import build_multimodal_content
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.message_assembly import rebuild_dynamic_context
from engine.protocol import ACTION_CANCELLED, TaskAction


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "PreemptChecker",
    "hook_points": [
        ("before_step", "handle"),
    ],
    "priority": 100,
}


class PreemptChecker:
    """Handle cancellation and preempt message injection before each LLM step."""

    name = "preempt_checker"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        """Run the legacy loop-top cancellation and preempt checks.

        Why: cancellation and preempt state were hard-coded in ai_step.py, which
        made the inference loop harder to extend. How: read the current loop
        state from HookContext.extra, perform the same checks, and mutate the
        message list when a preempt message must be injected. Purpose: preserve
        the old control flow while moving the behavior behind before_step.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        if await ls.rctx.check_cancelled():
            await ls.rctx.emit_event("cancel_acknowledged", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
            })
            return hook_result(
                action=TaskAction(
                    action=ACTION_CANCELLED,
                    node_id=ls.node.id,
                    summary="任务已被用户取消。",
                )
            )

        if not ls.preempt_after_step and ls.preempt_inject_info is None:
            preempt_info = await ls.rctx.check_preempted()
            if preempt_info.get("preempted"):
                if preempt_info.get("message"):
                    ls.preempt_inject_info = preempt_info
                else:
                    ls.preempt_after_step = True
                    await ls.rctx.emit_event("preempt_acknowledged", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "step": ctx.step,
                    })

        if ls.preempt_inject_info is None:
            return None

        await _inject_preempt_message(ctx, ls)
        return hook_result(modified=True)


async def _inject_preempt_message(ctx: Any, ls: Any) -> None:
    """Inject a pending preempt message into the loop state.

    Why: the next LLM prompt must include the user's new message instead of
    finishing stale work. How: remove old dynamic messages, rebuild dynamic
    skill and memory context, append the preempt user input, and acknowledge the
    runtime. Purpose: keep Preempt V2 behavior identical after extraction.
    """
    new_instruction = ls.preempt_inject_info.get("message", "")
    new_attachments = ls.preempt_inject_info.get("attachments", [])

    # Why: dynamic context (skills, memory) must be refreshed for the new
    # instruction. How: the shared assembly helper drops old _dynamic messages
    # and re-renders all dynamic-scope prompt sections in place. Purpose: the
    # preempt handler no longer imports the knowledge plugin or duplicates the
    # block/string prefix logic.
    rebuild_dynamic_context(
        ls.messages,
        workspace_root=ls.rctx.workspace_root,
        node=ls.node,
        runtime_cfg=ls.runtime_cfg,
        history=ls.history,
        instruction=new_instruction,
        is_block_mode=ls.is_block_mode,
        system_prompt=ls.system_prompt,
        session_id=getattr(ls.rctx, "session_id", "") or "",
        workspace_name=getattr(ls.rctx, "workspace_name", "") or "",
    )

    if new_attachments:
        ls.messages.append({
            "role": "user",
            "content": build_multimodal_content(
                new_instruction,
                new_attachments,
                workspace_root=ls.rctx.workspace_root,
            ),
        })
    else:
        ls.messages.append({"role": "user", "content": new_instruction})

    # [Fork/Merge 2026-05-12] Persist the injected preempt user message to ConversationStore.
    # Why: before this hook was extracted, preempt injection only mutated the in-memory prompt,
    # so a branch task could lose the new user message when it resumed or later merged. How:
    # append a USER_INPUT record to the active runtime session, preferring child_session_id for
    # delegated child nodes and otherwise using rctx.session_id, which may be an entry branch.
    # Purpose: preempted branch histories remain complete and merge back with the injected input.
    try:
        store = getattr(ls.rctx, "conversation_store", None)
        if store is not None and (new_instruction or new_attachments):
            from datetime import datetime, timezone
            from uuid import uuid4

            from engine.conversation_store import Message, MessageType

            target_session = getattr(ls.rctx, "child_session_id", "") or ls.rctx.session_id
            # [AutoC 2026-06-01] Why: preempt messages with attachments were
            # appended to the live prompt as multimodal content but persisted as
            # plain text, so a resume inside the same task lost the image. How:
            # build the same multimodal content for ConversationStore when
            # attachments are present. Purpose: injected user input follows the
            # same task-local image retention rule as initial task input.
            if new_attachments:
                persisted_content = build_multimodal_content(
                    new_instruction,
                    new_attachments,
                    workspace_root=ls.rctx.workspace_root,
                )
            else:
                persisted_content = new_instruction
            # [2026-06-06] Why: preempt messages injected here are always user-
            # initiated追加指令 (child-node callbacks use inject_async_result which
            # goes through the inbound path instead). How: unconditionally mark
            # meta.preempt=True. Purpose: frontend history hydration renders these
            # as inline preempt cards instead of plain user messages.
            _meta: dict[str, Any] = {"preempt": True}
            if new_attachments:
                _meta["attachments"] = list(new_attachments)
            store.append(
                target_session,
                Message(
                    id=str(uuid4()),
                    role="user",
                    content=persisted_content,
                    message_type=MessageType.USER_INPUT,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    meta=_meta,
                    source_node_id=getattr(ls.node, "id", ""),
                    source_task_id=getattr(ls.rctx, "task_id", ""),
                ),
            )
    except Exception:
        pass

    await ls.rctx.consume_preempt()
    await ls.rctx.emit_event("preempt_injected", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "step": ctx.step,
        "message": new_instruction,
    })

    ls.preempt_inject_info = None
    ls.plaintext_retry_count = 0
    ls.compacted = False
