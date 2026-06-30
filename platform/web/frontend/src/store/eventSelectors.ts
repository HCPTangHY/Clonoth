// [2026-05-31] Selectors for the reducer-backed chat state.
// Why: render components should consume ordered messages, tools, and logs without
// knowing the reducer's normalization tables. How: derive arrays from stable order
// indexes and filter missing IDs defensively. Purpose: later UI steps can switch
// to selector output without coupling React components to event replay internals.
import type { ChatState, EventLogEntry, ToolExecution, WsMessage } from '../types/message';

export function selectMessages(state: ChatState, conversationId: string): WsMessage[] {
  const order = state.messageOrderByConversation[conversationId] || [];
  return order
    .map((messageId) => state.messagesById[messageId])
    .filter((message): message is WsMessage => Boolean(message));
}

export function selectToolExecutions(state: ChatState, toolIds: readonly string[]): ToolExecution[] {
  return toolIds
    .map((toolId) => state.toolExecutionsById[toolId])
    .filter((tool): tool is ToolExecution => Boolean(tool) && !tool.hidden);
}

export function selectEventLog(state: ChatState, sessionId: string, limit = 200): EventLogEntry[] {
  const filtered = state.eventLog.filter((entry) => entry.sessionId === sessionId);
  return limit > 0 ? filtered.slice(-limit) : filtered;
}
