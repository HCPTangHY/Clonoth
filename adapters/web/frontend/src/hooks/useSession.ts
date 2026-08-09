// This hook is added as a small session adapter for the header while the real session API is not connected.
// It derives session information from the active mock conversation instead of making a network request.
// The purpose is to reserve a clean place for future /v1/sessions data without changing layout components later.
import type { Conversation } from '../types';

export const useSession = (conversation: Conversation | null) => ({
  sessionId: conversation?.sessionId ?? 'no-session',
  // [2026-06-01] Why: this hook can still feed visible header text in legacy paths.
  // How: translate only the fallback title while preserving the no-session sentinel.
  // Purpose: inactive conversation UI stays Chinese without changing session logic.
  title: conversation?.title ?? '未选择对话',
  messageCount: conversation?.messages.length ?? 0,
});
