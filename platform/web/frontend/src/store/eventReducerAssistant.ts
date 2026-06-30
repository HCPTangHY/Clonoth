// ── Event Reducer Assistant Card Helpers ───────────────────────────────────
// [AutoC 2026-06-16] Assistant card creation, round splitting, and outbound replacement.

import type { SupervisorEvent } from '../types/chat';
import type { ChatState, WsMessage } from '../types/message';
import { TERMINAL_TOOL_STATUSES, type ToolPatch } from './eventReducerShared';
import { appendUnique, getAssistantMessageId, getEventId, getPayload, getSourceInboundSeq, getString } from './eventReducerPayload';
import { finalizeStreamingBlocks, replaceAllTextBlocksWithFinalText, setMessageStatus } from './eventReducerBlocks';
import { buildMessageSource, getConversationId, getTaskNodeInfo, getTurnKey, mergeSource, mergeTaskNodeInfo, upsertMessage } from './eventReducerState';

export function recordTaskAndNodeInfo(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  let nextState = state;

  if (taskId) {
    const turnKey = getTurnKey(state, event);
    nextState = {
      ...nextState,
      taskTurnKeys: {
        ...nextState.taskTurnKeys,
        [taskId]: turnKey,
      },
    };
  }

  const nodeInfo = getTaskNodeInfo(event);
  if (taskId && (nodeInfo.nodeId || nodeInfo.nodeName)) {
    nextState = {
      ...nextState,
      nodeByTaskId: {
        ...nextState.nodeByTaskId,
        [taskId]: mergeTaskNodeInfo(nextState.nodeByTaskId[taskId], nodeInfo),
      },
    };
  }

  return nextState;
}

export function getOrCreateAssistantMessage(
  state: ChatState,
  event: SupervisorEvent,
  turnKey: string,
): { state: ChatState; message: WsMessage } {
  const existingId = state.assistantMessageByTurn[turnKey];
  const existing = existingId ? state.messagesById[existingId] : undefined;
  const eventId = getEventId(event);
  const source = buildMessageSource(state, event);

  if (existing) {
    const nextMessage: WsMessage = {
      ...existing,
      updatedAt: event.ts,
      source: mergeSource(existing.source, source),
      eventIds: appendUnique(existing.eventIds, eventId),
    };
    return { state: upsertMessage(state, nextMessage), message: nextMessage };
  }

  const conversationId = getConversationId(state, event);
  const messageId = getAssistantMessageId(conversationId, turnKey);
  const message: WsMessage = {
    id: messageId,
    conversationId,
    sessionId: event.session_id,
    role: 'assistant',
    status: 'pending',
    createdAt: event.ts,
    updatedAt: event.ts,
    source,
    blocks: [],
    eventIds: [eventId],
  };
  const nextState = upsertMessage({
    ...state,
    assistantMessageByTurn: {
      ...state.assistantMessageByTurn,
      [turnKey]: messageId,
    },
  }, message);

  return { state: nextState, message };
}

export function getAssistantMessageByTurn(state: ChatState, turnKey: string): WsMessage | undefined {
  const messageId = state.assistantMessageByTurn[turnKey];
  return messageId ? state.messagesById[messageId] : undefined;
}

export function findOutboundReplacementTarget(state: ChatState, event: SupervisorEvent, turnKey: string): WsMessage | undefined {
  const payload = getPayload(event);
  const conversationId = getConversationId(state, event);
  const directMessage = getAssistantMessageByTurn(state, turnKey);
  const sourceInboundSeq = getSourceInboundSeq(payload);
  const taskId = getString(payload.task_id);
  const nodeId = getString(payload.node_id);
  const actionType = getString(payload.action_type);
  const llmRequestId = getString(payload.llm_request_id);
  const order = state.messageOrderByConversation[conversationId] || [];
  const isTerminalAction = actionType === 'finish' || actionType === 'ask';

  if (llmRequestId && directMessage && !isTerminalAction) {
    // [AutoC 2026-06-16] Why: llm_request_id identifies the exact provider request,
    // while task_id can span later cards from the same task. How: when a direct
    // request-keyed card exists, use it before the broader task scan. Purpose: late
    // outbound_message events finalize the correct request card.
    return directMessage;
  }

  // [2026-06-30] Why: terminal pseudo tools can split a post-stream card while
  // keeping the same llm_request_id as the stream card. How: for finish/ask,
  // scan newest matching cards instead of returning the direct request card.
  // Purpose: outbound_message closes the finish/ask tool card, not the old text card.

  let lastMatch: WsMessage | undefined;
  for (let index = order.length - 1; index >= 0; index -= 1) {
    const message = state.messagesById[order[index]];
    if (!message || message.role !== 'assistant') continue;

    const matchesTask = Boolean(taskId && (message.source.taskId === taskId || message.source.childTaskId === taskId));
    const matchesRequest = Boolean(llmRequestId && message.source.llmRequestId === llmRequestId);
    const matchesInbound = sourceInboundSeq !== undefined && message.source.inboundSeq === sourceInboundSeq;

    if (taskId && !matchesTask && !(isTerminalAction && matchesRequest && matchesInbound)) continue;
    if (!taskId && sourceInboundSeq !== undefined && message.source.inboundSeq !== sourceInboundSeq) continue;
    if (!taskId && sourceInboundSeq === undefined && directMessage && message.id !== directMessage.id) continue;
    if (nodeId && message.source.nodeId && message.source.nodeId !== nodeId && message.source.childNodeId !== nodeId) continue;

    // For non-terminal: return first match (backward scan = newest)
    if (!isTerminalAction) return message;

    // For terminal: prefer the card that is NOT yet completed (the post-break card)
    if (!message.roundComplete) return message;
    if (!lastMatch) lastMatch = message;
  }

  return lastMatch || directMessage;
}

