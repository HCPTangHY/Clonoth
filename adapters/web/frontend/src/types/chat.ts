// [2026-05-16] Full type definitions for Clonoth Web frontend
// [AutoC 2026-06-03] Why: legacy chat types share the MessageRole union with the
// v2 renderer during migration. How: include dispatch_callback here as well as in
// types/message.ts. Purpose: older imports do not reject the new callback role.
export type MessageRole = 'user' | 'assistant' | 'system' | 'dispatch_callback';

export interface Attachment {
  name: string;
  size: number;
  url: string;
  type?: 'image' | 'file';
  path?: string;
  mime_type?: string;
  file?: File; // raw File object for upload; excluded from serialization
}

export interface ToolCall {
  id?: string;
  name: string;
  summary: string;
  // [2026-05-17] Preserve raw arguments so the renderer can show details without
  // reconstructing them from a lossy text summary. This keeps Clonoth's flat API
  // compatible with Lim-Code's functionCall/functionResponse rendering model.
  arguments?: Record<string, unknown>;
  nodeId?: string;
  status?: 'success' | 'error';
  // [2026-05-17] Store either a full rejected error or a safe 120-character result
  // preview. Automatic finish/reply "ok" responses intentionally leave this empty.
  result?: string;
  // [2026-05-17] Marks Clonoth's automatic finish/reply tool result so the UI can
  // show completion state without leaking meaningless "ok" result rows.
  isAutoResult?: boolean;
  // [2026-05-17] Keeps rejected tool calls distinguishable from ordinary failures,
  // especially for the dedicated red finish rejection banner.
  rejected?: boolean;
}

export interface ChatMessage {
  id: string;
  conversationId: string;
  role: MessageRole;
  content: string;
  createdAt: string;
  attachments?: Attachment[];
  approval?: ApprovalInfo;
  // Thinking chain (preserved from stream_delta type=thinking)
  thinking?: string;
  // Tool calls executed during this turn
  toolCalls?: ToolCall[];
  // Whether this is an intermediate reply (reply tool, not final)
  isIntermediate?: boolean;
}

export interface Conversation {
  id: string;
  sessionId: string;
  title: string;
  updatedAt: string;
  messages: ChatMessage[];
}

export interface SupervisorEvent {
  seq: number;
  event_id: string;
  ts: string;
  session_id: string;
  type: string;
  component?: string;
  payload: Record<string, any>;
}

export interface NodeDef {
  id: string;
  type: string;
  name?: string;
  description?: string;
  model?: string;
  provider?: string;
  delegate_targets?: string[];
}

export interface ApprovalInfo {
  id: string;
  operation: string;
  details: { path?: string; reason?: string; [key: string]: any };
  status: 'pending' | 'allowed' | 'denied';
}

export interface StreamPreviewState {
  thinkingPreview: string;
  textPreview: string;
  progressLines: string[];
  retryInfo: string;
  thinkingStartTime: number | null;
  isActive: boolean;
}
