// ── Session Recovery Helpers ─────────────────────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts.
// Why: reconnect and conversation switching need context usage, pending approval,
// and running-task recovery, but those concerns are not store actions themselves.
// How: expose pure helper functions that receive Zustand set/get from chatStore or
// eventPump. Purpose: recovery remains reusable without importing the store singleton.

import {
  getContextWindow,
  getSessionPendingApprovals,
  getSessionRunningTasks,
  type ContextWindowResponse,
} from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';
import type { ChatState } from '../types/message';
import { reduceChatEvent } from './eventReducer';
import { isConversationGenerating } from './eventRouting';
import { getActiveConversation } from './chatTypes';
import type { StoreGetter, StoreSetter } from './chatTypes';
import { loadSessionHistoryIntoStore } from './historyLoader';

export async function loadContextUsageIntoStore(sessionId: string, set: StoreSetter, get: StoreGetter): Promise<void> {
  // [AutoC 2026-06-16] Keep context usage refresh outside chatStore actions.
  // Why: WebSocket terminal events, startup restore, and conversation selection all
  // need the same backend read. How: call getContextWindow and route the normalized
  // payload through the store's public updateContextUsage action. Purpose: preserve
  // one display/update path while avoiding duplicated API code.
  let response: ContextWindowResponse;
  try {
    response = await getContextWindow(sessionId);
  } catch {
    return;
  }
  get().updateContextUsage({ ...response, session_id: response.session_id || sessionId });
}

export async function replayPendingApprovals(sessionId: string, set: StoreSetter, get: StoreGetter): Promise<void> {
  // [AutoC 2026-06-16] Rebuild pending approval cards after reconnect or selection.
  // Why: approval_requested events may have been missed while the WebSocket was down.
  // How: fetch pending approvals and replay them as synthetic reducer events only for
  // the active session. Purpose: users can still decide outstanding approvals after
  // session recovery without chatStore importing reducer details.
  let approvals: any[];
  try {
    approvals = await getSessionPendingApprovals(sessionId);
  } catch {
    return;
  }
  if (!approvals || approvals.length === 0) return;

  const state = get();
  const active = getActiveConversation(state);
  const viewingChildSessionId = state.viewingChildSessionId;
  const isActiveSession = active?.sessionId === sessionId || viewingChildSessionId === sessionId;
  if (!isActiveSession) return;

  const activeConversationKey = active?.id ? `web:${active.id}` : undefined;
  const replaySeqBase = Date.now();

  set((current) => {
    let next = current as ChatState;
    for (const [index, approval] of approvals.entries()) {
      const existingLocation = next.approvalBlockById[approval.approval_id];
      if (existingLocation && next.messagesById[existingLocation.messageId]) continue;
      const existingTool = Object.values(next.toolExecutionsById).find(
        (tool) => tool.approvalId === approval.approval_id && tool.approvalStatus === 'pending',
      );
      if (existingTool) continue;

      const approvalSessionId = approval.session_id || sessionId;
      const syntheticEvent: SupervisorEvent = {
        seq: replaySeqBase + index,
        // [AutoC 2026-06-21] Why: a previous pending-approval replay can be
        // processed before history hydration, then history rebuild removes the
        // synthetic card while processedEventIds still blocks the stable synthetic
        // id. How: use a fresh replay event id each recovery pass. Purpose: pending
        // approvals are re-applied after the recovered tool cards exist.
        event_id: `synthetic-approval-${approval.approval_id}-${replaySeqBase}-${index}`,
        session_id: approvalSessionId,
        type: 'approval_requested',
        ts: approval.requested_at || new Date().toISOString(),
        payload: {
          approval_id: approval.approval_id,
          operation: approval.operation,
          details: approval.details,
          status: 'pending',
          tool_call_id: approval.tool_call_id,
          node_id: approval.node_id,
          task_id: approval.task_id,
          // Route legacy fallback approval blocks to the active parent conversation.
          // Inline tool approvals still attach by tool_call_id regardless of route.
          conversation_key: activeConversationKey,
          parent_session_id: approvalSessionId === sessionId ? undefined : sessionId,
        },
      };
      next = reduceChatEvent(next, syntheticEvent);
    }
    return { ...next };
  });
}

interface RestoreGeneratingStateOptions {
  reloadHistory?: boolean;
}

export async function restoreGeneratingStateFromBackend(
  conversationId: string,
  sessionId: string,
  set: StoreSetter,
  get: StoreGetter,
  options: RestoreGeneratingStateOptions = {},
): Promise<void> {
  // [AutoC 2026-06-16] Restore in-flight generation flags from backend running tasks.
  // Why: after reconnect, local generating flags may be stale. How: query running
  // tasks, keep user-entry tasks only, update generating maps, then reload history
  // if work is still active. Purpose: active streams continue rendering after network
  // recovery without making eventPump depend on chatStore.
  let runningTasks: import('../api/supervisorClient').RunningTaskInfo[] = [];
  try {
    runningTasks = await getSessionRunningTasks(sessionId);
  } catch {
    // Best effort
  }

  const userEntryTasks = runningTasks.filter((task) => {
    if (task.is_user_entry !== undefined) return task.is_user_entry;
    if (task.source_inbound_seq !== undefined) return task.source_inbound_seq != null;
    return true;
  });
  const hasRunningTask = userEntryTasks.length > 0;
  const firstTaskId = hasRunningTask ? userEntryTasks[0].task_id : '';

  set((state) => {
    const wasGenerating = Boolean(state.generatingBySession[sessionId]);
    if (wasGenerating === hasRunningTask) return state;
    const generatingBySession = { ...state.generatingBySession, [sessionId]: hasRunningTask };
    const activeTaskBySession = { ...state.activeTaskBySession };
    if (hasRunningTask && firstTaskId) {
      activeTaskBySession[sessionId] = firstTaskId;
    } else if (!hasRunningTask) {
      delete activeTaskBySession[sessionId];
    }
    const isGenerating = isConversationGenerating(
      state.conversations,
      state.activeConversationId,
      generatingBySession,
      state.isGenerating,
      state.viewingChildSessionId,
    );
    return { ...state, generatingBySession, activeTaskBySession, isGenerating };
  });

  if (hasRunningTask && options.reloadHistory !== false) {
    await loadSessionHistoryIntoStore(conversationId, sessionId, set, get);
  }
}
