from __future__ import annotations

import logging
from typing import Any

from engine.compact import (
    count_real_task_segments,
    invalidate_stale_usage,
    is_compact_circuit_open,
    is_snipped_message,
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
        + tail chars//2        # 新 user、工具结果与增量动态上下文（估算）

    Historical dynamic rows are already included in measured prompt_tokens.
    New dynamic rows in the tail are additional tokens and must be counted.
    Purpose: the first before_step check of a task sees real usage and can
    compact before the first LLM request goes out.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = (msg.get("_meta") or {}).get("usage")
        if not isinstance(usage, dict) or usage.get("_compacted_stale"):
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
            content = tail.get("content", "")
            if isinstance(content, str):
                tail_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        tail_chars += len(part["text"])
        # Subtract tokens of images stripped from the task that recorded this prompt_tokens
        asst_tid = str((msg.get("_meta") or {}).get("source_task_id") or "")
        stripped_imgs = 0
        if asst_tid:
            for prev in messages[:i + 1]:
                if isinstance(prev, dict):
                    pmeta = prev.get("_meta") or {}
                    if isinstance(pmeta, dict) and str(pmeta.get("source_task_id") or "") == asst_tid:
                        stripped_imgs += int(pmeta.get("_stripped_images_count") or 0)
        seeded = int(pt) + int(ct) + tail_chars // 2 - stripped_imgs * 1600
        return max(seeded, tail_chars // 2)
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
    # [AutoC 2026-08-30] system.compactor 节点从 engine/system_nodes/ 迁移为
    # 插件声明（nodes face，Paradox 式覆盖语义，卸载自动回落）。
    "nodes": [{"file": "nodes/system.compactor.yaml"}],
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
    """Idle cleanup hook (microcompact and proactive snip disabled to preserve Prefix KV Cache)."""
    return False


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
            _conv_store.invalidate_cache(compact_sid)
            _runtime_sid = str(getattr(ls.rctx, "session_id", "") or "").strip()
            if _runtime_sid and _runtime_sid != compact_sid:
                _conv_store.invalidate_cache(_runtime_sid)
                _branch_disk_msgs = _conv_store.load(_runtime_sid)
                _disk_summary_ids = {
                    str(getattr(m, "id", "") or "")
                    for m in _branch_disk_msgs
                    if getattr(m, "message_type", "") == "summary" or getattr(m, "source_task_id", "") == "compact_summary"
                }
                _mem_summary_ids = {
                    str(m.get("id") or "")
                    for m in ls.messages
                    if isinstance(m, dict) and (
                        m.get("message_type") == "summary"
                        or (m.get("_meta") or {}).get("source_task_id") == "compact_summary"
                        or str(m.get("content") or "").startswith("[Below is a structured summary")
                    )
                }
                if _disk_summary_ids and not (_disk_summary_ids <= _mem_summary_ids):
                    # Concurrent task already compacted parent and synced our branch on disk while we were in-flight
                    from engine.runner import _message_to_history_dict
                    from providers.tool_format import sanitize_control_tool_history
                    _reloaded = [
                        _message_to_history_dict(m, current_task_id=str(getattr(ls.rctx, "task_id", "") or ""))
                        for m in _branch_disk_msgs if getattr(m, "role", "") != "system"
                    ]
                    _reloaded = sanitize_control_tool_history(_reloaded)
                    invalidate_stale_usage(_reloaded)
                    _sys_prefix = [m for m in ls.messages if isinstance(m, dict) and m.get("role") == "system"]
                    ls.messages[:] = _sys_prefix + _reloaded
                    from engine.inference.message_assembly import rebuild_dynamic_context
                    from engine.inference.ai_step import _persist_dynamic_messages
                    rebuild_dynamic_context(
                        ls.messages, workspace_root=ls.rctx.workspace_root,
                        node=ls.node, runtime_cfg=ls.runtime_cfg, history=ls.messages,
                        instruction=str(getattr(ls.rctx, "user_text", "") or ""),
                        is_block_mode=ls.is_block_mode, system_prompt=ls.system_prompt,
                        session_id=ls.rctx.session_id,
                        workspace_name=getattr(ls.rctx, "workspace_name", "") or "",
                        task_context=ls.rctx.task_context,
                    )
                    from engine.builtin.workspace_context import WorkspaceContext
                    await WorkspaceContext().handle(ctx)
                    _persist_dynamic_messages(ls)
                    ls.last_prompt_tokens = None
                    if not should_compact(ls.messages, ls.compact_threshold, None):
                        logger.info("skip compact: branch %s was already synced by a concurrent compaction", _runtime_sid)
                        return None
            _stored_msgs = _conv_store.load(compact_sid)
            _is_entry_branch = bool(
                getattr(ls.rctx, "parent_session_id", "")
                and not getattr(ls.rctx, "child_session_id", "")
            )
            _seg_source_msgs = (
                _branch_disk_msgs
                if (_is_entry_branch and _runtime_sid and _runtime_sid != compact_sid and _branch_disk_msgs)
                else _stored_msgs
            )
            _seg_count = count_real_task_segments(_seg_source_msgs)
            _snipped_stubs = sum(1 for m in _seg_source_msgs if is_snipped_message(m))
            _min_segs = 2 if (_seg_source_msgs is not _stored_msgs) else (1 if _is_entry_branch else 2)
            if _seg_count < _min_segs and _snipped_stubs == 0:
                logger.info(
                    "skip compact: only %d real task segments (min=%d, snipped=%d), not enough to compress",
                    _seg_count, _min_segs, _snipped_stubs,
                )
                ls.compacted = True
                return None
    except Exception as _seg_err:
        logger.warning("segment pre-check failed, proceeding with compact: %s", _seg_err)

    # [2026-10-26] Disabled L2 snip_compact: incremental 2-task snipping repeatedly
    # mutates the head of conversation history every 1-2 turns, invalidating the entire
    # 300k+ token Prefix KV Cache. Exceeding threshold_tokens now goes directly to
    # system.compactor with hysteresis low-watermark compaction.
    ls.compacted = True
    try:
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
            await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": ctx.step})
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
