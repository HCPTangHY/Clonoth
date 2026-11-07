// ── Global WebSocket Event Pump ──────────────────────────────────────────────
// [AutoC 2026-06-16] Extracted from chatStore.ts.
// Why: chatStore.ts mixed the realtime connection loop with UI actions and history
// recovery. How: keep WebSocket connection, reconnect state, and event routing here,
// while refreshing server-owned approval rules and receiving Zustand set/get
// as parameters. Purpose: this module owns realtime side effects without importing the store singleton or
// creating store dependency cycles.

import { connectGlobalWS, disconnectGlobalWS, getSessionChildren } from '../api';
import { useSessionApprovalStore } from './sessionApprovalStore';
import { dispatchPluginEvent } from './pluginRuntime';
import { reduceChatEvent } from './eventReducer';
import {
  appendAgentRouteEventLog,
  createReducerEventForChildSession,
  createReducerEventForConversation,
  getChildNodeSessionId,
  getEventPayload,
  isAgentEventRoutedToConversation,
  isConversationGenerating,
  isTerminalTaskEvent,
  maybeUpdateContextUsageFromEvent,
  resolveEventConversationId,
  seedConversationRouteForEvent,
  syncConversationsAfterEvent,
  updateChildNodesByEvent,
  updateGeneratingByEvent,
  updateTaskActivitiesByEvent,
} from './eventRouting';
import {
  _compactedSessionIds,
  getActiveConversation,
  getChildConversationId,
  getStringValue,
  mergeChildNodesFromSessionChildren,
  selectOrderedMessagesFromState,
  sortConversationsByRecency,
} from './chatTypes';
import type { ChatStoreState, StoreGetter, StoreSetter } from './chatTypes';
import {
  loadContextUsageIntoStore,
  replayPendingApprovals,
  restoreGeneratingStateFromBackend,
} from './sessionRecovery';
import { loadSessionHistoryIntoStore } from './historyLoader';

let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

// [2026-09-13] /children 兜底轮询：事件驱动之外补齐子节点列表刷新。
// Why: 会话激活期间 /children 只在切换会话/启动/重连时拉取，子节点的创建与
// 终态在页面停留时只能依赖 WS 任务事件，事件丢失时侧边栏长期不同步。
// How: 每 30s 对当前活跃会话拉一次 /children 增量合并（幂等，按 sessionId 覆写）。
// Purpose: 子节点列表在事件遗漏时也能在半分钟内自愈。
// 并发/串会话保护：上一轮未返回时跳过本轮，避免请求堆叠；响应回来后再读一次
// 活跃会话，会话已切换则丢弃结果，避免把子节点写进错误的会话树。
let childrenPollTimer: ReturnType<typeof setInterval> | null = null;
let childrenPollInFlight = false;

function stopChildrenPolling(): void {
  if (childrenPollTimer) {
    clearInterval(childrenPollTimer);
    childrenPollTimer = null;
  }
  childrenPollInFlight = false;
}

async function pollSessionChildren(set: StoreSetter, get: StoreGetter): Promise<void> {
  if (childrenPollInFlight) return;
  const active = getActiveConversation(get());
  const sessionId = active?.sessionId || '';
  if (!active || !sessionId) return;
  const conversationId = active.id;
  childrenPollInFlight = true;
  try {
    const children = await getSessionChildren(sessionId);
    set((state) => {
      // 请求飞行期间用户可能已切换会话：丢弃过期结果，不写错会话。
      if (getActiveConversation(state)?.sessionId !== sessionId) return {};
      return {
        childNodes: mergeChildNodesFromSessionChildren(state.childNodes, children, conversationId),
      };
    });
  } catch {
    // 后端暂不可达时跳过本轮，下个周期重试
  } finally {
    childrenPollInFlight = false;
  }
}

function startChildrenPolling(set: StoreSetter, get: StoreGetter): void {
  // 幂等：重复调用（重连、重复 startEventPump）只保留一个定时器。
  stopChildrenPolling();
  childrenPollTimer = setInterval(() => {
    void pollSessionChildren(set, get);
  }, 30_000);
}

export function stopEventPump(): void {
  // [AutoC 2026-06-16] Stop the realtime connection from one exported function.
  // Why: resetState must close WebSocket without knowing eventPump internals. How:
  // clear the reconnect timer and delegate socket closure to the API client. Purpose:
  // chatStore reset remains small and deterministic.
  clearReconnectTimer();
  stopChildrenPolling();
  disconnectGlobalWS();
}

export function resetEventPumpState(): void {
  // Logout invalidates cached rules and in-flight reads as well as live events.
  clearReconnectTimer();
  stopChildrenPolling();
  useSessionApprovalStore.getState().reset();
}

