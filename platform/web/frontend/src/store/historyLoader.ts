// ── Session History Loading Helpers ──────────────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts.
// Why: history hydration and child-session history loading are independent of the
// Zustand action definitions. How: keep backend history reads and hydration writes
// here, receiving Zustand set/get from callers. Purpose: chatStore.ts stays focused
// on user actions while history recovery remains testable and cycle-free.

import {
  getSessionChildren,
  getSessionHistory,
  getSessionHistoryPage,
  type ChildSessionInfo,
  type HistoryPageResponse,
  type StructuredMessage,
} from '../api/supervisorClient';
import { hydrateStructuredHistory } from './historyHydration';
import {
  getChildConversationId,
  mergeChildNodesFromSessionChildren,
  selectOrderedMessagesFromState,
  shouldPreserveConversationMessagesDuringHistoryLoad,
  upsertConversationMeta,
} from './chatTypes';
import type { ChatStoreState, StoreGetter, StoreSetter } from './chatTypes';

export const HISTORY_PAGE_SIZE = 80;

export async function loadSessionHistoryIntoStore(
  conversationId: string,
  sessionId: string,
  set: StoreSetter,
  get: StoreGetter,
): Promise<void> {
  // [AutoC 2026-06-16] Load the latest parent-session history page plus child metadata.
  // Why: selection, startup restore, and reconnect recovery all need the same hydrated
  // history write. How: fetch the first page and child registry together, preserve live
  // streamed messages when needed, then update pagination counters. Purpose: callers
  // share one consistent history loading path without importing the store singleton.
  let children: ChildSessionInfo[] = [];
  let historyPage: HistoryPageResponse;

  try {
    [historyPage, children] = await Promise.all([
      getSessionHistoryPage(sessionId, HISTORY_PAGE_SIZE, 0),
      getSessionChildren(sessionId),
    ]);
  } catch {
    return;
  }

  const { messages: history, total, has_more: hasMore } = historyPage;

  set((state) => {
    const preserveExistingMessages = shouldPreserveConversationMessagesDuringHistoryLoad(state, conversationId, sessionId);
    const hydrated = history.length > 0
      ? hydrateStructuredHistory(state, sessionId, conversationId, history, preserveExistingMessages)
      : state;
    const hydratedStore = hydrated as ChatStoreState;
    const childNodes = mergeChildNodesFromSessionChildren(hydratedStore.childNodes, children, conversationId);
    return {
      ...hydrated,
      childNodes,
      conversations: upsertConversationMeta(state.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
      historyFullyLoaded: preserveExistingMessages
        ? (state.historyFullyLoaded || !hasMore)
        : !hasMore,
      historyLoadedCount: preserveExistingMessages
        ? Math.max(state.historyLoadedCount || 0, history.length)
        : history.length,
      historyTotal: total,
      isLoadingMoreHistory: false,
    };
  });
}

export async function loadChildSessionHistoryIntoStore(
  sessionId: string,
  set: StoreSetter,
  taskId?: string,
): Promise<void> {
  // [AutoC 2026-06-16] Load a child agent session into its derived child conversation.
  // Why: viewing a child node uses a separate message list keyed by child session id.
  // How: fetch structured history, hydrate it under child:<session>, and cache ordered
  // messages in childSessionMessages. Purpose: the child view stays isolated from the
  // parent conversation while using the same hydration code.
  let history: StructuredMessage[] = [];
  try {
    history = await getSessionHistory(sessionId, 200, taskId);
  } catch {
    return;
  }

  set((state) => {
    const conversationId = getChildConversationId(sessionId);
    const hydrated = hydrateStructuredHistory(state, sessionId, conversationId, history, false);
    return {
      ...hydrated,
      childSessionMessages: {
        ...state.childSessionMessages,
        [sessionId]: selectOrderedMessagesFromState(hydrated, conversationId),
      },
    };
  });
}
