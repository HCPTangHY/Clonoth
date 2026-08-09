// [2026-05-31] Formal WebSocket-driven message model for the chat refactor.
// Why: the UI needs one replayable message shape that can hold stream text,
// thinking, tools, approvals, and notices together instead of splitting live
// preview from final message bubbles. How: define normalized render blocks,
// tool executions, event-log rows, and reducer state without replacing the old
// chat.ts file yet. Purpose: Step 1 can run beside the current store until later
// steps switch the rendering path over to this model.

// [AutoC 2026-06-03] Why: dispatch-result callbacks are neither human input nor
// generic system notices. How: add a dedicated render role while preserving the
// existing user/assistant/system roles. Purpose: history and live callbacks can use
// their own label, color, and child-session navigation affordance.
export type MessageRole = 'user' | 'assistant' | 'system' | 'dispatch_callback';

export interface Attachment {
  name: string;
  size?: number;
  url?: string;
  type?: 'image' | 'file' | string;
  path?: string;
  mime_type?: string;
  file?: File;
}

export interface BlockBase {
  id: string;
  createdAt: string;
  updatedAt: string;
  eventIds: string[];
}

export interface TextBlock extends BlockBase {
  kind: 'text';
  text: string;
  delivery: 'stream' | 'intermediate' | 'final' | 'history';
  streaming?: boolean;
}

export interface ThinkingBlock extends BlockBase {
  kind: 'thinking';
  text: string;
  streaming?: boolean;
  startedAt?: string;  // ISO timestamp: first thinking delta arrived
  endedAt?: string;    // ISO timestamp: stream_end finalized this block
}

export interface ToolBlock extends BlockBase {
  kind: 'tool';
  toolIds: string[];
}

export interface ApprovalBlock extends BlockBase {
  kind: 'approval';
  approvalId: string;
  operation: string;
  details: Record<string, unknown>;
  status: 'pending' | 'allowed' | 'denied';
  decision?: 'allow' | 'deny' | string;
  comment?: string;
}

export interface NoticeBlock extends BlockBase {
  kind: 'notice';
  level: 'info' | 'warning' | 'error' | 'preempt';
  text: string;
  title?: string;
  eventType?: string;
}

export type RenderBlock = TextBlock | ThinkingBlock | ToolBlock | ApprovalBlock | NoticeBlock;

export type ToolStatus =
  | 'args_streaming'
  | 'queued'
  | 'running'
  // [AutoC 2026-05-31] Why: approvals now belong to the tool lifecycle instead
  // of an independent card. How: add an explicit in-between status before the
  // terminal result. Purpose: ToolCallCard can show approval controls in place.
  | 'awaiting_approval'
  | 'async_started'
  | 'success'
  | 'error'
  | 'cancelled';

export interface ToolExecution {
  stableId: string;
  messageId: string;
  blockId?: string;
  id?: string;
  itemId?: string;
  index?: number;
  name: string;
  status: ToolStatus;
  arguments?: Record<string, unknown>;
  argumentsText?: string;
  summary?: string;
  result?: unknown;
  rawInline?: string;
  format?: string;
  elapsedMs?: number;
  attachments?: Attachment[];
  control?: boolean;
  rejected?: boolean;
  rejectionCode?: string;
  resultVisibility?: string;
  hidden?: boolean;
  error?: string;
  // [AutoC 2026-05-31] Why: approval_requested events now carry the tool_call_id
  // they belong to. How: store the approval metadata on ToolExecution itself.
  // Purpose: one tool card can show pending approval, buttons, and final decision.
  approvalId?: string;
  approvalStatus?: 'pending' | 'allowed' | 'denied';
  approvalDetails?: Record<string, unknown>;
  taskId?: string;
  // [AutoC 2026-06-04] Why: a task can span several visible LLM request cards.
  // How: keep the backend request id on the normalized source beside taskId.
  // Purpose: debugging and future hydration can correlate one card to one request.
  llmRequestId?: string;
  nodeId?: string;
  nodeName?: string;
  createdAt: string;
  updatedAt: string;
  eventIds: string[];
}

export type MessageStatus =
  | 'pending'
  | 'streaming'
  | 'running_tools'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface MessageSource {
  inboundSeq?: number;
  taskId?: string;
  // [AutoC 2026-06-04] Why: a task can span several visible LLM request cards.
  // How: keep the backend request id on the normalized source beside taskId.
  // Purpose: debugging and future hydration can correlate one card to one request.
  llmRequestId?: string;
  provider?: string;
  providerMetadata?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  nodeId?: string;
  nodeName?: string;
  branchSessionId?: string;
  parentSessionId?: string;
  // [AutoC 2026-06-04] Why: dispatch-result cards now receive pure structured
  // callback metadata instead of backend-localized text. How: store caller id, child
  // node/task ids, summary, and child session id on the normalized source. Purpose:
  // MessageCard can build the Chinese title and jump action without parsing content.
  callerNodeId?: string;
  childNodeId?: string;
  childTaskId?: string;
  summary?: string;
  childSessionId?: string;
}

export interface WsMessage {
  id: string;
  conversationId: string;
  sessionId: string;
  role: MessageRole;
  status: MessageStatus;
  createdAt: string;
  updatedAt: string;
  source: MessageSource;
  blocks: RenderBlock[];
  attachments?: Attachment[];
  eventIds: string[];
  hydratedFromHistory?: boolean;
  // [2026-06-01] Explicit completion type — replaces heuristic isTaskCompleteMessage.
  completionType?: 'finish' | 'ask' | 'reply';
  // [2026-06-03] Marks that this card's LLM round has ended (stream_end received).
  // Next stream_delta should start a new card.
  roundComplete?: boolean;
}

export interface EventLogEntry {
  id: string;
  eventId: string;
  seq: number;
  ts: string;
  sessionId: string;
  conversationId?: string;
  type: string;
  component?: string;
  messageId?: string;
  turnKey?: string;
  payload: Record<string, unknown>;
  summary?: string;
  hiddenFromChat?: boolean;
}

export interface ApprovalBlockLocation {
  messageId: string;
  blockId: string;
  // [AutoC 2026-05-31] Why: new approvals are anchored to a ToolExecution rather
  // than a standalone ApprovalBlock. How: keep the matching external tool_call_id
  // beside legacy block coordinates. Purpose: approval_decided can update the same
  // tool card while old approval blocks still use messageId/blockId.
  toolCallId?: string;
}

export interface TaskNodeInfo {
  nodeId?: string;
  nodeName?: string;
}

export interface ChatState {
  messagesById: Readonly<Record<string, WsMessage>>;
  messageOrderByConversation: Readonly<Record<string, readonly string[]>>;
  toolExecutionsById: Readonly<Record<string, ToolExecution>>;
  toolExecutionOrder: readonly string[];
  eventLog: readonly EventLogEntry[];
  processedEventIds: Readonly<Record<string, true>>;
  lastSeqBySession: Readonly<Record<string, number>>;
  conversationIdsBySession: Readonly<Record<string, string>>;
  assistantMessageByTurn: Readonly<Record<string, string>>;
  userMessageByInboundSeq: Readonly<Record<string, string>>;
  taskTurnKeys: Readonly<Record<string, string>>;
  toolStableIdByExternalId: Readonly<Record<string, string>>;
  toolStableIdByIndex: Readonly<Record<string, string>>;
  approvalBlockById: Readonly<Record<string, ApprovalBlockLocation>>;
  nodeByTaskId: Readonly<Record<string, TaskNodeInfo>>;
}
