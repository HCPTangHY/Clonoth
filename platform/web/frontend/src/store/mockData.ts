// [2026-05-16] Cleaned: only utility functions remain, zero mock data.
import type { Conversation } from '../types';

export const createMessageId = (prefix: string): string => {
  const randomPart = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${randomPart}`;
};

export const createEmptyConversation = (): Conversation => {
  const id = createMessageId('conv');
  const timestamp = new Date().toISOString();
  return {
    id,
    sessionId: '',
    // [2026-06-01] Why: mock conversations can appear in development UI. How:
    // translate the default visible title only. Purpose: local demo data follows
    // the same Chinese interface contract as runtime-created conversations.
    title: '新对话',
    updatedAt: timestamp,
    messages: [],
  };
};
