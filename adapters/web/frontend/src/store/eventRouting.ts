// ── WS Event Routing & Child Node State ────────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts. Handles:
//   - Event → conversation ID resolution (resolveEventConversationId)
//   - Session → conversation route seeding
//   - Dispatch child event detection & agent route resolution
//   - Child node lifecycle state machine
//   - Task activity tracking
//   - Generation state (isGenerating) management
//   - Context usage WS event handling

import type { SupervisorEvent } from '../types/chat';
import type { ChatState, WsMessage } from '../types/message';
import type {
  ChatStoreState,
  ChildNodeState,
  ChildNodeStatus,
  ContextUsageState,
  ConversationMeta,
  StoreGetter,
  TaskActivity,
  TaskActivityPhase,
} from './chatTypes';
import {
  CHILD_NODE_ACTIVE_STATUSES,
  CHILD_NODE_STATUS_BY_EVENT,
  TERMINAL_TASK_EVENTS,
  getActiveConversation,
  getChildConversationId,
  getStringValue,
  getNumberValue,
  isRecord,
  normalizeConversationKey,
  selectOrderedMessagesFromState,
  upsertConversationMeta,
  getInitialTitleFromClientPrefs,
  sortConversationsByRecency,
  normalizeContextUsage,
  _compactedSessionIds,
} from './chatTypes';

// ── Constants ──────────────────────────────────────────────────────────────

export const ACTIVE_TASK_EVENTS = new Set(['task_created', 'task_started', 'task_requeued', 'task_resumed', 'task_suspended']);

// ── Payload Extraction Helpers ─────────────────────────────────────────────

export function getEventPayload(event: SupervisorEvent): Record<string, unknown> {
  return isRecord(event.payload) ? event.payload : {};
}

function getNestedRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key];
  return isRecord(value) ? value : {};
}

function getTaskContext(payload: Record<string, unknown>): Record<string, unknown> {
  const input = getNestedRecord(payload, 'input');
  return getNestedRecord(input, 'task_context');
}

function isSystemTaskPayload(payload: Record<string, unknown>): boolean {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getTaskContext(payload);
  return Boolean(
    payload.is_system_task
    || payload._system_task
    || input.is_system_task
    || input._system_task
    || taskContext.is_system_task,
  );
}

export function getEventConversationKey(payload: Record<string, unknown>): string {
  return getStringValue(payload.conversation_key)
    || getStringValue(getNestedRecord(payload, 'input').conversation_key)
    || getStringValue(getTaskContext(payload).conversation_key)
    || getStringValue(getTaskContext(payload).route_conversation_key);
}

export function getEventParentSessionId(payload: Record<string, unknown>): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getTaskContext(payload);
  return getStringValue(payload.parent_session_id)
    || getStringValue(input.parent_session_id)
    || getStringValue(taskContext.parent_session_id);
}

export function getEventBranchSessionId(payload: Record<string, unknown>): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getTaskContext(payload);
  return getStringValue(payload.branch_session_id)
    || getStringValue(input.branch_session_id)
    || getStringValue(taskContext.branch_session_id);
}

export function getEventSourceInboundSeq(payload: Record<string, unknown>): number | undefined {
  const seq = getNumberValue(payload.source_inbound_seq);
  return seq !== undefined && seq > 0 ? seq : undefined;
}

// ── Session ↔ Conversation Lookup ──────────────────────────────────────────

export function findConversationIdBySession(state: ChatStoreState, sessionId: string): string {
  if (!sessionId) return '';
  return state.conversationIdsBySession[sessionId]
    || state.conversations.find((conversation) => conversation.sessionId === sessionId)?.id
    || '';
}

function resolveWebConversationKeyToId(state: ChatStoreState, conversationKey: string): string {
  if (!conversationKey.startsWith('web:')) return '';
  const normalized = normalizeConversationKey(conversationKey);
  return state.conversations.find((conversation) => conversation.id === normalized)?.id || normalized;
}

// ── Agent / Dispatch Child Routing ─────────────────────────────────────────

