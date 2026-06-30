// ── Client Approval Automation Manager ──────────────────────────────────────
// [AutoC 2026-06-16] Extracted from eventPump.ts.
// Why: automatic approval policy is client preference logic, not WebSocket pump
// infrastructure. How: keep approval de-duplication, preference lookup, persistence,
// and decideApproval side effects in this module. Purpose: eventPump only dispatches
// realtime events and delegates approval decisions here.

import { decideApproval } from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';
import { shouldAutoApproveTool, useClientPrefsStore } from './clientPrefsStore';
import { getToolNameForApprovalEvent } from './eventRouting';
import { loadAutoApproved, saveAutoApproved, getStringValue, isRecord } from './chatTypes';
import type { StoreGetter } from './chatTypes';

export const autoApprovedApprovalIds: Set<string> = loadAutoApproved();

const GIT_REMOTE_PUSH_PATTERN = /(?:^|[;&|()\n])\s*(?:(?:sudo|command|time)\s+)*(?:env\s+\S+\s+)*git(?:\s+(?:-[A-Za-z](?:\s+\S+)?|--[A-Za-z0-9][A-Za-z0-9-]*(?:=\S+)?))*\s+push(?:\s|$)/i;

function approvalCommand(payload: Record<string, unknown>): string {
  const details = isRecord(payload.details) ? payload.details : {};
  return getStringValue(details.command);
}

function mustKeepManualApproval(toolName: string, payload: Record<string, unknown>): boolean {
  return toolName === 'execute_command' && GIT_REMOTE_PUSH_PATTERN.test(approvalCommand(payload));
}

export function maybeAutoApproveApprovalRequest(event: SupervisorEvent, get: StoreGetter): void {
  // [AutoC 2026-06-16] Decide whether one approval_requested event can be auto-allowed.
  // Why: users can opt into local auto-approval for low-risk tools, but each approval
  // must be handled once. How: resolve the tool name from reducer state, read current
  // client preferences, persist a de-duplication id, and roll it back if the API call
  // fails. Purpose: auto-approval remains deterministic and separate from WebSocket
  // event routing.
  if (event.type !== 'approval_requested') return;
  const payload = event.payload || {};
  const approvalId = typeof payload.approval_id === 'string' ? payload.approval_id : '';
  if (!approvalId || autoApprovedApprovalIds.has(approvalId)) return;

  const state = get();
  const toolName = getToolNameForApprovalEvent(state, event);
  if (!toolName || mustKeepManualApproval(toolName, payload)) return;
  const prefs = useClientPrefsStore.getState();
  if (!shouldAutoApproveTool(toolName, prefs.autoApproveTools, prefs.approvalLevel)) return;

  autoApprovedApprovalIds.add(approvalId);
  saveAutoApproved(autoApprovedApprovalIds);
  void decideApproval(approvalId, 'allow', 'auto-approved by client preference').catch(() => {
    autoApprovedApprovalIds.delete(approvalId);
    saveAutoApproved(autoApprovedApprovalIds);
  });
}

export function resetApprovalState(): void {
  // [AutoC 2026-06-16] Clear approval automation state for store reset.
  // Why: resetState should remove remembered auto-approved ids just as before the
  // extraction. How: clear the exported set and write the empty value to localStorage.
  // Purpose: tests and user cleanup start from a known approval state.
  autoApprovedApprovalIds.clear();
  saveAutoApproved(autoApprovedApprovalIds);
}