export function startEventPump(set: StoreSetter, get: StoreGetter): void {
  // [AutoC 2026-06-16] Start the global WebSocket event pump through injected store
  // accessors. Why: importing the store singleton here would create a cycle. How: all
  // store reads and writes go through set/get supplied by chatStore. Purpose: realtime
  // routing, reconnect recovery, and history reconciliation stay behavior-compatible.
  clearReconnectTimer();
  const currentConnectionStatus = get().connectionStatus;
  if (currentConnectionStatus !== 'open' && currentConnectionStatus !== 'reconnecting') {
    set({ connectionStatus: 'connecting' });
  }
  startChildrenPolling(set, get);

  connectGlobalWS(
    0,
    (event) => {
      // [AutoC 2026-08-22] Plugin event tap: every incoming event reaches plugin
      // subscribers before the store reducer runs, regardless of whether the
      // reducer routes it to a conversation. Slot plugins get reactivity without
      // the host re-rendering for them.
      dispatchPluginEvent(event.type, event.payload);

      let shouldReconcileTerminalTask = false;
      let shouldRefreshContextUsage = false;

      maybeUpdateContextUsageFromEvent(event, get);

      set((state) => {
        const taskActivities = updateTaskActivitiesByEvent(state.taskActivities, event);
        const stateWithTaskActivity = { ...state, taskActivities };
        const conversationId = resolveEventConversationId(stateWithTaskActivity, event);

        if (!conversationId) {
          return { connectionStatus: 'open', taskActivities };
        }

        const payload = getEventPayload(event);
        const isAgentChildRoute = isAgentEventRoutedToConversation(stateWithTaskActivity, payload, conversationId);
        const reducerEvent = createReducerEventForConversation(event, payload, conversationId, isAgentChildRoute);
        const routedState = seedConversationRouteForEvent(stateWithTaskActivity, event, conversationId);
        const childNodes = updateChildNodesByEvent(routedState, event, conversationId);

        if (isAgentChildRoute) {
          const childSessionId = getChildNodeSessionId(event, payload);
          let nextState = appendAgentRouteEventLog({ ...routedState, childNodes }, event, conversationId);
          const viewChild = state.viewingChildSessionId;
          const childMatch = viewChild && (
            childSessionId === viewChild
            || event.session_id === viewChild
            || getStringValue(payload.parent_session_id) === viewChild
          );
          const effectiveChildId = childSessionId || viewChild;
          if (effectiveChildId && childMatch) {
            const childConversationId = getChildConversationId(effectiveChildId);
            const childEvent = createReducerEventForChildSession(event, payload, effectiveChildId);
            const reducedChildState = reduceChatEvent(nextState, childEvent);
            nextState = {
              ...(reducedChildState as ChatStoreState),
              conversationIdsBySession: nextState.conversationIdsBySession,
              childSessionMessages: {
                ...nextState.childSessionMessages,
                [effectiveChildId]: selectOrderedMessagesFromState(reducedChildState, childConversationId),
              },
            };
          }
          return {
            ...nextState,
            taskActivities,
            connectionStatus: 'open' as const,
          };
        }

        if (event.type === 'task_completed') {
          const completedNodeId = getStringValue((event.payload as Record<string, unknown>)?.node_id);
          if (completedNodeId === 'system.compactor') {
            _compactedSessionIds.add(event.session_id);
            shouldRefreshContextUsage = true;
          }
        }

        const reducedState = reduceChatEvent(routedState, reducerEvent);
        const conversations = syncConversationsAfterEvent(state.conversations, reducedState, reducerEvent, conversationId);
        const { generatingBySession, activeTaskBySession } = updateGeneratingByEvent({ ...state, conversations }, reducerEvent, conversationId);
        const isGenerating = isConversationGenerating(
          conversations,
          state.activeConversationId,
          generatingBySession,
          state.isGenerating,
          state.viewingChildSessionId,
        );

        if (isTerminalTaskEvent(event)) {
          shouldReconcileTerminalTask = true;
        }

        let childSessionMessages = state.childSessionMessages;
        const viewingChild = state.viewingChildSessionId;
        if (viewingChild && conversationId === getChildConversationId(viewingChild)) {
          childSessionMessages = {
            ...childSessionMessages,
            [viewingChild]: selectOrderedMessagesFromState(reducedState, conversationId),
          };
        }

        return {
          ...reducedState,
          conversations,
          generatingBySession,
          activeTaskBySession,
          childNodes,
          childSessionMessages,
          taskActivities,
          connectionStatus: 'open',
          isGenerating,
        };
      });

      if (event.type === 'session_approval_rules_changed') {
        useSessionApprovalStore.getState().refresh();
      }

      if (shouldReconcileTerminalTask) {
        set((state) => ({ conversations: sortConversationsByRecency(state.conversations) }));
        const activeAfterTerminal = getActiveConversation(get());
        if (activeAfterTerminal?.sessionId) {
          void loadContextUsageIntoStore(activeAfterTerminal.sessionId, set, get);
        }
      }

      if (shouldRefreshContextUsage) {
        void loadContextUsageIntoStore(event.session_id, set, get);
      }
    },
    () => {
      const wasReconnecting = get().connectionStatus === 'reconnecting';
      set({ connectionStatus: 'open' });
      if (wasReconnecting) {
        useSessionApprovalStore.getState().refresh();
        const state = get();
        const active = state.conversations.find((conversation) => conversation.id === state.activeConversationId);
        if (active?.sessionId) {
          void (async () => {
            // [AutoC 2026-06-18] Why: reconnect recovery previously fetched pending
            // approvals while history hydration was still in flight. Synthetic
            // approval_requested events could then render before their tool cards.
            // How: rebuild the active session history first, restore running flags
            // without launching another history reload, then replay pending approvals.
            // Purpose: WebSocket resubscribe presents approval prompts on the correct
            // recovered message/tool card.
            await loadSessionHistoryIntoStore(active.id, active.sessionId, set, get);
            await restoreGeneratingStateFromBackend(active.id, active.sessionId, set, get, { reloadHistory: false });
            await replayPendingApprovals(active.sessionId, set, get);
          })();
        }
      }
    },
    () => {
      set({ connectionStatus: 'reconnecting' });
      reconnectTimer = setTimeout(() => startEventPump(set, get), 2000);
    },
  );
}
