"""Context compaction -- pure utility functions.

Provides helper functions for context compression:
- should_compact: check if messages exceed token threshold
- apply_compact_summary: apply a pre-generated summary to compress messages
- _format_messages_for_summary: format messages into text for summary LLM
- _format_compact_summary: extract <summary> from LLM output
- split_task_segments / compute_effective_keep_recent / count_real_task_segments:
  the shared task-boundary policy used by engine, builtin handler and supervisor
- estimate_message_tokens / invalidate_stale_usage /
  strip_anthropic_thinking_signatures: token estimation and cache-invalidating
  metadata rewrites that every compact path must apply identically

No LLM calls in this module.  The actual summarization is done by the
system.compactor node, dispatched via supervisor task queue.
"""
from __future__ import annotations

from .conversation_store import is_dynamic_context

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Auto-compact circuit breaker (session-level)
#  [2026-04-24] P1.5 熔断器：连续 N 次压缩失败后暂停自动压缩，
#  避免每个新 task 的 _LoopState 重置 compacted=False 后反复浪费 API 调用。
#  _compact_failures 是 module-level dict，进程重启自然清零，无需持久化。
# ---------------------------------------------------------------------------

_MAX_CONSECUTIVE_FAILURES = 3
_compact_failures: dict[str, int] = {}  # session_id → consecutive failure count


def record_compact_failure(session_id: str) -> None:
    """Record a compact failure. After MAX failures, is_compact_circuit_open returns True."""
    count = _compact_failures.get(session_id, 0) + 1
    _compact_failures[session_id] = count
    if count >= _MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            "Compact circuit breaker tripped for session %s after %d consecutive failures",
            session_id, count,
        )


def record_compact_success(session_id: str) -> None:
    """Reset failure counter on success."""
    _compact_failures.pop(session_id, None)


def is_compact_circuit_open(session_id: str) -> bool:
    """Return True if compact should be skipped (too many consecutive failures)."""
    return _compact_failures.get(session_id, 0) >= _MAX_CONSECUTIVE_FAILURES


# ---------------------------------------------------------------------------
#  P1 Microcompact — time-based tool_result cleanup
#  When last assistant message is older than gap_minutes (cache expired),
#  clear old tool_result contents, keeping only the most recent ones.
#  [2026-10-26] 自动 idle 触发已停用（前缀会反复变化，冲掉 Prefix KV Cache），
#  这里只保留 llm_call 的 reactive compact（413 / context_length 安全网）在用。
# ---------------------------------------------------------------------------

_MICROCOMPACT_PLACEHOLDER = "[tool result cleared — cache expired]"


def _build_microcompact_content(content: str, tool_name: str) -> str:
    """Build one consistent compact placeholder for runtime and stored messages."""
    if content.startswith('Tool result for "'):
        first_line = content.split("\n", 1)[0][:200]
        return first_line + "\n" + _MICROCOMPACT_PLACEHOLDER
    preview = content[:120].replace("\n", " ")
    return f"[tool result ({tool_name}): {preview}...]\n{_MICROCOMPACT_PLACEHOLDER}"


