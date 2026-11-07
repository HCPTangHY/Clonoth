// ── Session History Loading Helpers ──────────────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts.
// Why: history hydration and child-session history loading are independent of the
// Zustand action definitions. How: keep backend history reads and hydration writes
// here, receiving Zustand set/get from callers. Purpose: chatStore.ts stays focused
// on user actions while history recovery remains testable and cycle-free.

import {
  getSessionChildren,
  getSessionHistoryPage,
  getSessionHistoryStrict,
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

// [2026-10-02] Store 世代守卫：resetState（登出）让 reset 之前发出的历史请求全部失效。
// Why: 登出会清空会话/消息缓存，但仍在飞的 parent 历史分页响应与 child 历史响应
// 回来时照样 set 进 store —— 上一个账号的会话消息会重新填满已清空的 store
// （child 历史还会重建 childSessionMessages 那一格）。How: reset 时递增世代并
// 清空子请求序号表，每次加载在入口捕获当时的世代，写回前比对世代；不匹配直接丢弃。
// Purpose: 只有属于当前 store 世代的响应能落库，登出后的旧响应绝对写不回来。
let storeGeneration = 0;

export function captureStoreGeneration(): number {
  return storeGeneration;
}

export function isCurrentStoreGeneration(generation: number): boolean {
  return generation === storeGeneration;
}

export function invalidateStoreGeneration(): void {
  storeGeneration += 1;
  // 旧世代里的子会话请求序号已无意义（视图与缓存都被清空），一并清掉。
  latestChildSessionLoadBySession.clear();
}

// [2026-10-02] 子会话历史请求序号：只有“同一 session 的最新一次请求”能写回 store。
// Why: 重试与首次加载可能同时在飞，或用户快速在 A/B 子会话之间来回切换，先发的
// 请求后返回就会用旧响应覆盖新响应（串会话/回退到旧内容）。How: 每个 session 记录
// 最新序号，响应回来先比对，过期直接丢弃。Purpose: 异步结果只落到自己那一格。
let childSessionLoadSeq = 0;
const latestChildSessionLoadBySession = new Map<string, number>();

function beginChildSessionLoad(sessionId: string): number {
  const token = ++childSessionLoadSeq;
  latestChildSessionLoadBySession.set(sessionId, token);
  return token;
}

function isLatestChildSessionLoad(sessionId: string, token: number): boolean {
  if (latestChildSessionLoadBySession.get(sessionId) !== token) return false;
  // 消费掉序号，映射只保留在飞请求，避免长时间运行后无限增长。
  latestChildSessionLoadBySession.delete(sessionId);
  return true;
}

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
  const storeGenerationAtEntry = captureStoreGeneration();
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

  // 请求飞行期间登出（resetState）了：这一页属于上一个账号，丢弃。
  if (!isCurrentStoreGeneration(storeGenerationAtEntry)) return;

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
  const storeGenerationAtEntry = captureStoreGeneration();
  const loadToken = beginChildSessionLoad(sessionId);

  let history: StructuredMessage[];
  try {
    // [2026-10-02] 严格读取：404/网络/后端失败必须冒泡，才能区分“无记录”与“失败”。
    history = await getSessionHistoryStrict(sessionId, 200, taskId);
  } catch (error) {
    if (!isCurrentStoreGeneration(storeGenerationAtEntry)) return;
    if (!isLatestChildSessionLoad(sessionId, loadToken)) return;
    // 404 表示该子会话确实不存在（已被 TTL 清理）：这是“无记录”，重试也没意义。
    // 其余错误（网络/5xx/响应异常）记为失败，视图给重试入口。
    const notFound = error instanceof Error && /^404\b/.test(error.message);
    set((state) => ({
      // 保留已有缓存：失败不能把上一个成功结果或事件流实时累积的消息抹掉。
      childSessionMessages: {
        ...state.childSessionMessages,
        [sessionId]: state.childSessionMessages[sessionId] || [],
      },
      childSessionEmpty: {
        ...state.childSessionEmpty,
        [sessionId]: notFound,
      },
      childSessionLoadFailed: {
        ...state.childSessionLoadFailed,
        [sessionId]: !notFound,
      },
    }));
    return;
  }

  // 请求飞行期间登出（resetState）了：旧世代的历史不能重新填满清空后的 store。
  if (!isCurrentStoreGeneration(storeGenerationAtEntry)) return;
  if (!isLatestChildSessionLoad(sessionId, loadToken)) return;

  const conversationId = getChildConversationId(sessionId);
  try {
    set((state) => {
      const hydrated = hydrateStructuredHistory(state, sessionId, conversationId, history, false);
      return {
        ...hydrated,
        childSessionMessages: {
          ...state.childSessionMessages,
          [sessionId]: selectOrderedMessagesFromState(hydrated, conversationId),
        },
        // [2026-09-13] 记录空结果：区分“还在加载”与“已确认无记录”，子会话
        // 视图据此显示明确提示（会话已被 TTL 清理或本身无输出）。
        childSessionEmpty: {
          ...state.childSessionEmpty,
          [sessionId]: history.length === 0,
        },
        childSessionLoadFailed: {
          ...state.childSessionLoadFailed,
          [sessionId]: false,
        },
      };
    });
  } catch {
    // [2026-10-02] 历史体损坏导致 hydration 抛错时，也必须落一个终态，
    // 否则视图永远停在“正在加载子会话记录…”。
    set((state) => ({
      childSessionLoadFailed: {
        ...state.childSessionLoadFailed,
        [sessionId]: true,
      },
    }));
  }
}
