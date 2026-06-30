// ── Event Reducer Message Block Helpers ────────────────────────────────────
// [AutoC 2026-06-16] Text/thinking block creation, merging, replacement, and status.

import type { SupervisorEvent } from '../types/chat';
import type { MessageStatus, RenderBlock, TextBlock, ThinkingBlock, WsMessage } from '../types/message';
import { appendUnique, getEventId } from './eventReducerPayload';

export function appendOrMergeThinkingBlock(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  // [2026-06-03] Only merge into the very last block if it is an active thinking
  // block. Why: scanning backwards could pull thinking text across intervening
  // text or tool blocks, breaking the natural event arrival order. How: check
  // only message.blocks[-1]. Purpose: blocks remain in the order they arrive.
  const lastBlock = message.blocks[message.blocks.length - 1];

  if (lastBlock?.kind === 'thinking' && lastBlock.streaming !== false) {
    const nextBlock: ThinkingBlock = {
      ...lastBlock,
      text: `${lastBlock.text}${text}`,
      streaming: true,
      updatedAt: event.ts,
      eventIds: appendUnique(lastBlock.eventIds, getEventId(event)),
    };
    return replaceBlock(message, nextBlock, event);
  }

  const block: ThinkingBlock = {
    id: `${message.id}|block:thinking:${getEventId(event)}`,
    kind: 'thinking',
    text,
    streaming: true,
    startedAt: event.ts,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  return {
    ...message,
    blocks: [...message.blocks, block],
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function appendOrMergeTextBlock(
  message: WsMessage,
  event: SupervisorEvent,
  text: string,
  delivery: TextBlock['delivery'],
  streaming: boolean,
): WsMessage {
  const lastBlock = message.blocks[message.blocks.length - 1];

  if (delivery === 'stream' && lastBlock?.kind === 'text' && lastBlock.delivery === 'stream' && lastBlock.streaming !== false) {
    const nextBlock: TextBlock = {
      ...lastBlock,
      text: `${lastBlock.text}${text}`,
      streaming: true,
      updatedAt: event.ts,
      eventIds: appendUnique(lastBlock.eventIds, getEventId(event)),
    };
    return replaceBlock(message, nextBlock, event);
  }

  const block = createTextBlock({
    id: `${message.id}|block:text:${getEventId(event)}`,
    event,
    text,
    delivery,
    streaming,
  });

  return {
    ...message,
    blocks: [...message.blocks, block],
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function createTextBlock(args: {
  id: string;
  event: SupervisorEvent;
  text: string;
  delivery: TextBlock['delivery'];
  streaming: boolean;
}): TextBlock {
  return {
    id: args.id,
    kind: 'text',
    text: args.text,
    delivery: args.delivery,
    streaming: args.streaming,
    createdAt: args.event.ts,
    updatedAt: args.event.ts,
    eventIds: [getEventId(args.event)],
  };
}

export function hasStreamTextBlock(message: WsMessage): boolean {
  // Why: stream_end should preserve the active tool phase only when the message had
  // assistant text streaming. How: check for text blocks delivered through the stream
  // before finalizeStreamingBlocks clears their streaming flag. Purpose: messages do
  // not appear completed while follow-up tools are still expected.
  return message.blocks.some((block) => block.kind === 'text' && block.delivery === 'stream');
}

export function finalizeStreamingBlocks(message: WsMessage, event: SupervisorEvent): WsMessage {
  let changed = false;
  const blocks = message.blocks.map((block) => {
    if ((block.kind === 'thinking' || block.kind === 'text') && block.streaming) {
      changed = true;
      // [2026-06-02] Close active ThinkingBlock timers whenever a card is finalized.
      // Why: reply-boundary cards and finish outbound cards can be finalized without a
      // stream_end event, leaving reasoning blocks visually active. How: text blocks
      // only clear streaming, while thinking blocks also receive endedAt. Purpose: the
      // UI can show a fixed elapsed time instead of an ever-running timer.
      if (block.kind === 'thinking') {
        return {
          ...block,
          streaming: false,
          endedAt: event.ts,
          updatedAt: event.ts,
          eventIds: appendUnique(block.eventIds, getEventId(event)),
        };
      }
      return {
        ...block,
        streaming: false,
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    }
    return block;
  });

  if (!changed) {
    return message;
  }

  return {
    ...message,
    blocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function replaceBlock(message: WsMessage, replacement: RenderBlock, event: SupervisorEvent): WsMessage {
  return {
    ...message,
    blocks: message.blocks.map((block) => (block.id === replacement.id ? replacement : block)),
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function replaceFirstTextBlock(blocks: readonly RenderBlock[], replacement: TextBlock): RenderBlock[] {
  let replaced = false;
  const nextBlocks = blocks.map((block) => {
    if (!replaced && block.kind === 'text') {
      replaced = true;
      return replacement;
    }
    return block;
  });

  return replaced ? nextBlocks : [replacement, ...nextBlocks];
}

function insertBeforeFirstToolBlock(blocks: readonly RenderBlock[], blockToInsert: TextBlock): RenderBlock[] {
  const firstToolIndex = blocks.findIndex((block) => block.kind === 'tool');
  if (firstToolIndex < 0) return [...blocks, blockToInsert];
  return [
    ...blocks.slice(0, firstToolIndex),
    blockToInsert,
    ...blocks.slice(firstToolIndex),
  ];
}

export function replaceAllTextBlocksWithFinalText(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  const finalBlock = text
    ? createTextBlock({
        id: `${message.id}|block:text:outbound:${getEventId(event)}`,
        event,
        text,
        delivery: 'final',
        streaming: false,
      })
    : undefined;
  let inserted = false;
  const blocks: RenderBlock[] = [];

  for (const block of message.blocks) {
    if (block.kind !== 'text') {
      blocks.push(block);
      continue;
    }
    if (finalBlock && !inserted) {
      blocks.push(finalBlock);
      inserted = true;
    }
  }

  const nextBlocks = finalBlock && !inserted ? insertBeforeFirstToolBlock(blocks, finalBlock) : blocks;
  return {
    ...message,
    blocks: nextBlocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function replaceStreamTextBlocksWithFinalText(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  const finalBlock = text.trim()
    ? createTextBlock({
        id: `${message.id}|block:text:clean:${getEventId(event)}`,
        event,
        text,
        delivery: 'final',
        streaming: false,
      })
    : undefined;
  let foundStreamText = false;
  let inserted = false;
  const blocks: RenderBlock[] = [];

  for (const block of message.blocks) {
    if (block.kind === 'text' && block.delivery === 'stream') {
      foundStreamText = true;
      if (finalBlock && !inserted) {
        blocks.push(finalBlock);
        inserted = true;
      }
      continue;
    }
    blocks.push(block);
  }

  const nextBlocks = finalBlock && !inserted
    ? insertBeforeFirstToolBlock(blocks, finalBlock)
    : blocks;
  const changed = foundStreamText || Boolean(finalBlock && !inserted);

  return {
    ...message,
    blocks: changed ? nextBlocks : message.blocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

export function setMessageStatus(message: WsMessage, status: MessageStatus, event: SupervisorEvent): WsMessage {
  if (message.status === status) {
    return {
      ...message,
      updatedAt: event.ts,
      eventIds: appendUnique(message.eventIds, getEventId(event)),
    };
  }

  return {
    ...message,
    status,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}