function getStructuredAgentRouteConversationKey(payload: Record<string, unknown>): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  const inputDispatchOrigin = getNestedRecord(input, '_dispatch_origin');
  const payloadDispatchOrigin = getNestedRecord(payload, 'dispatch_origin');
  const candidates = [
    getStringValue(taskContext.route_conversation_key),
    getStringValue(inputDispatchOrigin.parent_conversation_key),
    getStringValue(payloadDispatchOrigin.parent_conversation_key),
    getStringValue(payload.route_conversation_key),
    getStringValue(payload.parent_conversation_key),
  ];
  return candidates.find((candidate) => candidate.startsWith('web:')) || '';
}

function resolveAgentRouteConversationId(state: ChatStoreState, payload: Record<string, unknown>): string {
  return resolveWebConversationKeyToId(state, getStructuredAgentRouteConversationKey(payload));
}

export function isDispatchChildEvent(payload: Record<string, unknown>): boolean {
  return Boolean(getStructuredAgentRouteConversationKey(payload));
}

export function isAgentEventRoutedToConversation(
  state: ChatStoreState,
  payload: Record<string, unknown>,
  conversationId: string,
): boolean {
  if (!isDispatchChildEvent(payload)) return false;
  if (!conversationId) return false;
  return resolveAgentRouteConversationId(state, payload) === conversationId;
}

// ── Event → Conversation ID Resolution ─────────────────────────────────────

export function resolveEventConversationId(state: ChatStoreState, event: SupervisorEvent): string {
  const payload = getEventPayload(event);
  const conversationKey = getEventConversationKey(payload);

  // Has conversation_key → direct route. A temporary session view can override
  // this route so an arbitrary browsed session stays outside the normal sidebar
  // conversation list while still receiving live events.
  if (conversationKey) {
    const viewingChild = state.viewingChildSessionId;
    const viewingConversationKey = state.viewingChildConversationKey;
    if (viewingChild && (
      event.session_id === viewingChild
      || (viewingConversationKey && conversationKey === viewingConversationKey)
    )) {
      return getChildConversationId(viewingChild);
    }
    if (conversationKey.startsWith('web:')) {
      return normalizeConversationKey(conversationKey);
    }
    if (isDispatchChildEvent(payload)) {
      return resolveAgentRouteConversationId(state, payload);
    }
    return '';
  }

  // No conversation_key — dispatch child still routes via metadata
  if (isDispatchChildEvent(payload)) {
    return resolveAgentRouteConversationId(state, payload);
  }

  // Minimal fallback: session_id direct lookup, with system task filtering
  const directLookup = findConversationIdBySession(state, event.session_id);
  if (directLookup) {
    const isSystemTask = isSystemTaskPayload(payload);
    if (isSystemTask) {
      const RENDERING_EVENT_TYPES = new Set([
        'tool_call_start', 'tool_call_end', 'tool_call_delta',
        'stream_delta', 'stream_end', 'stream_text_final',
        'outbound_message', 'intermediate_reply',
        'context_usage', 'llm_usage',
      ]);
      if (RENDERING_EVENT_TYPES.has(event.type)) return '';
    }
    return directLookup;
  }

  // viewingChildSession fallback for global sessions
  const viewingChild = state.viewingChildSessionId;
  if (viewingChild) {
    const viewingTaskId = (state as ChatStoreState).viewingChildTaskId;
    const eventPayload = getEventPayload(event);
    const eventTaskId = getStringValue(eventPayload.task_id);
    const belongsToChild =
      (viewingTaskId && eventTaskId && eventTaskId === viewingTaskId)
      || event.session_id === viewingChild
      || getStringValue(eventPayload.parent_session_id) === viewingChild
      || getStringValue(eventPayload.route_session_id) === viewingChild
      || getStringValue((eventPayload.input as Record<string, unknown>)?.parent_session_id) === viewingChild;
    if (belongsToChild) return getChildConversationId(viewingChild);
  }

  return '';
}

// ── Route Seeding ──────────────────────────────────────────────────────────

