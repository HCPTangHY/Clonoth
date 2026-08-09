// ── Chat Store Types & Conversation Metadata Utilities ─────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts. Pure types, factory functions,
// and conversation metadata helpers. No WebSocket, no reducer, no side effects.

import type { ChildSessionInfo } from '../api/supervisorClient';
import type { Attachment, ChatState, WsMessage } from '../types/message';

// ── Types ──────────────────────────────────────────────────────────────────

export interface ConversationMeta {
  id: string;
  sessionId: string;
  title: string;
  updatedAt: string;
}

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export type ChildNodeStatus = 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'cancelled';
export type TaskActivityPhase = 'idle' | 'thinking' | 'generating' | 'tool_call' | 'awaiting_approval';

export interface TaskActivity {
  // [AutoC 2026-06-04] Why: ActiveTasksModal needs a small live status snapshot,
  // not the full event stream. How: store only the current phase, a short detail,
  // and the event timestamp. Purpose: modal rendering stays cheap and transient.
  phase: TaskActivityPhase;
  detail: string;
  lastEventAt: number;
}

export interface ChildNodeState {
  // [2026-06-03] Why: dispatched child agents run in independent Supervisor sessions.
  // How: keep the runtime session id as the stable map key and visible identifier.
  // Purpose: later UI phases can group scout/smith activity under the parent chat.
  sessionId: string;
  nodeId: string;
  parentConversationId: string;
  status: ChildNodeStatus;
  taskId?: string;
  startedAt?: string;
  completedAt?: string;
}

export interface ContextUsageState {
  // [2026-06-05] Why: the composer only needs a compact, normalized snapshot rather
  // than the full backend context-window response. How: store camelCase token counts,
  // a clamped utilization ratio, and the backend source label. Purpose: both API and
  // WebSocket payloads feed one UI-friendly shape.
  effectiveTokens: number;
  compactThreshold: number;
  utilization: number;
  source: string;
}

export interface ChatStoreState extends ChatState {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  isGenerating: boolean;
  connectionStatus: ConnectionStatus;
  generatingBySession: Readonly<Record<string, boolean>>;
  activeTaskBySession: Readonly<Record<string, string>>;
  childNodes: Readonly<Record<string, ChildNodeState>>;
  viewingChildSessionId: string | null;
  // [AutoC 2026-06-18] Optional conversation key for a temporary session view.
  // Why: the System tab can open any session without adding it to the sidebar, and
  // sending into that existing session still needs the original backend key. How:
  // keep the key only on the temporary view state. Purpose: operators can inspect
  // and talk in arbitrary sessions without polluting normal conversation metadata.
  viewingChildConversationKey: string | null;
  // [2026-06-07] Why: WS events use branch session ids, but viewChildSession
  // stores the parent session id. Matching by task_id is reliable because all
  // events from the same task share the same task_id regardless of session.
  viewingChildTaskId: string | null;
  childSessionMessages: Readonly<Record<string, WsMessage[]>>;
  taskActivities: Readonly<Record<string, TaskActivity>>;
  contextUsage: ContextUsageState | null;
  contextUsageBySession: Readonly<Record<string, ContextUsageState>>;
  historyFullyLoaded: boolean;
  historyLoadedCount: number;
  historyTotal: number;
  isLoadingMoreHistory: boolean;

  selectConversation: (id: string) => void;
  selectChildNodes: (conversationId: string) => ChildNodeState[];
  selectHasActiveChildNodes: (conversationId: string) => boolean;
  createConversation: () => string;
  deleteConversation: (id: string) => void;
  renameConversation: (id: string, newTitle: string) => void;
  sendMessage: (text: string, attachments?: any[], entryNodeId?: string, providerOverride?: Record<string, unknown> | null) => Promise<void>;
  retryMessage: (messageId: string, newText?: string) => Promise<void>;
  cancelCurrentTask: () => Promise<void>;
  preemptCurrentTask: (message: string, attachments?: Attachment[]) => Promise<void>;
  resetState: () => void;
  viewChildSession: (sessionId: string, taskId?: string, conversationKey?: string) => void;
  exitChildSession: () => void;
  loadStartup: () => void;
  updateContextUsage: (data: unknown) => void;
  loadMoreHistory: () => Promise<void>;
}

