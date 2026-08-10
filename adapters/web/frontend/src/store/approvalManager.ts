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
import { loadAutoApproved, saveAutoApproved } from './chatTypes';
import type { StoreGetter } from './chatTypes';

export const autoApprovedApprovalIds: Set<string> = loadAutoApproved();

export function maybeAutoApproveApprovalRequest(event: SupervisorEvent, get: StoreGetter): void {
  // [AutoC 2026-08-10] Auto-approve based on supervisor trust_level + client prefs.
  // trust_level comes from the supervisor policy layer via the event payload.
  // Only 'workspace' level can be auto-approved. 'trusted' and 'external' always
  // require manual approval regardless of client preference.
  if (event.type !== 'approval_requested') return;
  const payload = event.payload || {};
  const approvalId = typeof payload.approval_id === 'string' ? payload.approval_id : '';
  if (!approvalId || autoApprovedApprovalIds.has(approvalId)) return;

  // Read trust_level from supervisor policy decision
  const trustLevel = typeof payload.trust_level === 'string' ? payload.trust_level : '';

  // Only workspace-level operations can be auto-approved.
  // 'trusted' (workspace_root, extra_roots) and 'external' always require manual review.
  if (trustLevel !== 'workspace') return;

  const state = get();
  const toolName = getToolNameForApprovalEvent(state, event);
  const prefs = useClientPrefsStore.getState();
  if (!toolName || !shouldAutoApproveTool(toolName, prefs.autoApproveTools, prefs.approvalLevel)) return;

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
