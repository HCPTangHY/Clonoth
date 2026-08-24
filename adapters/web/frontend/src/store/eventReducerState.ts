// ── Event Reducer State, Identity and Event Log Helpers ────────────────────
// [AutoC 2026-06-16] Message table writes, event log writes, source merging, and routing IDs.

import type { SupervisorEvent } from '../types/chat';
import type { ChatState, EventLogEntry, MessageSource, TaskNodeInfo, ToolExecution, WsMessage } from '../types/message';
import { LOG_ONLY_EVENTS, MAX_EVENT_LOG, MAX_PROCESSED_IDS, type EventPayload } from './eventReducerShared';
import { getEventId, getPayload, getRecord, getSourceInboundSeq, getString, getUserMessageId, normalizeConversationKey } from './eventReducerPayload';

export function upsertToolRecord(state: ChatState, tool: ToolExecution): ChatState {
  // [AutoC 2026-05-31] Why: approval events update an already-created tool without
  // changing its arguments or block membership. How: replace only the normalized
  // tool table entry. Purpose: avoid manufacturing a second tool patch event just
  // to reflect approval status.
  return {
    ...state,
    toolExecutionsById: {
      ...state.toolExecutionsById,
      [tool.stableId]: tool,
    },
  };
}

export function upsertMessage(state: ChatState, message: WsMessage): ChatState {
  const exists = Boolean(state.messagesById[message.id]);
  const currentOrder = state.messageOrderByConversation[message.conversationId] || [];
  const nextOrder = exists || currentOrder.includes(message.id) ? currentOrder : [...currentOrder, message.id];

  return {
    ...state,
    messagesById: {
      ...state.messagesById,
      [message.id]: message,
    },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [message.conversationId]: nextOrder,
    },
  };
}

export function stampEvent(state: ChatState, event: SupervisorEvent, eventId: string): ChatState {
  const previousSeq = state.lastSeqBySession[event.session_id] || 0;
  const eventLogEntry = buildEventLogEntry(state, event, eventId);

  let nextLog = [...state.eventLog, eventLogEntry];
  if (nextLog.length > MAX_EVENT_LOG) {
    // Why: old event-log rows are useful for inspection but not for rendering current
    // chat state. How: retain the newest rows only, matching the backend log window.
    // Purpose: long sessions do not keep unbounded audit data in browser memory.
    nextLog = nextLog.slice(-MAX_EVENT_LOG);
  }

  let nextProcessedIds: Record<string, true> = {
    ...state.processedEventIds,
    [eventId]: true,
  };
  const processedKeys = Object.keys(nextProcessedIds);
  if (processedKeys.length > MAX_PROCESSED_IDS) {
    // Why: idempotency keys protect against recent WebSocket/EventLog overlap, but
    // keeping every key forever is unnecessary. How: keep the newest half by object
    // insertion order after the cap is exceeded. Purpose: bound memory while retaining
    // the keys most likely to be redelivered during reconnect.
    const keep = processedKeys.slice(Math.floor(processedKeys.length / 2));
    nextProcessedIds = {};
    for (const key of keep) nextProcessedIds[key] = true;
  }

  return {
    ...state,
    eventLog: nextLog,
    processedEventIds: nextProcessedIds,
    lastSeqBySession: {
      ...state.lastSeqBySession,
      [event.session_id]: Math.max(previousSeq, event.seq),
    },
  };
}

export function buildEventLogEntry(state: ChatState, event: SupervisorEvent, eventId: string): EventLogEntry {
  const payload = getPayload(event);
  const turnKey = getTurnKey(state, event);
  const messageId = event.type === 'inbound_message'
    ? getUserMessageId(getConversationId(state, event), event.seq)
    : state.assistantMessageByTurn[turnKey];

  return {
    id: `log:${eventId}`,
    eventId,
    seq: event.seq,
    ts: event.ts,
    sessionId: event.session_id,
    conversationId: getConversationId(state, event),
    type: event.type,
    component: event.component,
    messageId,
    turnKey,
    payload,
    summary: summarizeEvent(event),
    hiddenFromChat: isLogOnlyEvent(event.type),
  };
}

