// [2026-05-31] Pure reducer for replaying SupervisorEvent records into the new chat message model.
// Why: the frontend needs one deterministic path for user messages, assistant streams, tools,
// approvals, notices, and audit-only events. How: normalize each event into immutable ChatState
// tables and keep stable ids for turns, blocks, and tool executions. Purpose: WebSocket replay
// and reconnect catch-up can rebuild the same UI state without relying on live component state.
// [AutoC 2026-06-16] Shared reducer helpers live in eventReducerHelpers.ts.
import type { SupervisorEvent } from '../types/chat';
import type {
  Attachment,
  ChatState,
  MessageSource,
  MessageStatus,
  NoticeBlock,
  ToolExecution,
  WsMessage,
} from '../types/message';
import {
  TERMINAL_TOOL_STATUSES,
  type ToolPatch,
  getPayload,
  getString,
  getNumber,
  getBoolean,
  getRecord,
  getSourceInboundSeq,
  hasSourceInboundSeq,
  getAttachments,
  getEventId,
  getUserMessageId,
  appendUnique,
  stringifyJson,
  normalizeToolStatus,
  normalizeApprovalStatus,
  createTextBlock,
  appendOrMergeThinkingBlock,
  appendOrMergeTextBlock,
  hasStreamTextBlock,
  finalizeStreamingBlocks,
  replaceFirstTextBlock,
  replaceStreamTextBlocksWithFinalText,
  setMessageStatus,
  upsertMessage,
  upsertToolRecord,
  stampEvent,
  getTurnKey,
  getConversationId,
  getInboundMessageRole,
  buildMessageSource,
  mergeSource,
  getOrCreateAssistantMessage,
  getAssistantMessageByTurn,
  getOrCreateAssistantMessageForRoundStart,
  findOutboundReplacementTarget,
  replaceAssistantTextWithOutbound,
  applyToolPatchToAssistant,
  recordTaskAndNodeInfo,
  findToolByCallId,
  buildApprovalDetails,
  getToolStatusAfterApproval,
  appendNoticeToAssistant,
  appendLegacyApprovalBlock,
  updateLegacyApprovalBlock,
} from './eventReducerHelpers';

const SYSTEM_NODE_PREFIX = 'system.';
const COMPACTOR_NODE_ID = 'system.compactor';

/**
 * [AutoC 2026-06-16] All event types handled by the chat reducer.
 * Why: reducer switch cases used raw strings, so adding or renaming a handled event
 * could miss compile-time review. How: keep one literal tuple and derive the union
 * type plus runtime set from it. Purpose: backend JSON remains accepted as string,
 * while reducer-owned event handling gains a typed exhaustive switch.
 */
export const CHAT_EVENT_TYPE_VALUES = [
  'inbound_message',
  'stream_delta',
  'stream_end',
  'stream_text_final',
  'outbound_message',
  'intermediate_reply',
  'tool_call_delta',
  'tool_call_start',
  'tool_call_end',
  'task_created',
  'task_started',
  'node_started',
  'task_completed',
  'task_cancelled',
  'approval_requested',
  'approval_decided',
  'llm_retry',
  'node_switch',
  'preempt_injected',
  'system_notice',
] as const;

/** All event types handled by the chat reducer. */
export type ChatEventType = typeof CHAT_EVENT_TYPE_VALUES[number];

const CHAT_EVENT_TYPES: ReadonlySet<string> = new Set(CHAT_EVENT_TYPE_VALUES);

/** Type guard: narrows a raw event type string to a handled ChatEventType. */
export function isChatEventType(type: string): type is ChatEventType {
  // [AutoC 2026-06-16] Keep unknown backend events audit-only.
  // Why: SupervisorEvent.type is a raw string from JSON. How: check the literal set
  // before entering the reducer switch. Purpose: unknown events are stamped in the log
  // without forcing all backend event kinds into chat rendering.
  return CHAT_EVENT_TYPES.has(type);
}

export function createInitialChatState(): ChatState {
  return {
    messagesById: {},
    messageOrderByConversation: {},
    toolExecutionsById: {},
    toolExecutionOrder: [],
    eventLog: [],
    processedEventIds: {},
    lastSeqBySession: {},
    conversationIdsBySession: {},
    assistantMessageByTurn: {},
    userMessageByInboundSeq: {},
    taskTurnKeys: {},
    toolStableIdByExternalId: {},
    toolStableIdByIndex: {},
    approvalBlockById: {},
    nodeByTaskId: {},
  };
}