export type StoreSetter = (
  partial:
    | Partial<ChatStoreState>
    | ((state: ChatStoreState) => Partial<ChatStoreState>),
) => void;
export type StoreGetter = () => ChatStoreState;

export type HistoryToolResult = {
  status: 'success' | 'error';
  result?: unknown;
  rawInline?: string;
  format?: string;
  elapsedMs?: number;
  summary?: string;
  attachments?: Attachment[];
  rejected?: boolean;
  rejectionCode?: string;
  resultVisibility?: string;
  isAutoResult?: boolean;
};

export type HistoryToolCall = {
  id?: string;
  name: string;
  arguments?: Record<string, unknown>;
};

export type HistoryThinkingSegment = {
  text: string;
  startedAt?: string;
  endedAt?: string;
};

// ── Constants ──────────────────────────────────────────────────────────────

export const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
export const INTERNAL_USER_MESSAGE_TYPES = new Set(['tool_result', 'tool_result_attachment', 'system', 'summary', 'compact_request']);
export const TERMINAL_TASK_EVENTS = new Set(['task_completed', 'task_cancelled', 'task_failed']);
export const CHILD_NODE_ACTIVE_STATUSES = new Set<ChildNodeStatus>(['running', 'awaiting_approval']);
export const CHILD_NODE_STATUS_BY_EVENT: Readonly<Record<string, ChildNodeStatus | undefined>> = {
  task_created: 'running',
  task_started: 'running',
  approval_requested: 'awaiting_approval',
  approval_decided: 'running',
  task_completed: 'completed',
  task_failed: 'failed',
  task_cancelled: 'cancelled',
};

export const LS_KEY_TITLES = 'clonoth_conversation_titles';
export const LS_KEY_AUTO_APPROVED = 'clonoth_auto_approved_ids';
export const LS_KEY_LAST_ACTIVE_CONVERSATION = 'clonoth_last_active_conversation_id';

// ── LocalStorage Utilities ─────────────────────────────────────────────────