export function getTurnKey(state: ChatState, event: SupervisorEvent): string {
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  const sourceInboundSeq = getSourceInboundSeq(payload);
  const llmRequestId = getString(payload.llm_request_id);

  if (llmRequestId) {
    // [AutoC 2026-06-16] Why: one task can issue several provider requests, and a
    // late outbound_message must replace the card for its own request, not the newest
    // task card. How: make llm_request_id take priority over the task-to-turn binding.
    // Purpose: interleaved provider requests keep stable, exact card ownership.
    const parentKey = sourceInboundSeq !== undefined
      ? `inbound:${sourceInboundSeq}`
      : taskId
        ? `task:${taskId}`
        : `session:${event.session_id}`;
    return `${parentKey}:llm:${llmRequestId}`;
  }

  if (taskId && state.taskTurnKeys[taskId]) {
    // [2026-06-02] Why: one backend task can span multiple visible LLM-round cards
    // after reply()/ask(). How: prefer an explicit task-to-turn binding when it is
    // present, and let source_inbound_seq seed only the initial task card below.
    // Purpose: once the reducer moves a task to a post-reply turn key, later events
    // with the same source inbound sequence do not collapse back into the reply card.
    return state.taskTurnKeys[taskId];
  }

  if (sourceInboundSeq !== undefined) {
    return `inbound:${sourceInboundSeq}`;
  }

  if (taskId) {
    return `task:${taskId}`;
  }

  return `event:${getEventId(event)}`;
}

export function getConversationId(state: ChatState, event: SupervisorEvent): string {
  const payload = getPayload(event);
  const explicitConversation = normalizeConversationKey(getString(payload.conversation_key));

  if (explicitConversation) {
    return explicitConversation;
  }

  const sourceInboundSeq = getSourceInboundSeq(payload);
  if (sourceInboundSeq !== undefined) {
    const userMessageId = state.userMessageByInboundSeq[String(sourceInboundSeq)];
    const userMessage = userMessageId ? state.messagesById[userMessageId] : undefined;
    if (userMessage) {
      return userMessage.conversationId;
    }
  }

  const parentSessionId = getString(payload.parent_session_id);
  if (parentSessionId && state.conversationIdsBySession[parentSessionId]) {
    return state.conversationIdsBySession[parentSessionId];
  }

  return state.conversationIdsBySession[event.session_id] || event.session_id;
}

export function getInboundMessageRole(payload: EventPayload): WsMessage['role'] {
  // [AutoC 2026-06-03] Why: inbound_message is also used for backend-injected
  // dispatch callbacks. How: trust the structured message_type emitted by the
  // supervisor. Purpose: reducer output no longer labels child-task callbacks as
  // ordinary user messages during realtime WebSocket delivery.
  return getString(payload.message_type) === 'dispatch_result' ? 'dispatch_callback' : 'user';
}

export function buildMessageSource(state: ChatState, event: SupervisorEvent): MessageSource {
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  const input = getRecord(payload.input);
  const childTaskId = getString(payload.child_task_id)
    || taskId
    || (input ? getString(input.child_task_id) || getString(input.inbound_child_task_id) : '');
  const childNodeId = getString(payload.child_node_id)
    || getString(payload.node_id)
    || (input ? getString(input.child_node_id) || getString(input.inbound_child_node_id) : '');
  const nodeInfo = taskId ? state.nodeByTaskId[taskId] : undefined;
  const inboundSeq = getSourceInboundSeq(payload);
  const llmRequestId = getString(payload.llm_request_id);
  // [AutoC 2026-08-24] 实时事件的 provider/usage：context_usage 事件在 payload
  // 顶层携带；outbound/stream 类事件通常不带，空值由 mergeSource 跳过。
  const provider = getString(payload.provider);
  const usage = getRecord(payload.usage);

  return {
    inboundSeq,
    llmRequestId: llmRequestId || undefined,
    provider: provider || undefined,
    usage: usage ? { ...usage } : undefined,
    taskId: childTaskId || undefined,
    childTaskId: childTaskId || undefined,
    nodeId: childNodeId || nodeInfo?.nodeId,
    childNodeId: childNodeId || undefined,
    callerNodeId: getString(payload.caller_node_id)
      || (input ? getString(input.caller_node_id) || getString(input.inbound_caller_node_id) : '')
      || undefined,
    summary: getString(payload.summary)
      || (input ? getString(input.summary) || getString(input.inbound_summary) : '')
      || undefined,
    nodeName: getString(payload.node_name) || nodeInfo?.nodeName,
    branchSessionId: getString(payload.branch_session_id) || (input ? getString(input.branch_session_id) : ''),
    parentSessionId: getString(payload.parent_session_id) || (input ? getString(input.parent_session_id) : ''),
    // [AutoC 2026-06-04] Why: downstream task events can also carry callback metadata.
    // How: preserve child-session and child/caller fields in the shared source builder.
    // Purpose: any card anchored to those events keeps the same structured navigation
    // and title data as inbound dispatch_result events.
    childSessionId: getString(payload.child_session_id) || (input ? getString(input.child_session_id) : ''),
  };
}

