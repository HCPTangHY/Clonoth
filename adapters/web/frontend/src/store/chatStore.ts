// ── Zustand Chat Store — Actions & Startup Loading ──────────────────────────
// [AutoC 2026-06-16] Refactored into four files.
// Why: chatStore.ts had grown to own realtime event pumping, session recovery,
// history loading, and store actions at the same time. How: WebSocket logic moved
// to eventPump.ts, reconnect/session recovery moved to sessionRecovery.ts, and
// history hydration helpers moved to historyLoader.ts. Purpose: this file now owns
// only startup session loading and the Zustand useChatStore action surface.
import { create } from 'zustand';

import {
  cancelActiveTasks,
  deleteSession,
  getSessionHistoryPage,
  listAllSessions,
  postInbound,
  preemptTask,
  retryInbound,
  uploadAttachment,
} from '../api/supervisorClient';
import type { Attachment } from '../types/message';
import { createInitialChatState } from './eventReducer';

// ── Re-exports from chatTypes (public API surface) ─────────────────────────
export type {
  ChatStoreState,
  ChildNodeState,
  ChildNodeStatus,
  ConnectionStatus,
  ContextUsageState,
  ConversationMeta,
  StoreSetter,
  StoreGetter,
  TaskActivity,
  TaskActivityPhase,
} from './chatTypes';
import type { ChatStoreState, StoreSetter, StoreGetter } from './chatTypes';
import {
  createConversationMeta,
  createStoreBase,
  getActiveConversation,
  getChildConversationId,
  getConversationIdFromHash,
  getInitialTitleFromClientPrefs,
  getStringValue,
  isEntryBranchSessionId,
  isRecord,
  loadLastActiveConversationId,
  loadTitleCache,
  normalizeContextUsage,
  normalizeConversationKey,
  saveLastActiveConversationId,
  saveTitleCache,
  selectOrderedMessagesFromState,
  setHashForConversation,
  sortConversationsByRecency,
  titleFromSession,
  upsertConversationMeta,
} from './chatTypes';
import {
  isConversationGenerating,
  selectChildNodesFromState,
  selectHasActiveChildNodesFromState,
} from './eventRouting';
import { hydrateStructuredHistory, removeConversationMessages } from './historyHydration';
import { resetEventPumpState, startEventPump, stopEventPump } from './eventPump';
import {
  loadContextUsageIntoStore,
  replayPendingApprovals,
  restoreGeneratingStateFromBackend,
} from './sessionRecovery';
import {
  HISTORY_PAGE_SIZE,
  captureStoreGeneration,
  invalidateStoreGeneration,
  isCurrentStoreGeneration,
  loadChildSessionHistoryIntoStore,
  loadSessionHistoryIntoStore,
} from './historyLoader';

// ── Module-level State ─────────────────────────────────────────────────────

let startupLoaded = false;

type UploadedMessageAttachment = {
  name: string;
  size: number;
  type?: string;
  path?: string;
  url?: string;
  mime_type?: string;
};

async function uploadMessageAttachments(
  attachments: readonly Attachment[] | undefined,
  conversationKey: string,
): Promise<UploadedMessageAttachment[] | undefined> {
  if (!attachments || attachments.length === 0) return undefined;
  return Promise.all(
    attachments.map(async (attachment: Attachment) => {
      if (attachment.file) {
        const result = await uploadAttachment(attachment.file, conversationKey);
        return {
          name: result.name,
          size: result.size,
          type: result.type,
          path: result.path,
          mime_type: result.mime_type,
        };
      }
      return {
        name: attachment.name || attachment.path?.split('/').pop() || '附件',
        size: attachment.size ?? 0,
        type: attachment.type,
        path: attachment.path,
        url: attachment.url?.startsWith('blob:') ? undefined : attachment.url,
        mime_type: attachment.mime_type,
      };
    }),
  );
}