function collectEventRouteSessionIds(event: SupervisorEvent, payload: Record<string, unknown>): string[] {
  const input = getNestedRecord(payload, 'input');
  const routeSessionIds = [
    event.session_id,
    getStringValue(payload.session_id),
    getStringValue(payload.parent_session_id),
    getStringValue(payload.branch_session_id),
    getStringValue(payload.runtime_session_id),
    getStringValue(input.parent_session_id),
    getStringValue(input.branch_session_id),
  ];
  return routeSessionIds.filter((sessionId, index, all) => sessionId && all.indexOf(sessionId) === index);
}

export function seedConversationRouteForEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): ChatStoreState {
  if (!conversationId) return state;
  const payload = getEventPayload(event);
  if (isDispatchChildEvent(payload)) return state;

  const routeSessionIds = collectEventRouteSessionIds(event, payload);
  const conversationIdsBySession = { ...state.conversationIdsBySession };
  let changed = false;
  for (const sessionId of routeSessionIds) {
    if (conversationIdsBySession[sessionId] !== conversationId) {
      conversationIdsBySession[sessionId] = conversationId;
      changed = true;
    }
  }
  return changed ? { ...state, conversationIdsBySession } : state;
}

// ── Child Node State ───────────────────────────────────────────────────────

function getChildNodeIdFromAgentConversationKey(conversationKey: string): string {
  if (!conversationKey.startsWith('agent:')) return '';
  const rest = conversationKey.slice('agent:'.length);
  const separator = rest.indexOf(':');
  return separator > 0 ? rest.slice(0, separator) : '';
}

export function getChildNodeSessionId(event: SupervisorEvent, payload: Record<string, unknown>): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  return getStringValue(payload.child_session_id)
    || getStringValue(input.child_session_id)
    || getStringValue(taskContext.child_session_id)
    || getStringValue(payload.session_id)
    || event.session_id;
}

function getChildNodeId(payload: Record<string, unknown>, previous?: ChildNodeState): string {
  const input = getNestedRecord(payload, 'input');
  const taskContext = getNestedRecord(input, 'task_context');
  const conversationKey = getEventConversationKey(payload);
  return getStringValue(input.entry_node_id)
    || getStringValue(taskContext.node_id)
    || previous?.nodeId
    || getChildNodeIdFromAgentConversationKey(conversationKey)
    || 'unknown';
}

export function updateChildNodesByEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): Readonly<Record<string, ChildNodeState>> {
  const payload = getEventPayload(event);
  if (!isAgentEventRoutedToConversation(state, payload, conversationId)) return state.childNodes;

  const status = CHILD_NODE_STATUS_BY_EVENT[event.type];
  if (!status) return state.childNodes;

  const sessionId = getChildNodeSessionId(event, payload);
  if (!sessionId) return state.childNodes;

  const previous = state.childNodes[sessionId];
  const isTerminal = status === 'completed' || status === 'failed' || status === 'cancelled';
  return {
    ...state.childNodes,
    [sessionId]: {
      sessionId,
      nodeId: getChildNodeId(payload, previous),
      parentConversationId: conversationId,
      status,
      taskId: getStringValue(payload.task_id) || previous?.taskId,
      startedAt: previous?.startedAt || event.ts,
      completedAt: isTerminal ? event.ts : previous?.completedAt,
    },
  };
}

export function selectChildNodesFromState(state: ChatStoreState, conversationId: string): ChildNodeState[] {
  return Object.values(state.childNodes)
    .filter((child) => child.parentConversationId === conversationId)
    .sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
}

export function selectHasActiveChildNodesFromState(state: ChatStoreState, conversationId: string): boolean {
  return selectChildNodesFromState(state, conversationId).some((child) => CHILD_NODE_ACTIVE_STATUSES.has(child.status));
}

// ── Reducer Event Factories (for agent routing) ────────────────────────────

export function createReducerEventForConversation(
  event: SupervisorEvent,
  payload: Record<string, unknown>,
  conversationId: string,
  isAgentChildRoute: boolean,
): SupervisorEvent {
  if (!isAgentChildRoute) return event;
  return {
    ...event,
    payload: {
      ...event.payload,
      conversation_key: `web:${conversationId}`,
      child_conversation_key: getEventConversationKey(payload),
    },
  };
}