export function reduceChatEvent(state: ChatState, event: SupervisorEvent): ChatState {
  const eventId = getEventId(event);

  if (state.processedEventIds[eventId]) {
    // Why: EventLog catch-up and WebSocket live delivery can overlap. Returning the
    // original object proves idempotency to callers that rely on structural sharing.
    return state;
  }

  if (!isChatEventType(event.type)) {
    return stampEvent(state, event, eventId);
  }

  // Skip card rendering for system node events (memory_extractor, dream, etc.)
  // Exception: system.compactor task lifecycle events produce user-visible notices.
  const eventNodeId = getString((event.payload || {}).node_id);
  const isCompactorLifecycle = eventNodeId === COMPACTOR_NODE_ID
    && (event.type === 'task_created' || event.type === 'task_completed');
  if (eventNodeId.startsWith(SYSTEM_NODE_PREFIX) && !isCompactorLifecycle) {
    // Still stamp the event so it's tracked in processedEventIds and eventLog
    return stampEvent(state, event, eventId);
  }

  let nextState = stampEvent(state, event, eventId);

  switch (event.type) {
    case 'inbound_message':
      nextState = applyInboundMessage(nextState, event);
      break;
    case 'stream_delta':
      nextState = applyStreamDelta(nextState, event);
      break;
    case 'stream_end':
      nextState = applyStreamEnd(nextState, event);
      break;
    case 'stream_text_final':
      nextState = applyStreamTextFinal(nextState, event);
      break;
    case 'outbound_message':
      nextState = applyOutboundMessage(nextState, event);
      break;
    case 'intermediate_reply':
      nextState = applyIntermediateReply(nextState, event);
      break;
    case 'tool_call_delta':
      nextState = applyToolCallDelta(nextState, event);
      break;
    case 'tool_call_start':
      nextState = applyToolCallStart(nextState, event);
      break;
    case 'tool_call_end':
      nextState = applyToolCallEnd(nextState, event);
      break;
    case 'task_created':
      nextState = applyTaskCreated(nextState, event);
      break;
    case 'task_started':
    case 'node_started':
      nextState = applyTaskOrNodeStarted(nextState, event);
      break;
    case 'task_completed':
      nextState = applyTaskCompleted(nextState, event);
      break;
    case 'task_cancelled':
      nextState = applyTaskCancelled(nextState, event);
      break;
    case 'approval_requested':
      nextState = applyApprovalRequested(nextState, event);
      break;
    case 'approval_decided':
      nextState = applyApprovalDecided(nextState, event);
      break;
    case 'llm_retry':
      nextState = applyLlmRetry(nextState, event);
      break;
    case 'node_switch':
      nextState = applyNodeSwitch(nextState, event);
      break;
    case 'preempt_injected':
      nextState = applyPreemptInjected(nextState, event);
      break;
    case 'system_notice':
      nextState = applySystemNotice(nextState, event);
      break;
    default: {
      // [AutoC 2026-06-16] Exhaustive check for handled chat event types.
      // Why: if ChatEventType grows but the switch is not updated, TypeScript should
      // fail this assignment. How: after all known cases, event.type must be never.
      // Purpose: reducer changes get compile-time coverage instead of silent no-ops.
      const _exhaustiveCheck: never = event.type;
      void _exhaustiveCheck;
      break;
    }
  }

  return nextState;
}

// Why: older focused tests and call sites used the SupervisorEvent wording. How:
// keep these aliases as thin wrappers over the required reducer. Purpose: avoid a
// second replay implementation while the refactor settles on final naming.
export const reduceSupervisorEvent = reduceChatEvent;

function attachmentNameFromPath(path: string): string {
  const clean = path.replace(/\\/g, '/').split('?')[0].split('#')[0];
  try { return decodeURIComponent(clean.split('/').pop() || '附件'); } catch { return clean.split('/').pop() || '附件'; }
}

