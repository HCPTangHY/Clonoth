from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from engine.compact import (
    count_real_task_segments,
    is_compact_circuit_open,
    microcompact_messages,
    record_compact_failure,
    should_compact,
)
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.compact_flow import prepare_compaction
from engine.inference.loop_state import _persist_ctx
from engine.protocol import ACTION_DISPATCH, TaskAction

logger = logging.getLogger(__name__)


def _seed_prompt_tokens(messages: list[dict[str, Any]]) -> int | None:
    """Harvest the last real prompt_tokens from history when the loop has none.

    Why: a new task starts with last_prompt_tokens=None, so should_compact fell
    back to the chars//3 estimate, which underestimates CJK-heavy history and
    misses overshoot until the first provider call fails or returns usage —
    exactly the "上轮没到上限，这轮一开始就超了" case. How: scan newest-first
    for the last assistant message carrying _meta.usage.prompt_tokens
    (written by shadow write on every assistant turn, carried into history by
    _message_to_history_dict). The seed is then

        prompt_tokens          # 历史+上轮内容，含上轮动态（真实）
        + completion_tokens    # 上轮输出，本轮成为历史（真实）
        + tail chars//3        # 上轮末尾新增（finish 结果等）与新 user（估算）

    This turn's dynamic injection is deliberately NOT counted: the stale
    previous-turn dynamic still sits inside the real prompt_tokens, and the
    two renders are near-identical between turns, so the residual phantom
    stands in for the new one. Net error is only the turn-to-turn delta of
    the dynamic content, not its full size.
    Purpose: the first before_step check of a task sees real usage and can
    compact before the first LLM request goes out.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = (msg.get("_meta") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        pt = usage.get("prompt_tokens")
        if isinstance(pt, bool) or not isinstance(pt, (int, float)) or pt <= 0:
            continue
        ct = usage.get("completion_tokens")
        if isinstance(ct, bool) or not isinstance(ct, (int, float)) or ct < 0:
            ct = 0
        tail_chars = 0
        for tail in messages[i + 1:]:
            if not isinstance(tail, dict):
                continue
            if tail.get("_dynamic"):
                # 抵消项：本轮动态不数，由基数里残留的上轮动态顶替。
                continue
            content = tail.get("content", "")
            if isinstance(content, str):
                tail_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        tail_chars += len(part["text"])
        return int(pt) + int(ct) + tail_chars // 3
    return None


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "CompactChecker",
    "hook_points": [
        ("before_step", "handle"),
    ],
    "priority": 50,
}


class CompactChecker:
    """Run idle cleanup and automatic context compaction before each step."""

    name = "compact_checker"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Apply the legacy microcompact, proactive snip, and compact checks.

        Why: ai_step.py contained several context pressure checks inline. How:
        read loop state from HookContext.extra, run the same first-step cleanup,
        then dispatch the system compactor when the threshold is exceeded.
        Purpose: keep context management behavior unchanged while moving it into
        a before_step handler.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        modified = False
        step_count = int(ctx.extra.get("step_count", 0) or 0)
        if ctx.step == step_count:
            modified = await _run_idle_cleanup(ctx, ls) or modified

        action = await _check_and_compact(ctx, ls)
        # Why: snip-based compaction mutates messages but intentionally does not
        # dispatch the LLM compactor. How: _check_and_compact marks this in
        # ctx.extra, and the handler folds it into the returned HookResult.
        # Purpose: callers can observe non-terminal compaction mutations.
        modified = bool(ctx.extra.pop("compact_modified", False)) or modified
        if action is not None:
            return hook_result(action=action, modified=modified)
        if modified:
            return hook_result(modified=True)
        return None


def _compact_target_session_id(ls: Any) -> str:
    """Return the durable session that automatic compact should rewrite."""
    # [2026-05-27] Why: child sessions (accumulate 模式复用的子节点 session) 的历史
    # 从自己的 JSONL 加载，应该压缩自己的 session 而非父会话。
    # How: 优先返回 child_session_id；没有 child_session_id 时（entry branch 场景），
    # 回退到 parent_session_id or session_id 的旧逻辑。
    # Purpose: 让 accumulate 模式的子节点也能触发自动上下文压缩，防止多次 dispatch 后上下文爆炸。
    child_sid = str(getattr(ls.rctx, "child_session_id", "") or "").strip()
    if child_sid:
        return child_sid
    # [AutoC 2026-05-13] entry branch 场景：prefer parent_session_id over branch session
    return str(getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id or "").strip()


async def _run_idle_cleanup(ctx: Any, ls: Any) -> bool:
    """Run first-step microcompact and proactive snip cleanup.

    Why: these operations reduce stale context before the next model call. How:
    copy the old first-iteration logic from ai_step.py into this helper. Purpose:
    preserve the old trigger timing while keeping CompactChecker readable.
    """
    modified = False
    _messages, cleared = microcompact_messages(ls.messages)
    if cleared:
        logger.info("microcompact: cleared %d tool_results", cleared)
        modified = True

    try:
        from engine.task_record import load_task_records, snip_history, snip_store

        last_ts = None
        for msg in reversed(ls.messages):
            meta = msg.get("_meta", {})
            if isinstance(meta, dict) and (meta.get("message_type") == "assistant" or msg.get("role") == "assistant"):
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    try:
                        last_ts = datetime.fromisoformat(ts_str)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                break
        if last_ts is not None:
            gap_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
            if gap_hours >= 1.0:
                proactive_max = max(int(gap_hours) * 2, 2)
                # [AutoC 2026-05-13] Why: proactive L2 cleanup used to snip the
                # branch copy when child_session_id/session_id pointed at a fork.
                # How: use the same parent-first compact target helper as the
                # threshold path. Purpose: all persisted snips affect the durable
                # parent session.
                snip_sid = _compact_target_session_id(ls)
                snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
                if snip_records:
                    snipped, snip_count, snipped_ids = snip_history(
                        ls.messages,
                        snip_records,
                        keep_recent_tasks=3,
                        max_snip=proactive_max,
                    )
                    if snip_count > 0:
                        ls.messages = snipped
                        ctx.messages = ls.messages
                        store = getattr(ls.rctx, "conversation_store", None)
                        if store:
                            try:
                                persisted = snip_store(store.load(snip_sid), snip_records, snipped_ids)
                                store.replace_all(snip_sid, persisted)
                            except Exception as persist_error:
                                logger.warning("proactive snip persist failed: %s", persist_error)
                        logger.info(
                            "proactive snip: replaced %d tasks (gap=%.1fh, max=%d)",
                            snip_count,
                            gap_hours,
                            proactive_max,
                        )
                        modified = True
    except Exception as snip_error:
        logger.warning("proactive snip failed: %s", snip_error)

    return modified


async def _check_and_compact(ctx: Any, ls: Any) -> TaskAction | None:
    """Return a compactor dispatch action when the legacy threshold says so."""
    if ls.compacted or ls.compact_threshold <= 0:
        return None
    compact_sid = _compact_target_session_id(ls)
    if is_compact_circuit_open(compact_sid):
        return None
    if not should_compact(
        ls.messages, ls.compact_threshold,
        # [AutoC 2026-08-20] Why: the first check of a new task has no in-task
        # usage yet, but the previous task's real prompt size is sitting in the
        # history metadata. How: seed from the last assistant usage instead of
        # falling straight to the chars//3 estimate. Purpose: catch "last round
        # under threshold, this round starts over" before the first LLM call.
        ls.last_prompt_tokens
        if ls.last_prompt_tokens is not None
        else _seed_prompt_tokens(ls.messages),
    ):
        return None

    # ---------------------------------------------------------------
    # Pre-check: count task segments in ConversationStore. If there
    # are not enough segments to compress (≤ keep_recent), skip the
    # LLM compactor call entirely to avoid wasting API calls.
    # ---------------------------------------------------------------
    try:
        _conv_store = getattr(ls.rctx, 'conversation_store', None)
        if _conv_store:
            _stored_msgs = _conv_store.load(compact_sid)
            # [2026-05-17] Why: compact_summary is already compressed history,
            # not a real task segment, so counting it can trigger an endless
            # LLM compactor loop. How: use the shared counter that skips old
            # compact summaries and only counts consecutive real task ids.
            # Purpose: builtin compact and legacy ai_step compact make the same
            # dispatch decision before asking the compactor LLM to summarize.
            _seg_count = count_real_task_segments(_stored_msgs)
            if _seg_count <= ls.compact_keep_recent:
                logger.info(
                    "skip compact: only %d task segments (keep_recent=%d), not enough to compress",
                    _seg_count, ls.compact_keep_recent,
                )
                ls.compacted = True
                return None
    except Exception as _seg_err:
        logger.warning("segment pre-check failed, proceeding with compact: %s", _seg_err)

    try:
        from engine.task_record import (
            load_task_records,
            snip_history,
            snip_store,
        )

        # [AutoC 2026-05-13] Why: L2 persisted snips must affect the parent
        # ConversationStore, not an entry branch fork. How: reuse compact_sid for
        # transcripts and store replacement. Purpose: the next fork starts from
        # already-snipped durable history.
        snip_sid = compact_sid
        snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
        if snip_records:
            snipped, snip_count, snipped_ids = snip_history(ls.messages, snip_records)
            if snip_count > 0:
                ls.messages = snipped
                ctx.messages = ls.messages
                store = getattr(ls.rctx, "conversation_store", None)
                if store:
                    try:
                        stored = store.load(snip_sid)
                        persisted = snip_store(stored, snip_records, snipped_ids)
                        store.replace_all(snip_sid, persisted)
                    except Exception as persist_error:
                        logger.warning("failed to persist snipped history: %s", persist_error)
                await ls.rctx.emit_event("snip_compact", {
                    "node_id": ls.node.id,
                    "step": ctx.step,
                    "snipped_tasks": snip_count,
                })
                logger.info("snip_compact: replaced %d tasks, skipping LLM compact", snip_count)
                ls.compacted = True
                ctx.extra["compact_modified"] = True
                return None
    except Exception as snip_error:
        logger.warning("snip compact failed, falling through to LLM compact: %s", snip_error)

    ls.compacted = True
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": ctx.step})
        # [2026-06-06] Why: automatic compact should keep its threshold, breaker,
        # segment, and snip checks, but must not maintain a separate summary prompt
        # builder. How: call the shared planner after all automatic pre-checks pass.
        # Purpose: automatic compact dispatches the same input shape as manual and
        # API compaction.
        plan = await prepare_compaction(
            messages=ls.messages,
            target_session_id=compact_sid,
            node_id=ls.node.id,
            keep_recent=ls.compact_keep_recent,
            threshold_tokens=ls.compact_threshold,
        )
        if plan.reason.startswith("truncated:"):
            try:
                original_chars = int(plan.reason.split(":", 1)[1])
            except (IndexError, ValueError):
                original_chars = 0
            await ls.rctx.emit_event("ptl_truncated", {
                "node_id": ls.node.id,
                "step": ctx.step,
                "original_chars": original_chars,
            })
        if plan.status == "dispatch":
            ctx_ref = _persist_ctx(ls, ctx.step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input=plan.dispatch_input,
            )
        if plan.status == "failed":
            record_compact_failure(compact_sid)
            await ls.rctx.emit_event("compact_failed", {
                "node_id": ls.node.id,
                "step": ctx.step,
                "error": plan.reason,
            })
    except Exception as compact_error:
        # [AutoC 2026-05-13] Why: failures should trip the breaker for the session
        # we attempted to compact, not a temporary branch. How: record against
        # compact_sid. Purpose: retry behavior matches the parent ConversationStore
        # target used by L2/L3/LLM compact.
        record_compact_failure(compact_sid)
        await ls.rctx.emit_event("compact_failed", {
            "node_id": ls.node.id,
            "step": ctx.step,
            "error": str(compact_error),
        })
    return None
