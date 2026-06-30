// ── Event Reducer Shared Types and Constants ───────────────────────────────
// [AutoC 2026-06-16] Shared constants and reducer-local types.

import type { Attachment, ToolExecution, ToolStatus } from '../types/message';

export const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
// [2026-06-05] Why: context_usage now drives the composer token indicator and
// should stay visible to normal event consumers. How: leave only truly audit-only
// handoff progress in this set. Purpose: the store can process context_usage as a
// regular session event while the reducer still avoids rendering a chat card for it.
export const LOG_ONLY_EVENTS = new Set(['handoff_progress']);

// Why: system nodes (memory_extractor, dream, compactor, turn_summarizer) run within
// the same session but their events should not create user-visible message cards.
// How: check if the event's node_id starts with 'system.' and skip card rendering.
// Purpose: prevent internal maintenance tasks from polluting the chat UI.
const SYSTEM_NODE_PREFIX = 'system.';
// Why: system.compactor task lifecycle events should produce compact notice blocks
// so the user knows context compression is happening. How: exclude compactor from
// the blanket system-node skip and handle its task_created/task_completed specially.
// Purpose: context compression is user-visible unlike memory extraction or dreaming.
const COMPACTOR_NODE_ID = 'system.compactor';
export const TERMINAL_TOOL_STATUSES = new Set<ToolStatus>(['async_started', 'success', 'error', 'cancelled']);
// Why: reconnect catch-up can replay very long sessions. How: bound audit rows and
// idempotency keys in the reducer itself. Purpose: the browser store mirrors backend
// retention behavior instead of growing for the lifetime of the tab.
export const MAX_EVENT_LOG = 3000;
export const MAX_PROCESSED_IDS = 5000;

export type EventPayload = Record<string, unknown>;

export type ToolPatch = {
  id?: string;
  itemId?: string;
  index?: number;
  name?: string;
  status?: ToolStatus;
  arguments?: Record<string, unknown>;
  argumentsText?: string;
  argumentsTextDelta?: string;
  summary?: string;
  result?: unknown;
  rawInline?: string;
  format?: string;
  elapsedMs?: number;
  attachments?: Attachment[];
  error?: string;
  taskId?: string;
  nodeId?: string;
  nodeName?: string;
  rejected?: boolean;
  rejectionCode?: string;
  resultVisibility?: string;
  // [AutoC 2026-05-31] Why: approval state is now part of ToolExecution updates.
  // How: allow reducer patches and direct approval handlers to preserve these
  // fields across later tool_call_end events. Purpose: the ToolCallCard keeps the
  // approval decision visible after the tool produces its result.
  approvalId?: string;
  approvalStatus?: ToolExecution['approvalStatus'];
  approvalDetails?: Record<string, unknown>;
};
