"""Unified compact planner — single entry point for all compact triggers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from engine.compact import (
    _format_messages_for_summary,
    compute_effective_keep_recent,
    is_snipped_message,
    split_task_segments,
)


@dataclass
class CompactPlan:
    status: Literal["dispatch", "skip", "failed"]
    reason: str = ""
    dispatch_input: dict[str, Any] = field(default_factory=dict)
    conversation_text: str = ""
    # 调用者自行使用以下字段
    target_session_id: str = ""
    keep_recent: int = 6


async def prepare_compaction(
    messages: list[dict[str, Any]],
    target_session_id: str,
    node_id: str,
    keep_recent: int = 6,
    threshold_tokens: int = 0,
    max_summary_chars: int = 300_000,
) -> CompactPlan:
    """从消息列表准备 compact 计划。不依赖 LoopState。

    返回 CompactPlan：
    - status='dispatch': 需要 dispatch system.compactor
    - status='skip': 无需压缩（无可压缩内容）
    - status='failed': 出错
    """
    try:
        safe_keep_recent = max(int(keep_recent), 1)
    except (TypeError, ValueError):
        safe_keep_recent = 6
    try:
        safe_threshold_tokens = max(int(threshold_tokens), 0)
    except (TypeError, ValueError):
        safe_threshold_tokens = 0
    try:
        safe_max_summary_chars = max(int(max_summary_chars), 0)
    except (TypeError, ValueError):
        safe_max_summary_chars = 300_000

    target_sid = str(target_session_id or "").strip()
    try:
        # [2026-06-14] Why: the compactor was receiving the ENTIRE conversation
        # including keep_recent segments that will be kept verbatim, wasting tokens
        # and producing redundant summaries. How: split conversation into task
        # segments first, only send segments[:-keep_recent] to the compactor.
        # Purpose: compactor only summarizes what will actually be replaced.
        compact_messages = [
            message for message in messages
            if isinstance(message, dict)
            and message.get("role") != "system"
        ]

        # Split into prefix_msgs (old compact_summary + snipped stubs) and real task segments
        prefix_msgs, segments = split_task_segments(compact_messages)
        snipped_in_prefix = sum(1 for m in prefix_msgs if is_snipped_message(m))

        if len(segments) <= 1 and snipped_in_prefix == 0:
            return CompactPlan(
                status="skip",
                reason=f"only {len(segments)} real segments, nothing to compress",
                target_session_id=target_sid,
                keep_recent=safe_keep_recent,
            )

        effective_keep_recent = compute_effective_keep_recent(
            segments,
            keep_recent=safe_keep_recent,
            threshold_tokens=safe_threshold_tokens,
            prefix_msgs=prefix_msgs,
        )

        if len(segments) <= effective_keep_recent and snipped_in_prefix == 0:
            return CompactPlan(
                status="skip",
                reason=f"only {len(segments)} segments, effective_keep_recent={effective_keep_recent}",
                target_session_id=target_sid,
                keep_recent=effective_keep_recent,
            )

        compressible = segments[:-effective_keep_recent] if len(segments) > effective_keep_recent else []
        compressible_messages: list[dict[str, Any]] = list(prefix_msgs)
        for seg in compressible:
            compressible_messages.extend(seg)

        conversation_text = _format_messages_for_summary(compressible_messages)
        reason = ""

        if safe_max_summary_chars > 0 and len(conversation_text) > safe_max_summary_chars:
            original_len = len(conversation_text)
            # [2026-06-06] Why: the compactor model can reject an oversized
            # summarization prompt. How: retain the newest text and then align the
            # cut to the next message separator when possible. Purpose: avoid
            # dispatching a request that is too large while keeping recent context.
            conversation_text = conversation_text[-safe_max_summary_chars:]
            first_sep = conversation_text.find("\n\n---\n\n")
            if first_sep > 0:
                conversation_text = conversation_text[first_sep + len("\n\n---\n\n"):]
            reason = f"truncated:{original_len}"

        if not conversation_text.strip():
            return CompactPlan(
                status="skip",
                reason="no compressible content",
                conversation_text=conversation_text,
                target_session_id=target_sid,
                keep_recent=safe_keep_recent,
            )

        # [2026-06-06] Why: downstream task routing should not care which compact
        # trigger produced the request. How: build the exact compactor input once
        # here and let each caller only add transport-specific metadata. Purpose:
        # pseudo tools, automatic compact, and API compact share one dispatch shape.
        dispatch_input = {
            "instruction": conversation_text,
            "_compact_dispatch": True,
            "context_mode": "fresh",
            "_compact_keep_recent": effective_keep_recent,
            # Why: the entry branch may hold segments that are not merged into the
            # parent yet, so the supervisor cannot re-derive how many leading
            # segments were summarized. How: carry the exact count. Purpose: the
            # applied compaction replaces exactly what the compactor summarized.
            "_compact_compress_count": len(compressible),
            "_compact_threshold_tokens": safe_threshold_tokens,
            "target_session_id": target_sid,
            "_system_task": True,
            "use_context": False,
        }
        return CompactPlan(
            status="dispatch",
            reason=reason,
            dispatch_input=dispatch_input,
            conversation_text=conversation_text,
            target_session_id=target_sid,
            keep_recent=effective_keep_recent,
        )
    except Exception as exc:
        return CompactPlan(
            status="failed",
            reason=str(exc),
            target_session_id=target_sid,
            keep_recent=safe_keep_recent,
        )
