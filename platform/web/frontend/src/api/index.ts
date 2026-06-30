export {
  postInbound,
  // [2026-06-03] Export global realtime controls so stores subscribe once to all
  // Supervisor sessions instead of replacing per-session sockets.
  connectGlobalWS,
  disconnectGlobalWS,
  connectSessionWS,
  disconnectSessionWS,
  checkHealth,
  getAdminState,
  checkAdminAuth,
  getNodes,
  // [2026-06-03] Re-export child-session registry reads. Why: feature code should
  // not need to know the concrete supervisorClient file path when using API helpers.
  // How: surface getSessionChildren through the existing API barrel. Purpose: Phase 3
  // child navigation can share the same import style as other session APIs.
  getSessionChildren,
  // [2026-06-01] Why: client settings can import API helpers through the public
  // barrel as other frontend code does. How: re-export the dynamic tool-name API
  // beside the existing admin config helpers. Purpose: future settings code does
  // not need to couple directly to supervisorClient.ts for this endpoint.
  getAllToolNames,
  getModelConfig,
  updateModelConfig,
  decideApproval,
  cancelActiveTasks,
  // [2026-06-06] Why: the chat context popover may import through the API barrel in
  // future UI refactors. How: re-export the typed manual compaction helper beside
  // other session actions. Purpose: callers avoid constructing endpoint URLs inline.
  compactSession,
  // [AutoC 2026-06-04] Why: task-monitor UI code may import from the API barrel.
  // How: re-export the row-level cancellation helper beside session cancellation.
  // Purpose: components keep a stable import surface while using precise task cancel.
  cancelTask,
  resetConversation,
} from './supervisorClient';
export type { AdminState, ChildSessionInfo, HealthState, CompactSessionResponse } from './supervisorClient';