export function replaceAssistantTextWithOutbound(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  // [AutoC 2026-06-16] Why: outbound final text must not move below existing tool
  // blocks. How: replace the first text block in place and insert missing final text
  // before the first tool block. Purpose: history and live cards keep the stable order
  // thinking → text → tools, so tool rows continue expanding downward.
  return replaceAllTextBlocksWithFinalText(message, event, text);
}

export function shouldBreakCardForNewRound(message: WsMessage): boolean {
  // [2026-06-03] Why: each LLM round (thinking → text → tools) should be its own
  // card, matching hydrateStructuredHistory where each assistant message with content
  // becomes a separate card. How: break on reply/ask completions OR when the previous
  // round's stream has ended (roundComplete). Purpose: live streaming produces the
  // same card boundaries as history reconstruction.
  return message.completionType === 'reply' || message.completionType === 'ask' || message.roundComplete === true;
}

export function allToolsTerminal(state: ChatState, message: WsMessage): boolean {
  // [2026-06-10] Why: Claude does not emit stream_end, so roundComplete is never set.
  // tool_call_start for the next LLM round lands on the same card as the previous
  // round's tools. How: check if every tool on the current message is in a terminal
  // state. If so, a new tool_call_start must belong to a new LLM round. Purpose:
  // providers that skip stream_end still get correct card boundaries.
  for (const block of message.blocks) {
    if (block.kind !== 'tool') continue;
    for (const toolId of block.toolIds) {
      const tool = state.toolExecutionsById[toolId];
      if (!tool) continue;
      if (!tool.status || !TERMINAL_TOOL_STATUSES.has(tool.status)) return false;
    }
  }
  // Only return true if the message actually has tool blocks
  return message.blocks.some((b) => b.kind === 'tool');
}

export function isProviderToolStreamEvent(event: SupervisorEvent): boolean {
  // [2026-06-03] Why: durable tool_call_start/tool_call_end events can be emitted
  // after reply() while still belonging to the same assistant tool-call row, but
  // provider tool_call_delta events mark the streamed start of an LLM response. How:
  // treat only tool_call_delta as a possible post-reply round starter. Purpose: a
  // tool-only streamed next round gets its own card without splitting same-round
  // durable tool lifecycle updates.
  return event.type === 'tool_call_delta';
}

export function hasExistingToolPatchOnMessage(state: ChatState, message: WsMessage, patch: ToolPatch): boolean {
  // [2026-06-03] Why: later args, start, approval, and end events for a tool already
  // rendered on a reply card must update that same tool instead of creating a new
  // card. How: check the reducer's stable external-id and index maps for keys scoped
  // to the current message id. Purpose: card-boundary detection only applies to new
  // tool executions, not existing same-round lifecycle patches.
  const externalKey = patch.id ? `${message.id}|external:${patch.id}` : '';
  const indexKey = patch.index !== undefined ? `${message.id}|index:${patch.index}` : '';
  return Boolean(
    (externalKey && state.toolStableIdByExternalId[externalKey])
    || (indexKey && state.toolStableIdByIndex[indexKey]),
  );
}

export function getOrCreateAssistantMessageForToolPatch(
  state: ChatState,
  event: SupervisorEvent,
  patch: ToolPatch,
): { state: ChatState; message: WsMessage } {
  const turnKey = getTurnKey(state, event);
  const currentMessage = getAssistantMessageByTurn(state, turnKey);

  if (currentMessage && !hasExistingToolPatchOnMessage(state, currentMessage, patch)) {
    // [2026-06-10] Break card in two scenarios:
    // 1. After reply()/ask() or stream_end (roundComplete) — original logic.
    // 2. All existing tools on the card are terminal (success/error) and a new
    //    tool_call_start arrives — this covers providers like Claude that never
    //    emit stream_end, so roundComplete is never set.
    const shouldBreak = shouldBreakCardForNewRound(currentMessage)
      || (event.type === 'tool_call_start' && allToolsTerminal(state, currentMessage));
    if (shouldBreak) {
      return getOrCreateAssistantMessageForRoundStart(state, event);
    }
  }

  return getOrCreateAssistantMessage(state, event, turnKey);
}

export function getOrCreateAssistantMessageForRoundStart(
  state: ChatState,
  event: SupervisorEvent,
): { state: ChatState; message: WsMessage } {
  const turnKey = getTurnKey(state, event);
  const currentMessage = getAssistantMessageByTurn(state, turnKey);

  if (!currentMessage || !shouldBreakCardForNewRound(currentMessage)) {
    return getOrCreateAssistantMessage(state, event, turnKey);
  }

  const completionType = currentMessage.completionType || 'reply';
  const freshTurnKey = `after-${completionType}:${turnKey}:${currentMessage.id}`;
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  // [2026-06-02] Why: the first stream_delta after a reply is the real
  // boundary between LLM rounds, not intermediate_reply or tool lifecycle events.
  // How: close the current reply/ask card, then bind the task to a deterministic
  // key derived from the completed card id rather than from the triggering event
  // id. Purpose: every later event for the same task resolves to one new work
  // card, while same-round tools still remain on the reply card.
  const closedMessage = setMessageStatus(finalizeStreamingBlocks(currentMessage, event), 'completed', event);
  const stateWithClosedMessage = upsertMessage(state, closedMessage);
  const stateWithFreshTurn = taskId
    ? {
        ...stateWithClosedMessage,
        taskTurnKeys: {
          ...stateWithClosedMessage.taskTurnKeys,
          [taskId]: freshTurnKey,
        },
      }
    : stateWithClosedMessage;

  return getOrCreateAssistantMessage(stateWithFreshTurn, event, freshTurnKey);
}