export function loadTitleCache(): Record<string, string> {
  try {
    const raw = localStorage.getItem(LS_KEY_TITLES);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function saveTitleCache(titles: Record<string, string>) {
  try {
    const entries = Object.entries(titles);
    const trimmed = entries.length > 100 ? Object.fromEntries(entries.slice(-100)) : titles;
    localStorage.setItem(LS_KEY_TITLES, JSON.stringify(trimmed));
  } catch {}
}

export function loadLastActiveConversationId(): string {
  try {
    return localStorage.getItem(LS_KEY_LAST_ACTIVE_CONVERSATION) || '';
  } catch {
    return '';
  }
}

export function saveLastActiveConversationId(id: string | null): void {
  try {
    if (id) localStorage.setItem(LS_KEY_LAST_ACTIVE_CONVERSATION, id);
    else localStorage.removeItem(LS_KEY_LAST_ACTIVE_CONVERSATION);
  } catch {}
}

export function loadAutoApproved(): Set<string> {
  try {
    const raw = localStorage.getItem(LS_KEY_AUTO_APPROVED);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

export function saveAutoApproved(ids: Set<string>) {
  try {
    const arr = [...ids];
    const trimmed = arr.length > 200 ? arr.slice(-200) : arr;
    localStorage.setItem(LS_KEY_AUTO_APPROVED, JSON.stringify(trimmed));
  } catch {}
}

// ── Conversation ID / Hash / Meta Factories ────────────────────────────────

export function createConversationId(): string {
  const randomPart = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `conv-${randomPart}`;
}

export function getConversationIdFromHash(): string | null {
  const hash = window.location.hash;
  const match = hash.match(/^#\/chat\/(.+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function setHashForConversation(id: string | null): void {
  const target = id ? `#/chat/${encodeURIComponent(id)}` : '#/';
  if (window.location.hash === target) return;
  window.history.replaceState(null, '', target);
}

export function createConversationMeta(id = createConversationId(), sessionId = ''): ConversationMeta {
  const timestamp = new Date().toISOString();
  return { id, sessionId, title: '新对话', updatedAt: timestamp };
}

export function createStoreBase(): Pick<ChatStoreState, 'conversations' | 'activeConversationId' | 'isGenerating' | 'connectionStatus' | 'generatingBySession' | 'activeTaskBySession' | 'childNodes' | 'viewingChildSessionId' | 'viewingChildConversationKey' | 'viewingChildTaskId' | 'childSessionMessages' | 'taskActivities' | 'contextUsage' | 'contextUsageBySession' | 'historyFullyLoaded' | 'historyLoadedCount' | 'historyTotal' | 'isLoadingMoreHistory'> {
  return {
    conversations: [],
    activeConversationId: null,
    isGenerating: false,
    connectionStatus: 'idle',
    generatingBySession: {},
    activeTaskBySession: {},
    childNodes: {},
    viewingChildSessionId: null,
    viewingChildConversationKey: null,
    viewingChildTaskId: null,
    childSessionMessages: {},
    taskActivities: {},
    contextUsage: null,
    contextUsageBySession: {},
    historyFullyLoaded: false,
    historyLoadedCount: 0,
    historyTotal: 0,
    isLoadingMoreHistory: false,
  };
}

// ── Conversation Key / Title Normalization ──────────────────────────────────

export function normalizeConversationKey(value: string): string {
  if (!value) return '';
  if (value.startsWith('web:')) return value.slice(4);
  return value;
}

export function isEntryBranchSessionId(sessionId: string): boolean {
  return /^branch_\d+$/.test(sessionId);
}

export function titleFromSession(conversationKey: string, sessionId: string): string {
  if (conversationKey) {
    const stripped = conversationKey.replace(/^web:/, '').trim();
    if (stripped) return stripped.length > 24 ? `${stripped.slice(0, 24)}…` : stripped;
  }
  if (sessionId) {
    const suffix = sessionId.slice(-8);
    return `会话 ${suffix}`;
  }
  return '新对话';
}

export function truncateTitle(text: string, limit = 30): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function getInitialTitleFromClientPrefs(text: string, currentTitle: string | undefined): string | undefined {
  if (currentTitle && currentTitle !== '新对话') return undefined;
  const cleaned = text.replace(/\n+/g, ' ').trim();
  return cleaned ? truncateTitle(cleaned) : undefined;
}

// ── Conversation CRUD Helpers ──────────────────────────────────────────────

export function upsertConversationMeta(
  conversations: readonly ConversationMeta[],
  patch: Partial<ConversationMeta> & { id: string },
): ConversationMeta[] {
  const index = conversations.findIndex((conversation) => conversation.id === patch.id);
  if (index >= 0) {
    const current = conversations[index];
    const updated = [...conversations];
    updated[index] = {
      ...current,
      id: patch.id,
      sessionId: (() => {
        if (!patch.sessionId) return current.sessionId;
        if (current.sessionId && !isEntryBranchSessionId(current.sessionId) && isEntryBranchSessionId(patch.sessionId)) {
          return current.sessionId;
        }
        return patch.sessionId;
      })(),
      title: patch.title && patch.title.trim() ? patch.title : current.title,
      updatedAt: patch.updatedAt || current.updatedAt,
    };
    return updated;
  }
  return [
    ...conversations,
    {
      id: patch.id,
      sessionId: patch.sessionId ?? '',
      title: patch.title && patch.title.trim() ? patch.title : '新对话',
      updatedAt: patch.updatedAt ?? new Date().toISOString(),
    },
  ];
}

export function getActiveConversation(state: ChatStoreState): ConversationMeta | undefined {
  return state.conversations.find((conversation) => conversation.id === state.activeConversationId);
}

export function sortConversationsByRecency(conversations: readonly ConversationMeta[]): ConversationMeta[] {
  return [...conversations].sort((a, b) => {
    const da = Date.parse(a.updatedAt) || 0;
    const db = Date.parse(b.updatedAt) || 0;
    return db - da;
  });
}

export function getChildConversationId(sessionId: string): string {
  return `child:${sessionId}`;
}

export function selectOrderedMessagesFromState(state: ChatState, conversationId: string): WsMessage[] {
  const order = state.messageOrderByConversation[conversationId];
  if (!order) return [];
  return order.map((id) => state.messagesById[id]).filter(Boolean);
}

// ── Child Node Helpers ─────────────────────────────────────────────────────

export function normalizeChildNodeStatus(status: string | undefined): ChildNodeStatus {
  if (status === 'running' || status === 'awaiting_approval' || status === 'completed' || status === 'failed' || status === 'cancelled') {
    return status;
  }
  return 'running';
}

export function mergeChildNodesFromSessionChildren(
  existing: Record<string, ChildNodeState>,
  children: ChildSessionInfo[],
  parentConversationId: string,
): Record<string, ChildNodeState> {
  const result = { ...existing };
  for (const child of children) {
    const nodeId = child.node_id || child.session_id;
    const existing_node = result[nodeId];
    result[nodeId] = {
      sessionId: child.session_id,
      nodeId: child.node_id || existing_node?.nodeId || nodeId,
      parentConversationId,
      status: normalizeChildNodeStatus(child.status),
      taskId: child.task_id || existing_node?.taskId,
      startedAt: child.started_at || existing_node?.startedAt,
      completedAt: child.updated_at || existing_node?.completedAt,
    };
  }
  return result;
}

// ── Dispatch Result Detection ──────────────────────────────────────────────

export function isDispatchResultHistoryMessage(message: { message_type?: string; metadata?: Record<string, unknown> }, _text: string): boolean {
  // [AutoC 2026-06-04] Why: dispatch_callback rows from the backend ConversationStore
  // are backend-typed internal messages that should merge into the assistant card, not
  // appear as independent user messages. How: detect by the authoritative backend
  // message_type contract, with only async_dispatch: as a legacy protocol-id fallback
  // for stored rows created before message_type existed. Purpose: remove brittle
  // text-prefix matching and make dispatch detection resilient to formatting changes.
  if (message.message_type === 'dispatch_callback') return true;
  const convKey = message.metadata?.conversation_key;
  if (typeof convKey === 'string' && convKey.startsWith('async_dispatch:')) return true;
  return false;
}

// ── History Load Guard ─────────────────────────────────────────────────────

// Track compacted session ids so the next history load forces a full rebuild
export const _compactedSessionIds = new Set<string>();

export function shouldPreserveConversationMessagesDuringHistoryLoad(
  state: ChatStoreState,
  conversationId: string,
  sessionId: string,
): boolean {
  // [2026-06-03] Why: the global WebSocket keeps feeding events to the reducer even
  // while the user is looking at another conversation. How: if the conversation is
  // actively generating and already has reducer-built messages, keep them and only
  // append history below. Purpose: switching back to a running session sees the live
  // stream cards without a flash of rebuilding from scratch.
  if (_compactedSessionIds.has(sessionId)) {
    _compactedSessionIds.delete(sessionId);
    return false;
  }
  const isGen = Boolean(state.generatingBySession[sessionId]);
  const existingOrder = state.messageOrderByConversation[conversationId];
  return isGen && Boolean(existingOrder?.length);
}

// ── Context Usage Helpers ──────────────────────────────────────────────────

export function clampContextUtilization(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function normalizeContextUsage(data: unknown): ContextUsageState | null {
  if (!data || typeof data !== 'object') return null;
  const record = data as Record<string, unknown>;
  const effectiveTokens = typeof record.effective_tokens === 'number' ? record.effective_tokens : 0;
  const compactThreshold = typeof record.compact_threshold === 'number' ? record.compact_threshold : 0;
  if (!compactThreshold) return null;
  return {
    effectiveTokens,
    compactThreshold,
    utilization: clampContextUtilization(effectiveTokens / compactThreshold),
    source: typeof record.source === 'string' ? record.source : 'unknown',
  };
}

// ── Generic Value Helpers ──────────────────────────────────────────────────

export function getStringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function getNumberValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function stringifyContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value);
}

export function collapseForPreview(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

export function truncateForPreview(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}