function attachmentFromPath(pathValue: unknown): Attachment | undefined {
  const path = getString(pathValue).replace(/^file:\/\//, '').replace(/^\/+/, '').trim();
  if (!path) return undefined;
  const imageLike = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(path);
  return {
    name: attachmentNameFromPath(path),
    type: imageLike ? 'image' : 'file',
    path,
    mime_type: imageLike ? 'image/*' : undefined,
  };
}

function mergeAttachments(...groups: readonly (readonly Attachment[] | undefined)[]): Attachment[] {
  const result: Attachment[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    if (!group) continue;
    for (const attachment of group) {
      const key = attachment.path || attachment.url || attachment.name;
      if (key && seen.has(key)) continue;
      if (key) seen.add(key);
      result.push(attachment);
    }
  }
  return result;
}

function attachmentsFromToolArguments(args: Record<string, unknown> | undefined): Attachment[] {
  if (!args) return [];
  const direct = getAttachments(args.attachments);
  const paths = Array.isArray(args.attachment_paths)
    ? args.attachment_paths.map(attachmentFromPath).filter((item): item is Attachment => Boolean(item))
    : [];
  return mergeAttachments(direct, paths);
}

function collectMessageAttachmentsFromTools(state: ChatState, message: WsMessage): Attachment[] {
  const groups: Attachment[][] = [];
  for (const block of message.blocks) {
    if (block.kind !== 'tool') continue;
    for (const toolId of block.toolIds) {
      const tool = state.toolExecutionsById[toolId];
      if (!tool) continue;
      groups.push(mergeAttachments(tool.attachments, attachmentsFromToolArguments(tool.arguments)));
    }
  }
  return mergeAttachments(...groups);
}

export function replaySupervisorEvents(
  events: readonly SupervisorEvent[],
  initialState: ChatState = createInitialChatState(),
): ChatState {
  return events.reduce((currentState, event) => reduceChatEvent(currentState, event), initialState);
}

function applyInboundMessage(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const conversationId = getConversationId(state, event);
  const inboundSeq = event.seq;
  const messageId = getUserMessageId(conversationId, inboundSeq);
  const text = getString(payload.text);
  const attachments = getAttachments(payload.attachments);
  const now = event.ts;
  if (getString(payload.message_type) === 'compact_request') {
    // [2026-06-06] Why: the manual compaction endpoint reuses inbound_message to
    // drive the engine, but that synthetic instruction is not user-authored chat.
    // How: ignore compact_request events in live rendering while keeping them in
    // the supervisor queue. Purpose: the UI shows compaction progress/results
    // without adding a fake user message.
    return state;
  }
  // [AutoC 2026-06-03] Why: supervisor-injected dispatch results arrive as
  // inbound_message events but are not human-authored user input. How: derive the
  // normalized role from the backend message_type contract before creating or
  // merging the message. Purpose: live callbacks render the same way as hydrated
  // history rows.
  const role = getInboundMessageRole(payload);
  const existing = state.messagesById[messageId];
  const textBlock = createTextBlock({
    id: `${messageId}|block:text:${getEventId(event)}`,
    event,
    text,
    delivery: 'final',
    streaming: false,
  });
  const childTaskId = getString(payload.child_task_id) || getString(payload.task_id);
  const childNodeId = getString(payload.child_node_id) || getString(payload.node_id);
  const source: MessageSource = {
    inboundSeq,
    // [AutoC 2026-06-04] Why: dispatch-result inbound payloads now use explicit
    // child_* metadata plus caller_node_id and summary. How: prefer the new fields and
    // keep legacy task_id/node_id fallbacks for older event logs. Purpose: realtime
    // callback cards render from structured data without parsing localized text.
    taskId: childTaskId || undefined,
    childTaskId: childTaskId || undefined,
    nodeId: childNodeId || undefined,
    childNodeId: childNodeId || undefined,
    callerNodeId: getString(payload.caller_node_id) || undefined,
    summary: getString(payload.summary) || undefined,
    nodeName: getString(payload.node_name) || undefined,
    branchSessionId: getString(payload.branch_session_id) || undefined,
    parentSessionId: getString(payload.parent_session_id) || undefined,
    childSessionId: getString(payload.child_session_id) || undefined,
  };

  const message: WsMessage = existing
    ? {
        ...existing,
        role,
        status: 'completed',
        updatedAt: now,
        source: mergeSource(existing.source, source),
        blocks: existing.blocks.length > 0 ? replaceFirstTextBlock(existing.blocks, textBlock) : [textBlock],
        attachments,
        eventIds: appendUnique(existing.eventIds, getEventId(event)),
      }
    : {
        id: messageId,
        conversationId,
        sessionId: event.session_id,
        role,
        status: 'completed',
        createdAt: now,
        updatedAt: now,
        source,
        blocks: [textBlock],
        attachments,
        eventIds: [getEventId(event)],
      };

  const nextState = upsertMessage(state, message);

  return {
    ...nextState,
    conversationIdsBySession: {
      ...nextState.conversationIdsBySession,
      [event.session_id]: conversationId,
    },
    userMessageByInboundSeq: {
      ...nextState.userMessageByInboundSeq,
      [String(inboundSeq)]: messageId,
    },
  };
}

function applyStreamDelta(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const streamType = getString(payload.type);
  const content = getString(payload.content);

  if (!content) {
    return state;
  }

  // [2026-06-02] Why: reply()/ask() closes the visible content for one LLM
  // round, but the task can continue with another LLM request. How: stream deltas
  // resolve through the post-completion boundary helper before appending tokens.
  // Purpose: thinking/text after a reply starts a fresh work card instead of
  // appearing below the reply text.
  let result = getOrCreateAssistantMessageForRoundStart(state, event);

  let message = setMessageStatus(result.message, 'streaming', event);

  if (streamType === 'thinking') {
    message = appendOrMergeThinkingBlock(message, event, content);
  } else if (streamType === 'text') {
    message = appendOrMergeTextBlock(message, event, content, 'stream', true);
  } else {
    return result.state;
  }

  result = { state: upsertMessage(result.state, message), message };
  return result.state;
}

function applyStreamTextFinal(state: ChatState, event: SupervisorEvent): ChatState {
  // [stream-clean 2026-05-31] Why: JSON tool mode leaks protocol markers into
  // stream_delta text. How: when the backend emits stream_text_final with the
  // cleaned plain text, replace all delivery='stream' text blocks with a single
  // delivery='final' block containing the authoritative text. Purpose: the user
  // sees only clean content, not raw <<<TOOL_CALL>>> markers.
  const payload = getPayload(event);
  const cleanText = getString(payload.text);
  const turnKey = getTurnKey(state, event);
  const message = getAssistantMessageByTurn(state, turnKey);

  if (!message) return state;

  const updated = replaceStreamTextBlocksWithFinalText(message, event, cleanText);

  return upsertMessage(state, updated);
}

function applyStreamEnd(state: ChatState, event: SupervisorEvent): ChatState {
  const turnKey = getTurnKey(state, event);
  const message = getAssistantMessageByTurn(state, turnKey);

  if (!message) {
    return state;
  }

  // Why: stream_end only closes the text stream; tool_start/tool_end events for the
  // same turn can still arrive after it. How: check for stream text blocks BEFORE
  // finalizing (which clears streaming flags), then finalize. Purpose: the card
  // remains open for later tool activity and cannot appear finished too early.
  const hadStreamText = hasStreamTextBlock(message);
  const finalized = finalizeStreamingBlocks(message, event);
  const nextStatus: MessageStatus = message.status === 'streaming' || hadStreamText ? 'running_tools' : finalized.status;
  // [2026-06-03] Mark this card's LLM round as complete. Next stream_delta
  // (new thinking/text) will trigger a card break, matching history reconstruction
  // where each assistant message with content is a separate card.
  const withRoundComplete: WsMessage = {
    ...setMessageStatus(finalized, nextStatus, event),
    roundComplete: true,
  };
  return upsertMessage(state, withRoundComplete);
}

function applyOutboundMessage(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const text = getString(payload.text);
  const attachments = getAttachments(payload.attachments);

  // [2026-06-12] Why: even empty outbound_message must finalize the card and clean
  // up pending tool spinners. How: only skip the text/attachment replacement block
  // below, but always run the card completion and tool cleanup logic. Purpose: empty
  // finish(text="") events still close the card and stop loading indicators.
  const hasContent = Boolean(text) || attachments.length > 0;

  // [AutoC 2026-06-04] Why: outbound_message belongs to the same LLM request as the
  // streamed card. How: resolve the reducer turn key first, then use legacy source
  // metadata only as a fallback for payloads that do not carry task_id. Purpose: final
  // outbound text replaces an existing card instead of creating outbound:event-id cards.
  const turnKey = getTurnKey(state, event);
  const targetMessage = findOutboundReplacementTarget(state, event, turnKey);
  let nextState = state;
  let message: WsMessage;

  if (targetMessage) {
    const eventId = getEventId(event);
    message = {
      ...targetMessage,
      updatedAt: event.ts,
      source: mergeSource(targetMessage.source, buildMessageSource(state, event)),
      eventIds: appendUnique(targetMessage.eventIds, eventId),
    };
  } else {
    if (!hasContent) return state; // truly empty and no card to close
    const created = getOrCreateAssistantMessage(state, event, turnKey);
    nextState = created.state;
    message = created.message;
  }

  if (hasContent) {
    message = replaceAssistantTextWithOutbound(message, event, text);
  }

  // Read action_type from backend payload (finish/reply/ask)
  const actionType = getString(payload.action_type) as WsMessage['completionType'] | '';
  const resolvedAttachments = attachments.length > 0
    ? attachments
    : collectMessageAttachmentsFromTools(nextState, message);
  message = {
    ...setMessageStatus(finalizeStreamingBlocks(message, event), 'completed', event),
    attachments: resolvedAttachments.length > 0 ? resolvedAttachments : message.attachments,
    roundComplete: true,
    ...(actionType && { completionType: actionType as WsMessage['completionType'] }),
  };

  nextState = upsertMessage(nextState, message);

  // [2026-06-10] Terminate all pending tool executions on this card when
  // outbound_message arrives. finish/ask pseudo-tools emit tool_call_start
  // but NOT tool_call_end (the task may hand off to a caller chain). The
  // outbound_message is the authoritative completion signal for the whole
  // card. Any tool still in queued/running status is set to success+hidden
  // so the spinner stops.
  for (const block of message.blocks) {
    if (block.kind !== 'tool') continue;
    for (const toolId of block.toolIds) {
      const tool = nextState.toolExecutionsById[toolId];
      if (!tool || (tool.status && TERMINAL_TOOL_STATUSES.has(tool.status))) continue;
      nextState = {
        ...nextState,
        toolExecutionsById: {
          ...nextState.toolExecutionsById,
          [toolId]: {
            ...tool,
            status: 'success',
            hidden: tool.control || tool.hidden,
            updatedAt: event.ts,
          },
        },
      };
    }
  }

  return nextState;
}

function applyIntermediateReply(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const text = getString(payload.text);
  const attachments = getAttachments(payload.attachments);

  if (!text && attachments.length === 0) {
    return state;
  }

  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);

  // Why: intermediate_reply is a complete user-visible chunk, while stream text
  // preceding it may be a preview. How: close active stream blocks before appending
  // the intermediate block. Purpose: the UI does not show an endless streaming mark.
  message = finalizeStreamingBlocks(message, event);
  message = appendOrMergeTextBlock(message, event, text, 'intermediate', false);
  message = {
    ...setMessageStatus(message, 'running_tools', event),
    completionType: 'reply',
    // [AutoC 2026-08-11] Why: intermediate_reply can carry attachment_paths
    // (e.g. generated images). How: merge incoming attachments into the message
    // the same way outbound_message does. Purpose: web UI renders intermediate
    // reply attachments inline instead of silently dropping them.
    ...(attachments.length > 0 ? { attachments: mergeAttachments(attachments, message.attachments || []) } : {}),
  };

  nextState = upsertMessage(nextState, message);
  return nextState;
}