export function getTaskNodeInfo(event: SupervisorEvent): TaskNodeInfo {
  const payload = getPayload(event);
  return {
    nodeId: getString(payload.node_id) || undefined,
    nodeName: getString(payload.node_name) || undefined,
  };
}

export function mergeTaskNodeInfo(current: TaskNodeInfo | undefined, patch: TaskNodeInfo): TaskNodeInfo {
  return {
    nodeId: patch.nodeId || current?.nodeId,
    nodeName: patch.nodeName || current?.nodeName,
  };
}

export function mergeSource(current: MessageSource, patch: MessageSource): MessageSource {
  return {
    inboundSeq: patch.inboundSeq !== undefined ? patch.inboundSeq : current.inboundSeq,
    llmRequestId: patch.llmRequestId || current.llmRequestId,
    taskId: patch.taskId || current.taskId,
    childTaskId: patch.childTaskId || current.childTaskId,
    nodeId: patch.nodeId || current.nodeId,
    childNodeId: patch.childNodeId || current.childNodeId,
    callerNodeId: patch.callerNodeId || current.callerNodeId,
    summary: patch.summary || current.summary,
    nodeName: patch.nodeName || current.nodeName,
    branchSessionId: patch.branchSessionId || current.branchSessionId,
    parentSessionId: patch.parentSessionId || current.parentSessionId,
    // [AutoC 2026-06-04] Why: message sources are merged across related events.
    // How: carry forward existing child/caller metadata unless a newer patch supplies
    // it. Purpose: dispatch callback navigation and titles survive later updates.
    childSessionId: patch.childSessionId || current.childSessionId,
    // [AutoC 2026-08-24] 上游元信息透传：provider/usage/providerMetadata 由
    // context_usage 事件或 outbound 事件补齐，合并时后到的有效值覆盖。
    provider: patch.provider || current.provider,
    providerMetadata: patch.providerMetadata || current.providerMetadata,
    usage: patch.usage || current.usage,
  };
}

export function isLogOnlyEvent(type: string): boolean {
  // [2026-06-06] Why: preempt_injected must reach the reducer so it can append
  // a notice block to the active card. How: exclude it from the blanket
  // preempt_* skip. Other preempt events (acknowledged, requested) remain
  // log-only. Purpose: the preempt card renders at the correct time and on
  // the correct message.
  if (type === 'preempt_injected') return false;
  return LOG_ONLY_EVENTS.has(type)
    || type.startsWith('compact_')
    || type.startsWith('preempt_')
    || type === 'snip_compact';
}

export function summarizeEvent(event: SupervisorEvent): string | undefined {
  const payload = getPayload(event);

  if (event.type === 'handoff_progress') {
    return getString(payload.message) || undefined;
  }
  if (event.type === 'llm_retry') {
    return getString(payload.error) || undefined;
  }
  if (event.type === 'tool_call_start') {
    return getString(payload.tool_name) || undefined;
  }
  if (event.type === 'tool_call_end') {
    return getString(payload.summary) || getString(payload.tool_name) || undefined;
  }
  if (event.type === 'node_switch') {
    return getString(payload.target_node_id) || 'default';
  }
  if (event.type === 'approval_requested') {
    return getString(payload.operation) || undefined;
  }

  return undefined;
}