export function createReducerEventForChildSession(
  event: SupervisorEvent,
  payload: Record<string, unknown>,
  childSessionId: string,
): SupervisorEvent {
  const childConversationId = getChildConversationId(childSessionId);
  return {
    ...event,
    event_id: `${event.event_id || `${event.session_id}:${event.seq}:${event.type}`}:child-view:${childSessionId}`,
    payload: {
      ...event.payload,
      conversation_key: `web:${childConversationId}`,
      child_conversation_key: getEventConversationKey(payload),
      child_session_id: childSessionId,
    },
  };
}

export function appendAgentRouteEventLog(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): ChatStoreState {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  if (state.processedEventIds[eventId]) return state;
  return {
    ...state,
    processedEventIds: { ...state.processedEventIds, [eventId]: true },
    lastSeqBySession: {
      ...state.lastSeqBySession,
      [event.session_id]: Math.max(state.lastSeqBySession[event.session_id] || 0, event.seq || 0),
    },
    eventLog: [
      ...state.eventLog,
      {
        id: `log:${eventId}`,
        eventId,
        seq: event.seq,
        ts: event.ts,
        sessionId: event.session_id,
        conversationId,
        type: event.type,
        component: event.component,
        payload: event.payload || {},
      },
    ].slice(-3000),
  };
}

// ── Task Activity Tracking ─────────────────────────────────────────────────