function applyToolCallDelta(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const deltaType = getString(payload.event);

  if (deltaType === 'tool_call_start') {
    const attachments = getAttachments(payload.attachments);
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'args_streaming',
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
      ...(attachments.length > 0 ? { attachments } : {}),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  if (deltaType === 'tool_call_args_delta' || payload.delta !== undefined || payload.arguments_delta !== undefined) {
    const delta = getString(payload.delta) || getString(payload.arguments_delta);
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'args_streaming',
      argumentsTextDelta: delta,
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  if (deltaType === 'tool_call_done') {
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'queued',
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  return state;
}

function applyToolCallStart(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const args = getRecord(payload.arguments);
  const attachments = getAttachments(payload.attachments);
  const patch: ToolPatch = {
    id: getString(payload.tool_call_id) || getString(payload.id),
    itemId: getString(payload.item_id),
    index: getNumber(payload.index),
    name: getString(payload.tool_name) || getString(payload.name),
    status: 'running',
    arguments: args,
    argumentsText: args ? stringifyJson(args) : undefined,
    taskId: getString(payload.task_id),
    nodeId: getString(payload.node_id),
    nodeName: getString(payload.node_name),
    ...(attachments.length > 0 ? { attachments } : {}),
  };

  return applyToolPatchToAssistant(state, event, patch, true);
}

function applyToolCallEnd(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const result = payload.result;
  const resultRecord = getRecord(result);
  const attachments = mergeAttachments(getAttachments(payload.attachments), getAttachments(payload.tool_attachments));
  const patch: ToolPatch = {
    id: getString(payload.tool_call_id) || getString(payload.id),
    itemId: getString(payload.item_id),
    index: getNumber(payload.index),
    name: getString(payload.tool_name) || getString(payload.name),
    status: normalizeToolStatus(payload.status),
    summary: getString(payload.summary),
    result,
    rawInline: getString(payload.raw_inline),
    format: getString(payload.format),
    elapsedMs: getNumber(payload.elapsed_ms),
    error: getString(payload.error) || (resultRecord ? getString(resultRecord.error) : ''),
    taskId: getString(payload.task_id),
    nodeId: getString(payload.node_id),
    nodeName: getString(payload.node_name),
    rejected: getBoolean(payload.rejected) || getBoolean(payload.tool_rejected),
    rejectionCode: getString(payload.rejection_code) || getString(payload.tool_rejection_code),
    resultVisibility: getString(payload.result_visibility) || getString(payload.tool_result_visibility),
    ...(attachments.length > 0 ? { attachments } : {}),
  };

  return applyToolPatchToAssistant(state, event, patch, true);
}

function applyTaskCreated(state: ChatState, event: SupervisorEvent): ChatState {
  // Why: later engine events often carry only task_id. How: bind the task to the
  // current turn as soon as the supervisor snapshot is seen. Purpose: streams and
  // tools emitted by that task merge into the same assistant message.
  const nextState = recordTaskAndNodeInfo(state, event);

  // Why: system.compactor is a synchronous child node that compresses session history.
  // How: insert an info notice into the current assistant card when the compactor task
  // is created. Purpose: the user sees a visual indicator that context is being compressed.
  const payload = getPayload(event);
  if (getString(payload.node_id) === COMPACTOR_NODE_ID) {
    return appendNoticeToAssistant(nextState, event, {
      level: 'info',
      title: '上下文压缩',
      text: '正在压缩对话上下文…',
      eventType: 'context_compacting',
    });
  }

  return nextState;
}

function applyTaskOrNodeStarted(state: ChatState, event: SupervisorEvent): ChatState {
  let nextState = recordTaskAndNodeInfo(state, event);
  const turnKey = getTurnKey(nextState, event);
  const taskId = getString(getPayload(event).task_id);
  const shouldCreate = hasSourceInboundSeq(getPayload(event)) || Boolean(nextState.assistantMessageByTurn[turnKey]);

  if (!taskId || !shouldCreate) {
    return nextState;
  }

  const result = getOrCreateAssistantMessage(nextState, event, turnKey);
  nextState = upsertMessage(result.state, setMessageStatus(result.message, 'running_tools', event));
  return nextState;
}

function applyTaskCompleted(state: ChatState, event: SupervisorEvent): ChatState {
  const nextState = recordTaskAndNodeInfo(state, event);

  // Why: system.compactor completing means the session history JSONL has been rewritten.
  // How: replace the "正在压缩" notice with a completion notice. The actual history refresh
  // happens when the user re-enters the conversation (selectConversation → loadSessionHistory).
  // Purpose: the user knows compression finished without disrupting the active stream.
  const payload = getPayload(event);
  if (getString(payload.node_id) === COMPACTOR_NODE_ID) {
    const compactResult = getRecord(payload.result);
    const compactAction = compactResult ? getString(compactResult.action) : '';
    const compactStatus = getString(payload.status);
    const compactFailed = compactStatus === 'failed' || compactAction === 'fail';
    // 提取具体错误信息
    const compactError = getString(payload.error)
      || (compactResult ? getString(compactResult.error) : '')
      || '';
    const compactText = compactFailed
      ? compactError
        ? `上下文压缩失败: ${compactError}`
        : '上下文压缩失败，将在下次对话时重试。'
      : '对话上下文已压缩。切换会话后将加载压缩后的历史记录。';
    return appendNoticeToAssistant(nextState, event, {
      level: compactFailed ? 'warning' : 'info',
      title: '上下文压缩',
      text: compactText,
      eventType: compactFailed ? 'compact_failed' : 'context_compacted',
    });
  }

  const turnKey = getTurnKey(nextState, event);
  const message = getAssistantMessageByTurn(nextState, turnKey);

  if (!message) {
    return nextState;
  }

  const result = getRecord(payload.result);
  const action = result ? getString(result.action) : '';
  const rawStatus = getString(payload.status);
  const status: MessageStatus = rawStatus === 'cancelled' || action === 'cancelled'
    ? 'cancelled'
    : rawStatus === 'failed' || action === 'fail'
      ? 'failed'
      : 'completed';

  return upsertMessage(nextState, setMessageStatus(finalizeStreamingBlocks(message, event), status, event));
}

function applyTaskCancelled(state: ChatState, event: SupervisorEvent): ChatState {
  const nextState = recordTaskAndNodeInfo(state, event);
  const turnKey = getTurnKey(nextState, event);
  const message = getAssistantMessageByTurn(nextState, turnKey);

  if (!message) {
    return nextState;
  }

  return upsertMessage(nextState, setMessageStatus(finalizeStreamingBlocks(message, event), 'cancelled', event));
}

function applyApprovalRequested(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const approvalId = getString(payload.approval_id);
  const toolCallId = getString(payload.tool_call_id);

  if (!approvalId) {
    return state;
  }

  if (toolCallId) {
    const tool = findToolByCallId(state, toolCallId);
    if (tool) {
      // [AutoC 2026-05-31] Why: approval_requested now identifies the tool call
      // that is waiting. How: update that ToolExecution instead of appending an
      // ApprovalBlock. Purpose: the user sees the prompt and buttons in the same
      // card that already shows the tool name and arguments.
      const updatedTool: ToolExecution = {
        ...tool,
        status: 'awaiting_approval',
        approvalId,
        approvalStatus: 'pending',
        approvalDetails: buildApprovalDetails(payload),
        updatedAt: event.ts,
        eventIds: appendUnique(tool.eventIds, getEventId(event)),
      };
      let nextState = upsertToolRecord(state, updatedTool);
      const message = nextState.messagesById[tool.messageId];
      if (message) {
        nextState = upsertMessage(nextState, setMessageStatus(message, 'awaiting_approval', event));
      }
      return {
        ...nextState,
        approvalBlockById: {
          ...nextState.approvalBlockById,
          [approvalId]: {
            messageId: updatedTool.messageId,
            blockId: updatedTool.blockId || updatedTool.stableId,
            toolCallId,
          },
        },
      };
    }

    // [AutoC 2026-06-21] Why: when a tool pauses before approval, the pending
    // tool call may not have been persisted into /history yet. History recovery
    // then has no ToolExecution for the approval to attach to. How: synthesize the
    // awaiting tool card directly from the pending approval payload. Purpose: a
    // refreshed or reconnected session still shows the actionable approval card.
    const details = getRecord(payload.details) || {};
    const toolName = getString(details.tool_name) || getString(details.tool) || getString(payload.operation) || 'tool';
    const args = getRecord(details.arguments) || getRecord(details.parameters) || details;
    const patch: ToolPatch = {
      id: toolCallId,
      name: toolName,
      status: 'awaiting_approval',
      arguments: args,
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      approvalId,
      approvalStatus: 'pending',
      approvalDetails: buildApprovalDetails(payload),
    };
    let nextState = applyToolPatchToAssistant(state, event, patch, true);
    const createdTool = findToolByCallId(nextState, toolCallId);
    if (createdTool) {
      const message = nextState.messagesById[createdTool.messageId];
      if (message) {
        nextState = upsertMessage(nextState, setMessageStatus(message, 'awaiting_approval', event));
      }
      return {
        ...nextState,
        approvalBlockById: {
          ...nextState.approvalBlockById,
          [approvalId]: {
            messageId: createdTool.messageId,
            blockId: createdTool.blockId || createdTool.stableId,
            toolCallId,
          },
        },
      };
    }
  }

  return appendLegacyApprovalBlock(state, event, payload, approvalId);
}

function applyApprovalDecided(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const approvalId = getString(payload.approval_id);
  const location = approvalId ? state.approvalBlockById[approvalId] : undefined;

  if (!approvalId) {
    return state;
  }

  // [AutoC 2026-05-31] Why: clients can reconnect after approval_requested has
  // already been processed elsewhere, leaving no local approvalBlockById entry.
  // How: prefer the tool_call_id carried by approval_decided and only require the
  // legacy location for ApprovalBlock fallback. Purpose: tool-card decisions still
  // update during partial catch-up or mixed event ordering.
  const toolCallId = getString(payload.tool_call_id) || location?.toolCallId || '';
  if (toolCallId) {
    const tool = findToolByCallId(state, toolCallId);
    if (tool) {
      // [AutoC 2026-05-31] Why: approval decisions should close the inline
      // approval state on the same tool card. How: resolve the approval back to
      // the external tool_call_id and keep existing terminal tool results intact.
      // Purpose: replaying approval_decided updates the card instead of looking for
      // a standalone ApprovalBlock.
      const approvalStatus = normalizeApprovalStatus(payload.status, payload.decision);
      const updatedTool: ToolExecution = {
        ...tool,
        status: getToolStatusAfterApproval(tool.status, approvalStatus),
        approvalId,
        approvalStatus,
        approvalDetails: tool.approvalDetails || buildApprovalDetails(payload),
        updatedAt: event.ts,
        eventIds: appendUnique(tool.eventIds, getEventId(event)),
      };
      let nextState = upsertToolRecord(state, updatedTool);
      const message = nextState.messagesById[tool.messageId];
      if (message) {
        nextState = upsertMessage(nextState, setMessageStatus(message, 'running_tools', event));
      }
      return nextState;
    }
  }

  if (!location) {
    return state;
  }

  return updateLegacyApprovalBlock(state, event, payload, approvalId, location);
}

function applyLlmRetry(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const attempt = getNumber(payload.attempt);
  const maxRetries = getNumber(payload.max_retries);
  const delaySec = getNumber(payload.delay_sec);
  const error = getString(payload.error) || '未知错误';
  // [2026-06-01] Why: retry notices are rendered as user-visible chat notices.
  // How: preserve retry numbers and backend error text while translating the fixed
  // phrases. Purpose: operational notices no longer introduce English UI copy.
  const pieces = ['模型请求将重试。'];

  if (attempt !== undefined && maxRetries !== undefined) {
    pieces.push(`第 ${attempt} 次，共 ${maxRetries} 次。`);
  }
  if (delaySec !== undefined) {
    pieces.push(`${delaySec} 秒后重试。`);
  }
  pieces.push(`原因：${error}`);

  return appendNoticeToAssistant(state, event, {
    level: 'warning',
    title: '模型请求重试',
    text: pieces.join(' '),
    eventType: event.type,
  });
}

// [2026-06-06] Why: preempt_injected is emitted by the engine after a user's
//追加指令 has been applied to the running task. How: append a notice block
// with level=preempt to the active assistant card using the shared helper.
// Purpose: the preempt card appears at the correct time (after backend
// confirmation) and on the correct message (the current working card).
function applyPreemptInjected(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  // The injected message text is stored in the inbound user message that the
  // engine persisted; the event payload itself only carries node/task metadata.
  // For now we show a minimal notice; the full text is visible in the persisted
  // history via the preempt meta flag.
  const text = getString(payload.message) || '追加指令已注入';
  return appendNoticeToAssistant(state, event, {
    level: 'preempt' as NoticeBlock['level'],
    text,
    eventType: event.type,
  });
}

function applyNodeSwitch(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const target = getString(payload.target_node_id);
  const defaultNode = getString(payload.default_node_id);
  // [2026-06-01] Why: node-switch notices are rendered directly in chat. How:
  // preserve target/default identifiers and translate the surrounding message.
  // Purpose: operational notices are localized without altering event payloads.
  const text = target
    ? `已切换到节点 ${target}。`
    : `已清除节点切换${defaultNode ? `；默认节点为 ${defaultNode}。` : '。'}`;

  return appendNoticeToAssistant(state, event, {
    level: 'info',
    title: '节点切换',
    text,
    eventType: event.type,
  });
}

// [AutoC 2026-07-02] Why: backend system_notice events carry structured failure
// information from compact, turn_summary, and memory_extract subsystems. How:
// render them as NoticeBlock in the active assistant card. Purpose: the bot and
// user can see when background maintenance tasks fail.
function applySystemNotice(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const level = (getString(payload.level) || 'warning') as NoticeBlock['level'];
  const title = getString(payload.title) || '系统通知';
  const text = getString(payload.text) || '';
  if (!text) return state;
  return appendNoticeToAssistant(state, event, {
    level,
    title,
    text,
    eventType: event.type,
  });
}