async function loadStartupSessions(set: StoreSetter, get: StoreGetter) {
  if (startupLoaded) return;
  startupLoaded = true;
  // [2026-10-02] 捕获 store 世代：resetState（登出）会让这一轮的所有异步结果作废。
  // Why: 登出与启动列表/首个会话历史会并发；旧响应若写回就会把上一个账号的会话
  // 列表/消息填进已清空的 store，并重新拉一条事件泵（登出后仍有 socket 与轮询）。
  // How: 每次 await 之后比对世代，不匹配就整轮退出（不写 store、不启泵）。
  // Purpose: 登出即彻底断开，重新登录只由新的一轮 startup 建立连接。
  const storeGeneration = captureStoreGeneration();

  // [2026-09-02] Why: a fixed 50-row page could hide older web conversations once
  // the registry grows. How: paginate channel=web until a short page ends the scan.
  // Purpose: the sidebar always reflects every web session, not just the newest 50.
  let serverSessions: Awaited<ReturnType<typeof listAllSessions>> = [];
  try {
    serverSessions = await listAllSessions('web');
  } catch {
    // 请求飞行期间登出：旧世代不得重启事件泵。
    if (!isCurrentStoreGeneration(storeGeneration)) return;
    // [2026-10-02] 启动列表失败（后端刚重启/网络抖动）不能把启动流程锁死。
    // Why: startupLoaded 在函数开头就被置 true，这里直接 throw 会让本次挂载
    // 既没有会话列表、也没有事件流，且后续 loadStartup 全部被短路。How: 复位
    // 启动标记并启动事件泵（WS 自带重连与历史恢复）。Purpose: 重登/重载后
    // 事件流一定建立，列表与历史可被下一次挂载或重连恢复补上。
    startupLoaded = false;
    startEventPump(set, get);
    return;
  }
  // [2026-10-02] 登出后旧响应必须整体丢弃：不写回旧会话列表、不重启事件泵。
  if (!isCurrentStoreGeneration(storeGeneration)) return;
  const userSessions = (serverSessions || []).filter((session) =>
    !isEntryBranchSessionId(session.session_id)
    && !session.parent_session_id
  );
  if (userSessions.length === 0) {
    startEventPump(set, get);
    return;
  }

  const titleCache = loadTitleCache();
  const seenConversationIds = new Set<string>();
  const conversations = userSessions.flatMap((session) => {
    const conversationId = normalizeConversationKey(session.conversation_key) || session.session_id;
    if (seenConversationIds.has(conversationId)) return [];
    seenConversationIds.add(conversationId);
    return [{
      id: conversationId,
      sessionId: session.session_id,
      title: titleCache[conversationId] || titleFromSession(session.conversation_key, session.session_id),
      workspaceName: session.workspace_name || undefined,
      updatedAt: session.updated_at || session.created_at || new Date().toISOString(),
    }];
  });

  const restoredCache = loadTitleCache();
  let cacheUpdated = false;
  for (const conv of conversations) {
    if (conv.title && conv.title !== '新对话' && conv.title !== 'New conversation' && restoredCache[conv.id] !== conv.title) {
      restoredCache[conv.id] = conv.title;
      cacheUpdated = true;
    }
  }
  if (cacheUpdated) saveTitleCache(restoredCache);

  const sortedConversations = sortConversationsByRecency(conversations);
  set((state) => ({
    conversations: sortedConversations,
    activeConversationId: state.activeConversationId || null,
    conversationIdsBySession: sortedConversations.reduce<Record<string, string>>((acc, conversation) => {
      if (conversation.sessionId) acc[conversation.sessionId] = conversation.id;
      return acc;
    }, { ...state.conversationIdsBySession }),
  }));

  startEventPump(set, get);

  const hashConvId = getConversationIdFromHash();
  const persistedActiveId = loadLastActiveConversationId();
  const currentActive = hashConvId || persistedActiveId || get().activeConversationId;
  const activeConv = (currentActive
    ? sortedConversations.find((c) => c.id === currentActive)
    : null) || sortedConversations[0] || null;
  if (activeConv) {
    set({ activeConversationId: activeConv.id });
    saveLastActiveConversationId(activeConv.id);
  }
  if (activeConv?.sessionId) {
    void loadContextUsageIntoStore(activeConv.sessionId, set, get);
    await loadSessionHistoryIntoStore(activeConv.id, activeConv.sessionId, set, get);
    // 历史加载期间登出：后续的两个恢复请求属于旧账号，不再发出。
    if (!isCurrentStoreGeneration(storeGeneration)) return;
    await replayPendingApprovals(activeConv.sessionId, set, get);
    if (!isCurrentStoreGeneration(storeGeneration)) return;
    void restoreGeneratingStateFromBackend(activeConv.id, activeConv.sessionId, set, get, { reloadHistory: false });
  }
}