function getTaskActivityTimestamp(event: SupervisorEvent): number {
  const parsed = event.ts ? new Date(event.ts).getTime() : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function getTaskActivityKeys(event: SupervisorEvent, payload: Record<string, unknown>): string[] {
  const taskId = getStringValue(payload.task_id);
  const nodeId = getStringValue(payload.node_id);
  const keys = [
    taskId,
    event.session_id && nodeId ? `${event.session_id}:${nodeId}` : '',
    nodeId,
    taskId || nodeId ? '' : event.session_id,
  ];
  return keys.filter((key, index, all): key is string => Boolean(key) && all.indexOf(key) === index);
}

function getTaskActivityDetail(event: SupervisorEvent, payload: Record<string, unknown>): string {
  if (event.type === 'tool_call_start') {
    return getStringValue(payload.tool_name) || getStringValue(payload.name) || getStringValue(payload.operation);
  }
  if (event.type === 'approval_requested') {
    return getStringValue(payload.tool_name) || getStringValue(payload.operation) || getStringValue(payload.name);
  }
  return '';
}

function getTaskActivityPhase(event: SupervisorEvent, payload: Record<string, unknown>): TaskActivityPhase | null {
  if (event.type === 'stream_delta') {
    const deltaType = getStringValue(payload.type);
    if (deltaType === 'thinking') return 'thinking';
    if (deltaType === 'text') return 'generating';
    return null;
  }
  if (event.type === 'tool_call_start') return 'tool_call';
  if (event.type === 'approval_requested') return 'awaiting_approval';
  if (event.type === 'stream_end' || event.type === 'tool_call_end' || event.type === 'approval_decided') return 'idle';
  return null;
}

export function updateTaskActivitiesByEvent(
  current: Readonly<Record<string, TaskActivity>>,
  event: SupervisorEvent,
): Record<string, TaskActivity> {
  const payload = getEventPayload(event);
  const keys = getTaskActivityKeys(event, payload);
  if (keys.length === 0) return { ...current };

  const isTerminal = event.type === 'task_completed' || event.type === 'task_cancelled' || event.type === 'task_failed';
  const hasTaskId = Boolean(getStringValue(payload.task_id));
  const phase = getTaskActivityPhase(event, payload);
  if (!isTerminal && !phase) return { ...current };

  const next = { ...current };
  const lastEventAt = getTaskActivityTimestamp(event);

  if (isTerminal && hasTaskId) {
    for (const key of keys) delete next[key];
    return next;
  }

  const activity: TaskActivity = {
    phase: isTerminal ? 'idle' : phase || 'idle',
    detail: isTerminal ? '' : getTaskActivityDetail(event, payload),
    lastEventAt,
  };
  for (const key of keys) next[key] = activity;
  return next;
}

// ── Generation State ───────────────────────────────────────────────────────

export function isTerminalTaskEvent(event: SupervisorEvent): boolean {
  return TERMINAL_TASK_EVENTS.has(event.type);
}

export function getAffectedSessionIds(state: ChatStoreState, event: SupervisorEvent, conversationId: string): string[] {
  const payload = getEventPayload(event);
  const ids = new Set<string>();
  const add = (value: string) => { if (value) ids.add(value); };

  add(event.session_id);
  add(getStringValue(payload.session_id));
  add(getStringValue(payload.runtime_session_id));
  add(getEventParentSessionId(payload));
  add(getEventBranchSessionId(payload));

  const conversation = state.conversations.find((item) => item.id === conversationId);
  add(conversation?.sessionId || '');

  return [...ids];
}

export function updateGeneratingByEvent(
  state: ChatStoreState,
  event: SupervisorEvent,
  conversationId: string,
): { generatingBySession: Record<string, boolean>; activeTaskBySession: Record<string, string> } {
  const shouldMarkActive = ACTIVE_TASK_EVENTS.has(event.type);
  const payload = getEventPayload(event);
  const outboundActionType = event.type === 'outbound_message'
    ? getStringValue((payload as Record<string, unknown>).action_type) : '';
  const isOutboundFinish = outboundActionType === 'finish' || outboundActionType === 'ask';
  const shouldMarkDone = isTerminalTaskEvent(event) || isOutboundFinish;
  if (!shouldMarkActive && !shouldMarkDone) {
    return {
      generatingBySession: { ...state.generatingBySession },
      activeTaskBySession: { ...state.activeTaskBySession },
    };
  }

  const nextGen = { ...state.generatingBySession };
  const nextTask = { ...state.activeTaskBySession };
  const taskId = getStringValue(payload.task_id);
  const sourceInboundSeq = getEventSourceInboundSeq(payload);
  const isSystemTask = isSystemTaskPayload(payload);
  for (const sessionId of getAffectedSessionIds(state, event, conversationId)) {
    const activeTask = state.activeTaskBySession[sessionId];

    if (shouldMarkDone) {
      if (taskId && activeTask && activeTask !== taskId) continue;
      if (isSystemTask && (!activeTask || activeTask !== taskId)) continue;
      nextGen[sessionId] = false;
      delete nextTask[sessionId];
      continue;
    }

    if (shouldMarkActive) {
      if (isSystemTask) continue;
      if (!sourceInboundSeq && (!taskId || activeTask !== taskId)) continue;
      nextGen[sessionId] = true;
      if (taskId) nextTask[sessionId] = taskId;
    }
  }
  return { generatingBySession: nextGen, activeTaskBySession: nextTask };
}

export function isConversationGenerating(
  conversations: readonly ConversationMeta[],
  activeConversationId: string | null,
  generatingBySession: Readonly<Record<string, boolean>>,
  fallback: boolean,
  visibleSessionId?: string | null,
): boolean {
  if (visibleSessionId) return Boolean(generatingBySession[visibleSessionId]);
  const active = activeConversationId ? conversations.find((conversation) => conversation.id === activeConversationId) : undefined;
  return active?.sessionId ? Boolean(generatingBySession[active.sessionId]) : fallback;
}

// ── Conversation Sync After Event ──────────────────────────────────────────

function getLastEventConversationId(state: ChatState, event: SupervisorEvent, fallbackConversationId: string): string {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  const logEntry = state.eventLog.find((entry) => entry.eventId === eventId);
  return logEntry?.conversationId || fallbackConversationId;
}

function getSidebarSessionIdForEvent(
  existing: ConversationMeta | undefined,
  event: SupervisorEvent,
  payload: Record<string, unknown>,
): string {
  if (existing?.sessionId) return existing.sessionId;
  const parentSessionId = getEventParentSessionId(payload);
  if (parentSessionId) return parentSessionId;
  return getStringValue(payload.session_id) || event.session_id;
}

export function syncConversationsAfterEvent(
  conversations: readonly ConversationMeta[],
  nextChatState: ChatState,
  event: SupervisorEvent,
  fallbackConversationId: string,
): ConversationMeta[] {
  const conversationId = getLastEventConversationId(nextChatState, event, fallbackConversationId);
  if (conversationId.startsWith('child:')) return [...conversations];
  const payload = getEventPayload(event);
  const isInbound = event.type === 'inbound_message';
  const existing = conversations.find((conversation) => conversation.id === conversationId);

  if (!existing && conversationId !== fallbackConversationId) {
    return [...conversations];
  }

  const inboundText = typeof payload.text === 'string' ? payload.text : '';
  const title = isInbound && inboundText && (!existing || existing.title === '新对话' || existing.title === 'New conversation')
    ? getInitialTitleFromClientPrefs(inboundText, existing?.title)
    : undefined;
  const sessionIdForSidebar = getSidebarSessionIdForEvent(existing, event, payload);

  return upsertConversationMeta(conversations, {
    id: conversationId,
    sessionId: sessionIdForSidebar,
    title,
    updatedAt: event.ts || new Date().toISOString(),
  });
}

// ── Context Usage WS Event Handler ─────────────────────────────────────────

export function getContextUsagePayloadFromEvent(event: SupervisorEvent): unknown {
  const payload = getEventPayload(event);
  return isRecord(payload.usage) ? payload.usage : payload;
}

export function maybeUpdateContextUsageFromEvent(event: SupervisorEvent, get: StoreGetter) {
  if (event.type !== 'context_usage') return;
  const payload = getEventPayload(event);
  const nodeId = getStringValue(payload.node_id) || getStringValue((isRecord(payload.usage) ? payload.usage : payload).node_id);
  if (nodeId && nodeId.startsWith('system.')) return;
  const state = get();
  const activeConversation = getActiveConversation(state);
  if (!activeConversation?.sessionId) return;
  const eventBelongsToActive = activeConversation.sessionId === event.session_id
    || state.conversationIdsBySession[event.session_id] === state.activeConversationId;
  if (!eventBelongsToActive) return;

  const usage = isRecord(payload.usage) ? payload.usage : payload;
  const promptTokens = getNumberValue(usage.prompt_tokens);
  if (promptTokens === undefined) return;

  const existing = state.contextUsageBySession[activeConversation.sessionId] || state.contextUsage;
  const threshold = existing?.compactThreshold ?? 0;
  if (!existing || threshold <= 0) return;
  const effectiveTokens = Math.max(0, Math.round(promptTokens));
  if (existing.effectiveTokens > effectiveTokens) return;
  const utilization = Math.min(1, effectiveTokens / threshold);

  state.updateContextUsage({
    session_id: activeConversation.sessionId,
    effective_tokens: effectiveTokens,
    compact_threshold: threshold,
    utilization,
    source: 'llm_usage',
    // [AutoC 2026-08-24] 原样透传 usage：updateContextUsage 的缓存命中率 EMA
    // 需要读 payload.usage.cached_prompt_tokens，此前此处构造对象时丢弃了
    // usage 字段，导致仪表上的缓存率永远不会被实时事件更新。
    usage,
  });
}

// ── Approval Auto-Approve Helpers ──────────────────────────────────────────

export function getToolNameForApprovalEvent(state: ChatStoreState, event: SupervisorEvent): string {
  const payload = event.payload || {};
  const toolCallId = typeof payload.tool_call_id === 'string' ? payload.tool_call_id : '';
  if (toolCallId) {
    const tool = Object.values(state.toolExecutionsById).find((item) => item.id === toolCallId);
    if (tool?.name) return tool.name;
  }
  const details = getNestedRecord(payload, 'details');
  const detailToolName = getStringValue(details.tool_name) || getStringValue(details.tool) || getStringValue(details.name);
  if (detailToolName) return detailToolName;
  return getStringValue(payload.tool_name) || getStringValue(payload.name) || getStringValue(payload.operation);
}
