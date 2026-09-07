// [2026-09-05] Render-time merge of fragmented thinking blocks.
// Why: providers with interleaved thinking (GLM reasoning_content, Claude
// interleaved-thinking beta, OpenAI Responses reasoning items, Gemini thought
// parts) may alternate thinking and text deltas inside one assistant round.
// The reducer keeps one block per contiguous arrival run, which renders as
// thinking → text → thinking → tool and splits one chain of thought into
// visually disjoint blocks. How: merge all thinking blocks of one message card
// into a single display block at the first thinking block's position; text and
// tool blocks keep their arrival order. Data blocks stay untouched — this only
// reorders presentation, so hydration, streaming state, and elapsed timers are
// preserved through the merged block's aggregated fields.
// Purpose: one message card shows one thought process, one body, then tools.
import type { RenderBlock, ThinkingBlock } from '../../types/message';

export function mergeThinkingBlocksForDisplay(blocks: readonly RenderBlock[]): RenderBlock[] {
  let thinkingCount = 0;
  for (const block of blocks) {
    if (block.kind === 'thinking') thinkingCount += 1;
  }
  if (thinkingCount <= 1) return [...blocks];

  const result: RenderBlock[] = [];
  let merged: ThinkingBlock | null = null;
  for (const block of blocks) {
    if (block.kind !== 'thinking') {
      result.push(block);
      continue;
    }
    if (!merged) {
      // Copy: the merged display block must never alias store state.
      merged = { ...(block as ThinkingBlock), eventIds: [...(block as ThinkingBlock).eventIds] };
      result.push(merged);
      continue;
    }
    const incoming = block as ThinkingBlock;
    merged.text = merged.text ? `${merged.text}\n\n${incoming.text}` : incoming.text;
    merged.streaming = Boolean(merged.streaming || incoming.streaming);
    if (incoming.updatedAt > merged.updatedAt) merged.updatedAt = incoming.updatedAt;
    const currentEnd = merged.endedAt;
    const nextEnd = incoming.endedAt;
    merged.endedAt = currentEnd && nextEnd
      ? (currentEnd > nextEnd ? currentEnd : nextEnd)
      : (nextEnd || currentEnd);
    for (const eventId of incoming.eventIds) {
      if (!merged.eventIds.includes(eventId)) merged.eventIds.push(eventId);
    }
  }
  return result;
}