// ── Zustand Store Definition ───────────────────────────────────────────────

export const useChatStore = create<ChatStoreState>((set, get) => ({
  ...createInitialChatState(),
  ...createStoreBase(),

  resetState: () => {
    // [AutoC 2026-06-16] Reset realtime side effects through eventPump.
    // Why: reconnect timers, WebSocket state, and auto-approval state now live outside
    // chatStore.ts. How: stop the socket pump, reset pump-local state, then reset
    // startupLoaded here. Purpose: resetState preserves previous behavior without
    // reaching into module internals.
    stopEventPump();
    resetEventPumpState();
    // [2026-10-02] 递增 store 世代：作废 reset 之前发出的 startup/parent/child
    // 历史响应（含子会话请求序号表）。Why: 登出清空缓存后，在飞的旧响应仍会
    // 写回旧会话/旧消息，child 历史还会重建 childSessionMessages。How: 世代
    // 在 historyLoader 里统一比较（parent 与 child 两条路径共用）。Purpose:
    // 登出后 store 只接受重新登录后的响应。
    invalidateStoreGeneration();
    startupLoaded = false;
    set({
      ...createInitialChatState(),
      ...createStoreBase(),
    });
  },

  selectConversation: (id) => {
    const state = get();
    const target = state.conversations.find((conversation) => conversation.id === id);
    const targetIsGenerating = target?.sessionId ? Boolean(state.generatingBySession[target.sessionId]) : false;
    setHashForConversation(id);
    saveLastActiveConversationId(id);
    set({
      activeConversationId: id,
      viewingChildSessionId: null,
      viewingChildConversationKey: null,
      contextUsage: target?.sessionId ? state.contextUsageBySession[target.sessionId] || null : null,
      isGenerating: targetIsGenerating,
      historyFullyLoaded: false,
      historyLoadedCount: 0,
      historyTotal: 0,
      isLoadingMoreHistory: false,
    });
    if (target?.sessionId) {
      const isConnected = state.connectionStatus === 'open';
      const hasExistingMessages = Boolean(state.messageOrderByConversation[id]?.length);
      if (targetIsGenerating && isConnected && hasExistingMessages) {
        void loadContextUsageIntoStore(target.sessionId, set, get);
        void replayPendingApprovals(target.sessionId, set, get);
        return;
      }
      void (async () => {
        // [AutoC 2026-06-18] Why: pending approval replay creates synthetic
        // approval_requested events that should attach to already rebuilt tool cards.
        // How: wait for session history hydration before fetching and replaying any
        // outstanding approvals. Purpose: switching conversations does not create
        // orphan approval cards before the tool history exists.
        await loadSessionHistoryIntoStore(target.id, target.sessionId, set, get);
        await replayPendingApprovals(target.sessionId, set, get);
      })();
      void loadContextUsageIntoStore(target.sessionId, set, get);
    }
  },

  selectChildNodes: (conversationId) => {
    return selectChildNodesFromState(get(), conversationId);
  },

  selectHasActiveChildNodes: (conversationId) => {
    return selectHasActiveChildNodesFromState(get(), conversationId);
  },

  viewChildSession: (sessionId, taskId, conversationKey) => {
    const trimmed = sessionId.trim();
    if (!trimmed) return;
    const trimmedTaskId = taskId?.trim() || null;
    const trimmedConversationKey = conversationKey?.trim() || null;
    set((state) => ({
      viewingChildSessionId: trimmed,
      viewingChildConversationKey: trimmedConversationKey,
      viewingChildTaskId: trimmedTaskId,
      childSessionMessages: {
        ...state.childSessionMessages,
        [trimmed]: [],
      },
      // [2026-09-13] 进入子会话视图时重置空态标记，历史加载完成后重新写入。
      childSessionEmpty: {
        ...state.childSessionEmpty,
        [trimmed]: false,
      },
      // [2026-10-02] 同时复位失败标记：viewChildSession 既是首次进入，也是重试入口，
      // 重试期间视图必须回到“加载中”，不能停在上一轮的失败态。
      childSessionLoadFailed: {
        ...state.childSessionLoadFailed,
        [trimmed]: false,
      },
      contextUsage: state.contextUsageBySession[trimmed] || state.contextUsage,
    }));
    void loadChildSessionHistoryIntoStore(trimmed, set, trimmedTaskId || undefined);
    void loadContextUsageIntoStore(trimmed, set, get);
    // [2026-09-05] Why: entering a running child task from the active-tasks panel
    // showed an idle composer. The child session's generating flags are only set
    // by events flowing through the pump while viewing it; entering after the
    // task started receives nothing, so generatingBySession[child] stayed false
    // and submission fell back to plain inbound instead of preempt. How: query
    // the backend's running-tasks list on entry, same recovery used after a
    // WebSocket reconnect. Purpose: the composer immediately reflects the
    // child's real running state (stop / preempt) without waiting for events.
    void restoreGeneratingStateFromBackend(
      getChildConversationId(trimmed),
      trimmed,
      set,
      get,
      { reloadHistory: false },
    );
  },

  exitChildSession: () => {
    const active = getActiveConversation(get());
    set({
      viewingChildSessionId: null,
      viewingChildConversationKey: null,
      viewingChildTaskId: null,
      contextUsage: active?.sessionId ? get().contextUsageBySession[active.sessionId] || null : null,
    });
    if (active?.sessionId) {
      void loadContextUsageIntoStore(active.sessionId, set, get);
    }
  },

  createConversation: () => {
    const current = get().activeConversationId;
    if (current) {
      setHashForConversation(null);
      saveLastActiveConversationId(null);
      set({
        activeConversationId: null,
        viewingChildSessionId: null,
        viewingChildConversationKey: null,
        contextUsage: null,
      });
      return '';
    }
    const meta = createConversationMeta();
    setHashForConversation(meta.id);
    saveLastActiveConversationId(meta.id);
    set((state) => ({
      conversations: upsertConversationMeta(state.conversations, meta),
      activeConversationId: meta.id,
      viewingChildSessionId: null,
      viewingChildConversationKey: null,
    }));
    return meta.id;
  },

  deleteConversation: (id) => {
    const conversation = get().conversations.find((item) => item.id === id);
    if (conversation?.sessionId) {
      void deleteSession(conversation.sessionId).catch(() => undefined);
    }

    set((state) => {
      const conversations = state.conversations.filter((item) => item.id !== id);
      const activeConversationId = state.activeConversationId === id ? conversations[0]?.id || null : state.activeConversationId;
      if (state.activeConversationId === id) {
        setHashForConversation(activeConversationId);
        saveLastActiveConversationId(activeConversationId);
      }
      const nextChatState = removeConversationMessages(state, id);
      const conversationIdsBySession = Object.fromEntries(
        Object.entries(nextChatState.conversationIdsBySession).filter(([, conversationId]) => conversationId !== id),
      );
      const childNodes = Object.fromEntries(
        Object.entries(state.childNodes).filter(([, child]) => child.parentConversationId !== id),
      );

      const generatingBySession = conversation?.sessionId
        ? { ...state.generatingBySession, [conversation.sessionId]: false }
        : state.generatingBySession;

      return {
        ...nextChatState,
        conversationIdsBySession,
        conversations,
        activeConversationId,
        viewingChildSessionId: state.viewingChildSessionId && (childNodes[state.viewingChildSessionId] || state.viewingChildConversationKey)
          ? state.viewingChildSessionId
          : null,
        viewingChildConversationKey: state.viewingChildSessionId && (childNodes[state.viewingChildSessionId] || state.viewingChildConversationKey)
          ? state.viewingChildConversationKey
          : null,
        generatingBySession,
        childNodes,
        contextUsage: (() => {
          if (activeConversationId === state.activeConversationId) return state.contextUsage;
          const nextConversation = conversations.find((item) => item.id === activeConversationId);
          return nextConversation?.sessionId ? state.contextUsageBySession[nextConversation.sessionId] || null : null;
        })(),
        isGenerating: isConversationGenerating(conversations, activeConversationId, generatingBySession, false, state.viewingChildSessionId),
      };
    });
  },

  renameConversation: (id, newTitle) => {
    const trimmed = newTitle.trim();
    if (!trimmed) return;
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, title: trimmed, updatedAt: new Date().toISOString() } : c,
      ),
    }));
    const cache = loadTitleCache();
    cache[id] = trimmed;
    saveTitleCache(cache);
  },

  sendMessage: async (text, attachments, entryNodeId, providerOverride) => {
    const trimmed = text.trim();
    if (!trimmed && (!attachments || attachments.length === 0)) return;

    const state = get();
    const temporarySessionId = state.viewingChildSessionId?.trim() || '';
    const temporaryConversationKey = state.viewingChildConversationKey?.trim() || temporarySessionId;

    if (temporarySessionId && temporaryConversationKey) {
      const conversationId = getChildConversationId(temporarySessionId);
      set((current) => ({
        isGenerating: true,
        connectionStatus: current.connectionStatus === 'open' ? 'open' : 'connecting',
        generatingBySession: {
          ...current.generatingBySession,
          [temporarySessionId]: true,
        },
        conversationIdsBySession: {
          ...current.conversationIdsBySession,
          [temporarySessionId]: conversationId,
        },
      }));

      let sessionId = temporarySessionId;
      try {
        const uploadedAttachments = await uploadMessageAttachments(attachments, temporaryConversationKey);
        const result = await postInbound({
          conversation_key: temporaryConversationKey,
          text: trimmed,
          attachments: uploadedAttachments,
          use_context: true,
          entry_node_id: entryNodeId,
          provider_override: providerOverride ?? undefined,
          session_id: temporarySessionId,
        });
        sessionId = result.session_id || temporarySessionId;
        const inboundSeq = result.inbound_seq;
        if (inboundSeq != null) {
          const targetConversationId = getChildConversationId(sessionId);
          const optimisticId = `message:${targetConversationId}:user:inbound:${inboundSeq}`;
          const now = new Date().toISOString();
          set((current) => {
            if (current.messagesById[optimisticId]) return current;
            const nextState = {
              ...current,
              viewingChildSessionId: sessionId,
              viewingChildConversationKey: temporaryConversationKey,
              messagesById: {
                ...current.messagesById,
                [optimisticId]: {
                  id: optimisticId,
                  conversationId: targetConversationId,
                  sessionId,
                  role: 'user' as const,
                  status: 'completed' as const,
                  createdAt: now,
                  updatedAt: now,
                  source: { inboundSeq },
                  blocks: [{
                    id: `${optimisticId}|block:text:optimistic`,
                    kind: 'text' as const,
                    text: trimmed,
                    delivery: 'final' as const,
                    streaming: false,
                    createdAt: now,
                    updatedAt: now,
                    eventIds: [],
                  }],
                  attachments: (uploadedAttachments || []).map((attachment) => ({
                    name: attachment.name,
                    size: attachment.size,
                    type: attachment.type,
                    path: attachment.path,
                    url: attachment.url,
                    mime_type: attachment.mime_type,
                  })),
                  eventIds: [],
                },
              },
              messageOrderByConversation: {
                ...current.messageOrderByConversation,
                [targetConversationId]: [
                  ...(current.messageOrderByConversation[targetConversationId] || []),
                  optimisticId,
                ],
              },
              userMessageByInboundSeq: {
                ...current.userMessageByInboundSeq,
                [String(inboundSeq)]: optimisticId,
              },
              conversationIdsBySession: {
                ...current.conversationIdsBySession,
                [sessionId]: targetConversationId,
              },
              generatingBySession: {
                ...current.generatingBySession,
                [sessionId]: true,
              },
              lastSeqBySession: {
                ...current.lastSeqBySession,
                [sessionId]: current.lastSeqBySession[sessionId] || 0,
              },
            };
            return {
              ...nextState,
              childSessionMessages: {
                ...current.childSessionMessages,
                [sessionId]: selectOrderedMessagesFromState(nextState, targetConversationId),
              },
            };
          });
        }
      } catch {
        set((current) => ({
          isGenerating: false,
          connectionStatus: current.connectionStatus === 'open' ? 'open' : 'closed',
          generatingBySession: {
            ...current.generatingBySession,
            [temporarySessionId]: false,
          },
        }));
        return;
      }

      startEventPump(set, get);
      return;
    }

    const conversationId = state.activeConversationId || state.createConversation();
    saveLastActiveConversationId(conversationId);
    const conversationKey = `web:${conversationId}`;
    const existingConversation = get().conversations.find((conversation) => conversation.id === conversationId);

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId: existingConversation?.sessionId || '',
        title: (existingConversation?.title === '新对话' || existingConversation?.title === 'New conversation') && trimmed
          ? getInitialTitleFromClientPrefs(trimmed, existingConversation?.title)
          : existingConversation?.title,
        updatedAt: new Date().toISOString(),
      }),
      activeConversationId: conversationId,
      isGenerating: true,
      connectionStatus: current.connectionStatus === 'open' ? 'open' : 'connecting',
    }));

    let sessionId = '';
    try {
      const uploadedAttachments = await uploadMessageAttachments(attachments, conversationKey);

      const result = await postInbound({
        conversation_key: conversationKey,
        text: trimmed,
        attachments: uploadedAttachments,
        use_context: true,
        entry_node_id: entryNodeId,
        provider_override: providerOverride ?? undefined,
      });
      sessionId = result.session_id;
      const inboundSeq = result.inbound_seq;
      if (inboundSeq != null) {
        const optimisticId = `message:${conversationId}:user:inbound:${inboundSeq}`;
        const now = new Date().toISOString();
        set((current) => {
          if (current.messagesById[optimisticId]) return current;
          return {
            ...current,
            messagesById: {
              ...current.messagesById,
              [optimisticId]: {
                id: optimisticId,
                conversationId,
                sessionId,
                role: 'user' as const,
                status: 'completed' as const,
                createdAt: now,
                updatedAt: now,
                source: { inboundSeq },
                blocks: [{
                  id: `${optimisticId}|block:text:optimistic`,
                  kind: 'text' as const,
                  text: trimmed,
                  delivery: 'final' as const,
                  streaming: false,
                  createdAt: now,
                  updatedAt: now,
                  eventIds: [],
                }],
                attachments: (uploadedAttachments || []).map((attachment) => ({
                  name: attachment.name,
                  size: attachment.size,
                  type: attachment.type,
                  path: attachment.path,
                  url: attachment.url,
                  mime_type: attachment.mime_type,
                })),
                eventIds: [],
              },
            },
            messageOrderByConversation: {
              ...current.messageOrderByConversation,
              [conversationId]: [
                ...(current.messageOrderByConversation[conversationId] || []),
                optimisticId,
              ],
            },
            userMessageByInboundSeq: {
              ...current.userMessageByInboundSeq,
              [String(inboundSeq)]: optimisticId,
            },
          };
        });
      }
    } catch {
      set((current) => ({
        isGenerating: false,
        connectionStatus: current.connectionStatus === 'open' ? 'open' : 'closed',
      }));
      return;
    }

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
      conversationIdsBySession: {
        ...current.conversationIdsBySession,
        [sessionId]: conversationId,
      },
      lastSeqBySession: {
        ...current.lastSeqBySession,
        [sessionId]: current.lastSeqBySession[sessionId] || 0,
      },
      generatingBySession: {
        ...current.generatingBySession,
        [sessionId]: true,
      },
    }));

    startEventPump(set, get);
  },

  retryMessage: async (messageId: string, newText?: string) => {
    const state = get();
    const message = state.messagesById[messageId];
    if (!message || message.role !== 'user') return;
    const inboundSeq = message.source.inboundSeq;
    // [2026-08-20] history 消息 id 是 message:{cid}:history:{后端UUID}，拆出原始 id 作凭证；
    // 实时消息 id 是前端生成的，此时靠 inboundSeq。
    const rawMessageId = message.id.includes(':history:')
      ? (message.id.split(':history:').pop() || message.id)
      : message.id;
    if (!inboundSeq && !rawMessageId) return;

    // [2026-08-26] sessionId 优先取消息自身：子会话（临时会话）视图下
    // activeConversation 是父会话，用它会把重试发进父会话——父会话的
    // 分支合并查找能命中子会话消息，还会删掉全部分支 JSONL 再把子会话
    // 原文作为新 inbound 注入父会话，两边串台。消息属于哪个 session
    // 就在哪个 session 内重试。
    const sessionId = message.sessionId || getActiveConversation(state)?.sessionId || '';
    if (!sessionId) return;

    try {
      const result = await retryInbound(sessionId, { inboundSeq, messageId: rawMessageId }, newText);
      if (!result.ok) {
        // [2026-08-26] 失败不再静默：console.error 被 public/error-overlay.js 劫持，
        // 会以全局弹窗形式展示后端 detail（如子会话守卫的 400）。
        console.error(`[retry] 重试请求失败：${result.error || '未知错误'}`);
        return;
      }

      // Remove all messages from the retried message onward in the UI
      const conversationId = message.conversationId;
      const order = state.messageOrderByConversation[conversationId] || [];
      const retryIndex = order.indexOf(messageId);
      if (retryIndex >= 0) {
        const idsToRemove = new Set(order.slice(retryIndex));
        set((current) => {
          const messagesById = { ...current.messagesById };
          for (const id of idsToRemove) delete messagesById[id];
          const newOrder = (current.messageOrderByConversation[conversationId] || []).filter(
            (id) => !idsToRemove.has(id),
          );
          const toolExecutionsById = { ...current.toolExecutionsById };
          const toolExecutionOrder = current.toolExecutionOrder.filter((tid) => {
            const tool = current.toolExecutionsById[tid];
            if (tool && idsToRemove.has(tool.messageId)) {
              delete toolExecutionsById[tid];
              return false;
            }
            return true;
          });
          return {
            messagesById,
            messageOrderByConversation: { ...current.messageOrderByConversation, [conversationId]: newOrder },
            toolExecutionsById,
            toolExecutionOrder,
            isGenerating: true,
            generatingBySession: { ...current.generatingBySession, [sessionId]: true },
          };
        });
      }
    } catch (err) {
      // [2026-08-26] 空改捕获后转发：apiFetch 对非 2xx 抛错（含后端 detail），
      // 转发给 console.error 使全局错误弹窗可见。
      console.error(`[retry] 重试请求失败：${err instanceof Error ? err.message : String(err)}`);
    }
  },

  cancelCurrentTask: async () => {
    const current = get();
    const activeConversation = getActiveConversation(current);
    const sessionId = current.viewingChildSessionId || activeConversation?.sessionId || '';
    if (!sessionId) return;

    try {
      await cancelActiveTasks(sessionId);
    } catch {}

    set((state) => {
      const nextActiveTask = { ...state.activeTaskBySession };
      delete nextActiveTask[sessionId];
      return {
        isGenerating: false,
        generatingBySession: {
          ...state.generatingBySession,
          [sessionId]: false,
        },
        activeTaskBySession: nextActiveTask,
      };
    });
  },

  preemptCurrentTask: async (message: string, attachments?: Attachment[]) => {
    const state = get();
    const activeConversation = getActiveConversation(state);
    const sessionId = state.viewingChildSessionId || activeConversation?.sessionId || '';
    if (!sessionId) return;
    const taskId = state.activeTaskBySession[sessionId];
    if (!taskId) return;
    const conversationKey = state.viewingChildConversationKey || (state.activeConversationId ? `web:${state.activeConversationId}` : activeConversation?.id || sessionId);
    try {
      const uploadedAttachments = await uploadMessageAttachments(attachments, conversationKey);
      await preemptTask(taskId, message, uploadedAttachments);
    } catch {}
  },

  updateContextUsage: (data) => {
    const contextUsage = normalizeContextUsage(data);
    if (!contextUsage) return;

    const payload = isRecord(data) ? data : {};
    const sessionId = getStringValue(payload.session_id) || getStringValue(payload.sessionId);

    // [AutoC 2026-08-24] 会话级缓存命中率 EMA：从 payload.usage 读统一字段
    // cached_prompt_tokens（provider 层归一化），与 prompt_tokens 计算本轮
    // 命中率，再与既有值做指数移动平均（alpha=0.3）。上游未上报时保留旧值。
    const usageRaw = isRecord(payload.usage) ? payload.usage : null;
    const promptTokens = usageRaw && typeof usageRaw.prompt_tokens === 'number' ? usageRaw.prompt_tokens : 0;
    const cachedTokens = usageRaw && typeof usageRaw.cached_prompt_tokens === 'number' ? usageRaw.cached_prompt_tokens : 0;
    const instantRate = promptTokens > 0 && cachedTokens > 0 ? cachedTokens / promptTokens : null;

    set((state) => {
      const active = getActiveConversation(state);
      // EMA 更新：有本轮命中率时与既有值平滑，无则沿用
      const existing = sessionId ? state.contextUsageBySession[sessionId] : state.contextUsage;
      const prevRate = existing?.cacheHitRate ?? null;
      const nextRate = instantRate !== null
        ? (prevRate !== null ? prevRate * 0.7 + instantRate * 0.3 : instantRate)
        : prevRate;
      const merged = { ...contextUsage, cacheHitRate: nextRate };

      const nextBySession = sessionId
        ? { ...state.contextUsageBySession, [sessionId]: merged }
        : state.contextUsageBySession;
      const shouldDisplay = !sessionId || active?.sessionId === sessionId;
      return {
        contextUsageBySession: nextBySession,
        contextUsage: shouldDisplay ? merged : state.contextUsage,
      };
    });
  },

  loadStartup: () => {
    void loadStartupSessions(set, get);
  },

  loadMoreHistory: async () => {
    const state = get();
    if (state.historyFullyLoaded || state.isLoadingMoreHistory) return;
    const active = getActiveConversation(state);
    if (!active?.sessionId) return;
    const conversationId = state.activeConversationId;
    if (!conversationId) return;

    set({ isLoadingMoreHistory: true });
    try {
      const page = await getSessionHistoryPage(
        active.sessionId, HISTORY_PAGE_SIZE, state.historyLoadedCount,
      );
      if (page.messages.length === 0) {
        set({ historyFullyLoaded: true, isLoadingMoreHistory: false });
        return;
      }
      set((current) => {
        const tempState = hydrateStructuredHistory(
          { ...current, messageOrderByConversation: { ...current.messageOrderByConversation, [conversationId]: [] } },
          active.sessionId, conversationId, page.messages, false,
        );
        const olderOrder = tempState.messageOrderByConversation[conversationId] || [];
        const existingOrder = current.messageOrderByConversation[conversationId] || [];
        const existingSet = new Set(existingOrder);
        const prependOrder = olderOrder.filter((id) => !existingSet.has(id));
        return {
          ...current,
          messagesById: { ...current.messagesById, ...tempState.messagesById },
          messageOrderByConversation: {
            ...current.messageOrderByConversation,
            [conversationId]: [...prependOrder, ...existingOrder],
          },
          toolExecutionsById: { ...current.toolExecutionsById, ...tempState.toolExecutionsById },
          toolExecutionOrder: [
            ...tempState.toolExecutionOrder.filter((id) => !current.toolExecutionOrder.includes(id)),
            ...current.toolExecutionOrder,
          ],
          historyLoadedCount: current.historyLoadedCount + page.messages.length,
          historyFullyLoaded: !page.has_more,
          historyTotal: page.total,
          isLoadingMoreHistory: false,
        };
      });
    } catch {
      set({ isLoadingMoreHistory: false });
    }
  },
}));