def microcompact_messages(
    messages: list[dict[str, Any]],
    *,
    gap_minutes: int = 60,
    keep_recent: int = 5,
    min_tool_results: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """Time-based microcompact: clear old tool_result contents.

    Triggered when the last assistant message is older than gap_minutes,
    indicating the provider's prompt cache has likely expired.
    Clears tool_result content for all but the most recent `keep_recent` results.

    Returns (messages, cleared_count). Messages are modified in-place.
    """
    from datetime import datetime, timezone

    # Find last assistant message timestamp
    last_assistant_ts = None
    for msg in reversed(messages):
        meta = msg.get("_meta", {})
        if isinstance(meta, dict):
            mt = meta.get("message_type", "")
            if mt == "assistant" or msg.get("role") == "assistant":
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    try:
                        last_assistant_ts = datetime.fromisoformat(ts_str)
                        if last_assistant_ts.tzinfo is None:
                            last_assistant_ts = last_assistant_ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                break

    if last_assistant_ts is None:
        return messages, 0

    now = datetime.now(timezone.utc)
    gap = (now - last_assistant_ts).total_seconds() / 60.0
    if gap < gap_minutes:
        return messages, 0

    # Collect tool_result indices (only count non-trivial tool results > 200 chars)
    tr_indices: list[int] = []
    for i, msg in enumerate(messages):
        meta = msg.get("_meta", {})
        is_tr = False
        if msg.get("role") == "tool" or msg.get("message_type") == "tool_result":
            is_tr = True
        elif isinstance(meta, dict) and meta.get("message_type") == "tool_result":
            is_tr = True
        elif isinstance(msg.get("content"), str) and msg["content"].startswith('Tool result for "'):
            is_tr = True
        if is_tr:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 200:
                tr_indices.append(i)

    if len(tr_indices) < min_tool_results:
        return messages, 0

    # Clear all but the last `keep_recent` tool results
    to_clear = tr_indices[:-keep_recent] if keep_recent > 0 else tr_indices
    cleared = 0
    for idx in to_clear:
        msg = messages[idx]
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 200 and not content.endswith(_MICROCOMPACT_PLACEHOLDER):
            tname = str(msg.get("name") or (msg.get("_meta") or {}).get("name") or "tool")
            new_content = _build_microcompact_content(content, tname)
            msg["content"] = new_content
            meta = msg.get("_meta")
            if isinstance(meta, dict):
                if "tool_result_raw_inline" in meta:
                    meta["tool_result_raw_inline"] = new_content
                meta.pop("tool_result_structured", None)
            cleared += 1

    if cleared:
        logger.info(
            "microcompact: cleared %d/%d tool_results (gap=%.0fmin, kept=%d recent)",
            cleared, len(tr_indices), gap, keep_recent,
        )

    if cleared > 0 and to_clear:
        # Prefix changed → context-bound Anthropic thinking signatures are void.
        strip_anthropic_thinking_signatures(messages[min(to_clear):])
    return messages, cleared


# ---------------------------------------------------------------------------
#  Summary formatting
# ---------------------------------------------------------------------------

# Why: the same summary message may have been written by this repo (Chinese
# header, older records) or by the production compactor (English header).
# How: strip either header before validating. Purpose: a re-compaction never
# nests two headers, and legacy Chinese summaries stay usable.
_SUMMARY_HEADERS = (
    "[Below is a structured summary of the previous conversation; the original context has been compacted]",
    "[以下是之前对话的结构化摘要，原始上下文已被压缩]",
)


def _format_compact_summary(raw_summary: str) -> str:
    """规范化并校验 LLM 输出的上下文压缩摘要（支持无标签纯文本直出，向下兼容 <analysis>/<summary> 标签）。

    返回空字符串表示摘要不合格（长度不足等），
    调用方应视为压缩失败，不采纳摘要，不重置上下文。
    """
    text = str(raw_summary or "").strip()
    if not text:
        return ""

    # 向下兼容：若仍带有 <analysis> 草稿块则剥离
    text = re.sub(r'<analysis>[\s\S]*?</analysis>', '', text).strip()
    if '<analysis>' in text and '<summary>' in text:
        text = text[text.find('<summary>'):].strip()

    # 向下兼容：若带有 <summary> 标签（无论末尾是否闭合 </summary>），提取其内部内容
    m = re.search(r'<summary>([\s\S]*?)(?:</summary>|$)', text)
    if m:
        text = m.group(1).strip()
    else:
        # 剥离可能包裹在最外层的 markdown 代码块围栏
        fence_m = re.match(r'^```(?:markdown|md|text|xml)?\s*\n([\s\S]*?)\n```\s*$', text)
        if fence_m:
            text = fence_m.group(1).strip()
        # 移除可能残留的孤立闭合标签
        text = re.sub(r'</?(?:summary|analysis)>\s*', '', text).strip()

    # 防止模型重复写出固定摘要头（中英两种历史格式都剥离）
    for _header in _SUMMARY_HEADERS:
        if text.startswith(_header):
            text = text[len(_header):].strip()

    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # 最低长度校验：摘要不足 200 字符视为垃圾摘要，不采纳
    if len(text) < 200:
        logger.warning(
            "compact summary rejected: too short (%d chars, minimum 200)",
            len(text),
        )
        return ""

    return text


# ---------------------------------------------------------------------------
#  Threshold check
# ---------------------------------------------------------------------------

def should_compact(
    messages: list[dict[str, Any]],
    threshold_tokens: int,
    last_prompt_tokens: int | None = None,
) -> bool:
    """Return True if estimated/actual token count exceeds *threshold_tokens*.

    If *last_prompt_tokens* (from the previous LLM response's usage) is
    available, use that directly.  Otherwise, estimate from the message
    (content + tool_calls + multimodal blocks + recorded completion usage).
    """
    if threshold_tokens <= 0:
        return False
    if last_prompt_tokens is not None:
        return last_prompt_tokens > threshold_tokens
    total_tokens = 0
    for m in messages:
        total_tokens += estimate_message_tokens(m)
        if total_tokens > threshold_tokens:
            return True
    return total_tokens > threshold_tokens


# ---------------------------------------------------------------------------
#  Message-like accessors (works for dict history and ConversationStore.Message)
# ---------------------------------------------------------------------------

def _message_meta(message: Any) -> dict[str, Any]:
    """Return the metadata dict of a history dict or stored Message."""
    if isinstance(message, dict):
        meta = message.get("_meta") or message.get("meta") or {}
    else:
        meta = getattr(message, "meta", None) or {}
    return meta if isinstance(meta, dict) else {}


def _source_task_id_from_message_like(message: Any) -> str:
    """Return source_task_id from either a stored Message object or history dict."""
    # [2026-05-17] Why: compact pre-checks run on ConversationStore Message
    # objects, while legacy snapshot compaction still uses dict histories. How:
    # read meta/_meta first and then fall back to the top-level field/attribute.
    # Purpose: keep segment counting consistent across both engine compact paths.
    meta_tid = _message_meta(message).get("source_task_id", "")
    if isinstance(message, dict):
        return str(meta_tid or message.get("source_task_id", "") or "")
    return str(meta_tid or getattr(message, "source_task_id", "") or "")


def _content_from_message_like(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _message_type_from_message_like(message: Any) -> str:
    """Return the structured message type of a history dict or stored Message."""
    if isinstance(message, dict):
        top = message.get("message_type", "")
    else:
        top = getattr(message, "message_type", "")
    if top:
        return str(top)
    return str(_message_meta(message).get("message_type") or "")


def is_snipped_message(message: Any) -> bool:
    """Return True if message is an L2 snipped task stub."""
    content = _content_from_message_like(message)
    return isinstance(content, str) and content.startswith("[Task summary — original messages snipped]")


def is_summary_message(message: Any) -> bool:
    """Return True when a message is an already-compacted summary block.

    Why: summary blocks exist in three shapes — engine dict history tagged with
    _meta.source_task_id=compact_summary, supervisor Message rows typed
    message_type=summary, and legacy rows identified only by their header text.
    How: accept the structured type first, then the legacy Chinese/English
    header. Purpose: every compact path treats an old summary as replaceable
    prefix instead of a keep_recent task segment.
    """
    if _source_task_id_from_message_like(message) == "compact_summary":
        return True
    if _message_type_from_message_like(message) == "summary":
        return True
    content = _content_from_message_like(message)
    return isinstance(content, str) and content.startswith(_SUMMARY_HEADERS)


def estimate_message_tokens(message: Any) -> int:
    """Estimate tokens of a Message or dict (text, tool_calls, raw parts, images)."""
    content = _content_from_message_like(message)
    chars = 0
    if isinstance(content, str):
        chars += len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    chars += len(t)
                elif part.get("type") in ("image_url", "image") or "inlineData" in part:
                    chars += 3200  # ~1600 tokens per image
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    if tool_calls:
        chars += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    meta = _message_meta(message)
    completion_toks = 0
    usage = meta.get("usage")
    if isinstance(usage, dict):
        ct = usage.get("completion_tokens")
        if not isinstance(ct, bool) and isinstance(ct, (int, float)) and ct > 0:
            completion_toks = int(ct)
    md = meta.get("metadata")
    if isinstance(md, dict) and md and not chars and not completion_toks:
        chars += len(json.dumps(md, ensure_ascii=False, default=str))
    return max(completion_toks, chars // 2, 1)


def strip_anthropic_thinking_signatures(messages: list[Any]) -> int:
    """Strip context-bound Anthropic thinking_blocks from messages whose prefix changed.

    Why: Anthropic rejects (or degrades) replayed thinking blocks whose signed
    prefix no longer matches — after a merge/compact the prefix is new. How:
    drop only metadata.anthropic.thinking_blocks, leaving the visible thinking
    text intact. Purpose: history stays replayable without touching content.
    """
    stripped = 0
    for m in messages:
        meta = _message_meta(m)
        md = meta.get("metadata")
        if isinstance(md, dict):
            anth = md.get("anthropic")
            if isinstance(anth, dict) and anth.pop("thinking_blocks", None):
                stripped += 1
    return stripped


def invalidate_stale_usage(messages: list[Any]) -> None:
    """Mark historical assistant usage.prompt_tokens as stale and strip context-bound Anthropic thinking signatures after compaction/snip."""
    for m in messages:
        meta = _message_meta(m)
        usage = meta.get("usage")
        if isinstance(usage, dict) and "prompt_tokens" in usage:
            # Why: prompt_tokens measured against the pre-compact prefix must not
            # be reused as the current context size or as a compact seed.
            # How: flag it instead of deleting the recorded usage.
            usage["_compacted_stale"] = True
        md = meta.get("metadata")
        if isinstance(md, dict):
            anth = md.get("anthropic")
            if isinstance(anth, dict):
                anth.pop("thinking_blocks", None)


# ---------------------------------------------------------------------------
#  Task-boundary segmentation (single source of truth)
# ---------------------------------------------------------------------------

def split_task_segments(messages: list[Any]) -> tuple[list[Any], list[list[Any]]]:
    """Split messages into (prefix_msgs, real_segments) consistently across Engine and Supervisor.

    - Messages with source_task_id == 'compact_summary' (or summary typed rows)
      and L2 snipped stubs go into prefix_msgs: they are replaceable compressed
      history, never a keep_recent task segment.
    - Messages with empty source_task_id in the middle of a task (output_rejected,
      async_tool dispatch_result, ...) are merged into the surrounding task
      segment so one task is never split into multiple segments.
    - Real user/preempt turns may start a new segment within one task; rejection,
      retry and tool-result messages remain in the surrounding turn.
    - system messages are re-rendered; dynamic rows stay with their task.
    """
    filtered: list[Any] = []
    for m in messages:
        if isinstance(m, dict):
            if m.get("role") == "system":
                continue
        else:
            if getattr(m, "role", "") == "system":
                continue
        filtered.append(m)

    tids = [_source_task_id_from_message_like(m) for m in filtered]
    n = len(filtered)
    for i in range(n):
        if not tids[i]:
            prev_tid = tids[i - 1] if i > 0 else ""
            next_tid = ""
            for j in range(i + 1, n):
                if tids[j]:
                    next_tid = tids[j]
                    break
            c = _content_from_message_like(filtered[i])
            item = filtered[i]
            role = item.get("role", "") if isinstance(item, dict) else getattr(item, "role", "")
            # Live assistant/tool rows may not carry their persisted task ID yet.
            # Keep the response after a tagged preempt in that same task, even
            # at the tail where there is no later row to infer ownership from.
            is_mid_task = role in ("assistant", "tool") or is_dynamic_context(item) or isinstance(c, str) and (
                c.startswith("reject by ") or c.startswith("<dispatch_result")
            )
            if prev_tid and prev_tid != "compact_summary" and (prev_tid == next_tid or is_mid_task):
                tids[i] = prev_tid
            elif is_dynamic_context(filtered[i]) and next_tid and next_tid != "compact_summary":
                tids[i] = next_tid

    def _role_of(item: Any) -> str:
        return str(item.get("role") or "") if isinstance(item, dict) else str(getattr(item, "role", "") or "")

    def _has_tool_calls(item: Any) -> bool:
        calls = item.get("tool_calls") if isinstance(item, dict) else getattr(item, "tool_calls", None)
        return bool(isinstance(calls, list) and calls)

    def _is_preempt_turn_boundary(item: Any, segment: list[Any]) -> bool:
        if is_dynamic_context(item) or _role_of(item) != "user" or not segment:
            return False
        if not any(_role_of(message) == "assistant" for message in segment):
            return False
        if _role_of(segment[-1]) == "assistant" and _has_tool_calls(segment[-1]):
            return False
        meta = _message_meta(item)
        message_type = _message_type_from_message_like(item).strip()
        if meta.get("is_rejection") or meta.get("rejected_draft"):
            return False
        if message_type in ("output_rejected", "retry_hint", "tool_result_attachment", "summary"):
            return False
        content = _content_from_message_like(item)
        if isinstance(content, str) and content.startswith((
            "reject by ",
            "[SYSTEM REJECTED]",
            "[Execution Warning]",
            "[Important Reminder]",
            "The tool executions above produced the following image results:",
        )):
            return False
        return bool(meta.get("preempt")) or message_type == "user_input"

    prefix_msgs: list[Any] = []
    segments: list[list[Any]] = []
    cur_seg: list[Any] = []
    cur_tid: str | None = None

    for m, tid in zip(filtered, tids):
        if tid == "compact_summary" or is_snipped_message(m) or is_summary_message(m):
            if cur_seg:
                segments.append(cur_seg)
                cur_seg = []
                cur_tid = None
            prefix_msgs.append(m)
            continue
        if cur_tid is None or tid != cur_tid or _is_preempt_turn_boundary(m, cur_seg):
            if cur_seg:
                segments.append(cur_seg)
            cur_seg = [m]
            cur_tid = tid
        else:
            cur_seg.append(m)
    if cur_seg:
        segments.append(cur_seg)

    return prefix_msgs, segments


def compute_effective_keep_recent(
    segments: list[list[Any]],
    keep_recent: int = 6,
    threshold_tokens: int = 0,
    prefix_msgs: list[Any] | None = None,
) -> int:
    """Compute effective keep_recent with a low watermark and a minimum batch size.

    Why: compacting only one tiny leading segment per turn re-triggers compacting
    every turn and rewrites the prefix each time (killing the prefix KV cache).
    How: keep at most ~55% of the threshold after compacting (hysteresis), and
    always free a meaningful chunk (>=10% of threshold or 45% of history).
    Purpose: one compaction is sufficient, and the next few turns keep the same
    prefix.
    """
    total_segs = len(segments)
    if total_segs <= 1:
        return total_segs
    actual_keep = max(1, min(int(keep_recent or 6), total_segs - 1))
    seg_tokens = [sum(estimate_message_tokens(m) for m in seg) for seg in segments]

    if threshold_tokens > 0:
        # Low watermark: target ~55% of threshold_tokens (reserve ~48k for system prompt + tools + summary)
        target_total = int(threshold_tokens * 0.55)
        system_and_summary_overhead = 48_000
        kept_budget = max(32_000, target_total - system_and_summary_overhead)
        while actual_keep > 1 and sum(seg_tokens[-actual_keep:]) > kept_budget:
            actual_keep -= 1

    # Ensure every compaction frees a meaningful chunk of history (at least ~10% of threshold
    # or 45% of total conversation tokens) rather than just 1-2k tokens of a tiny leading segment.
    total_conv_tokens = sum(seg_tokens)
    snipped_tokens = sum(estimate_message_tokens(m) for m in (prefix_msgs or []) if is_snipped_message(m))
    min_free_target = min(
        max(16_000, int((threshold_tokens or 200_000) * 0.10)),
        int(total_conv_tokens * 0.45),
    )
    while (
        actual_keep > 1
        and (sum(seg_tokens[:-actual_keep]) + snipped_tokens) < min_free_target
    ):
        actual_keep -= 1

    return max(1, actual_keep)


def count_real_task_segments(messages: list[Any]) -> int:
    """Count consecutive real (non-summary, non-snipped) task segments."""
    _, segments = split_task_segments(messages)
    return len(segments)


# ---------------------------------------------------------------------------
#  Apply summary (pure function, no LLM)
# ---------------------------------------------------------------------------

def apply_compact_summary(
    messages: list[dict[str, Any]],
    summary: str,
    *,
    keep_recent: int = 2,
    threshold_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Apply a pre-generated summary to compress messages.

    Same split logic as the ConversationStore path, but works on history dicts.
    Takes the summary text directly instead of calling LLM.

    If *threshold_tokens* > 0, the function will progressively reduce
    *keep_recent* until the result fits within the budget or keep_recent
    reaches 0.

    Returns compressed message list.  If summary is empty or
    messages too short, returns original list unchanged.
    """
    if not summary or len(messages) <= keep_recent + 2:
        return messages

    # --- split segments ---
    # 1. 收集开头的 system 消息（静态前缀：静态 prompt + 常驻 skills/memory）
    prefix_systems: list[dict[str, Any]] = []
    body = list(messages)
    while body and body[0].get("role") == "system":
        prefix_systems.append(body.pop(0))

    # 2. 分离 body 中嵌入的 system 消息（动态 prompt/skills/memory）与对话消息
    conversation: list[dict[str, Any]] = []
    inner_systems: list[dict[str, Any]] = []
    for msg in body:
        if msg.get("role") == "system":
            inner_systems.append(msg)
        else:
            conversation.append(msg)

    # --- 按统一规则划分 prefix_msgs 与 real segments ---
    prefix_msgs, segments = split_task_segments(conversation)

    # keep_recent 现在是「保留最近 N 个完整 task segment」
    keep_recent = compute_effective_keep_recent(
        segments, keep_recent=keep_recent, threshold_tokens=threshold_tokens, prefix_msgs=prefix_msgs,
    )
    if len(segments) <= keep_recent and not any(is_snipped_message(m) for m in prefix_msgs):
        return messages

    kept_segments = segments[-keep_recent:] if keep_recent > 0 else []
    compressed_segments = segments[:-keep_recent] if keep_recent > 0 else list(segments)
    to_keep: list[dict[str, Any]] = []
    for seg in kept_segments:
        to_keep.extend(seg)
    invalidate_stale_usage(to_keep)

    # P6.5 Metadata Preservation: 收集被压缩掉的消息所属的 source_task_id，
    # 存入摘要消息的 _meta 中，防止 L2 snip 因 ID 丢失而重复触发 L3 LLM 压缩。
    # [2026-04-26] 累积继承：当旧的 compact_summary 被再次压缩时，
    # 继承其 compressed_task_ids，避免更早被压缩的任务 ID 丢失。
    _compressed: list[dict[str, Any]] = list(prefix_msgs)
    for seg in compressed_segments:
        _compressed.extend(seg)
    _compressed_tids = set()
    for _m in _compressed:
        _meta = _m.get("_meta", {})
        if not isinstance(_meta, dict):
            _meta = {}
        _tid = _meta.get("source_task_id")
        if _tid and _tid != "compact_summary":
            _compressed_tids.add(str(_tid))
        # 继承旧摘要中已记录的 compressed_task_ids
        _old_ctids = _meta.get("compressed_task_ids")
        if isinstance(_old_ctids, list):
            for _ctid in _old_ctids:
                if _ctid:
                    _compressed_tids.add(str(_ctid))

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            _SUMMARY_HEADERS[0] + "\n\n" + summary
        ),
        "_meta": {
            "source_task_id": "compact_summary",
            "compressed_task_ids": list(_compressed_tids),
        }
    }

    result: list[dict[str, Any]] = []
    result.extend(prefix_systems)
    result.append(summary_msg)
    result.extend(inner_systems)
    result.extend(to_keep)

    # --- Progressive keep_recent reduction ---
    # If the result still exceeds the token threshold, drop older kept
    # segments one at a time until we fit or keep_recent reaches 0.
    if threshold_tokens > 0 and keep_recent > 0:
        while should_compact(result, threshold_tokens) and keep_recent > 0:
            keep_recent -= 1
            kept_segments = segments[-keep_recent:] if keep_recent > 0 else []
            to_keep = []
            for seg in kept_segments:
                to_keep.extend(seg)
            invalidate_stale_usage(to_keep)
            result = []
            result.extend(prefix_systems)
            result.append(summary_msg)
            result.extend(inner_systems)
            result.extend(to_keep)
            logger.info(
                "apply_compact_summary: still over threshold, reduced keep_recent to %d",
                keep_recent,
            )

    logger.info(
        "apply_compact_summary: %d -> %d messages (keep_recent=%d)",
        len(messages), len(result), keep_recent,
    )
    return result


# ---------------------------------------------------------------------------
#  Message formatting (for summary LLM input)
# ---------------------------------------------------------------------------

def _format_messages_for_summary(
    messages: list[dict[str, Any]],
) -> str:
    """Format messages into plain text for the summary LLM.

    Inserts ``=== TASK [task_id] ===`` markers when source_task_id changes,
    so the compactor LLM can see task boundaries.
    """
    from providers.tool_format import sanitize_control_tool_history

    # [2026-05-07] 摘要输入也要先清洗 finish 控制流历史。
    # 原因：system.compactor 可能读取旧 child session 中已经持久化的 finish tool_call/tool_result；
    # 若直接拼接，污染文本会被写进压缩摘要并回流父会话。
    # 做法：复用 L2 的控制流清洗，只移除 finish 伪工具配对，普通业务工具结果保持原样。
    # 目的：压缩摘要记录真实交付内容，不记录运行期协议占位结果。
    messages = sanitize_control_tool_history(messages)

    parts: list[str] = []
    _prev_tid: str = ""
    for m in messages:
        if is_dynamic_context(m):
            continue
        # Task boundary detection
        _meta = m.get("_meta") or {}
        _tid = _meta.get("source_task_id", "") if isinstance(_meta, dict) else ""
        if _tid and _tid != _prev_tid:
            parts.append(f"=== TASK [{_tid}] ===")
            _prev_tid = _tid
        elif not _tid and _prev_tid:
            _prev_tid = ""
        role = str(m.get("role", "unknown"))
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        text_parts.append(t)
            text = "\n".join(text_parts)
        else:
            text = str(content)
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            call_lines: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(call.get("name") or function.get("name") or "").strip()
                if name in ("finish", "ask"):
                    raw_args = call.get("arguments") if "arguments" in call else function.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(raw_args, dict):
                        delivered = str(raw_args.get("text") or "").strip()
                        if delivered:
                            call_lines.append(f"[delivered via {name}] {delivered[:3000]}")
                            continue
                    call_lines.append(f"[used tool: {name}]")
                elif name:
                    call_lines.append(f"[used tool: {name}]")
            if call_lines:
                text = "\n".join(part for part in [text, *call_lines] if part)
        if len(text) > 20_000:
            text = text[:20_000] + "\n...[truncated]"
        parts.append(f"[{role}]\n{text}")
    return "\n\n---\n\n".join(parts)
