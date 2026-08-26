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
  listSessions,
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

  const serverSessions = await listSessions('web', 50);
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
    await replayPendingApprovals(activeConv.sessionId, set, get);
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
      contextUsage: state.contextUsageBySession[trimmed] || state.contextUsage,
    }));
    void loadChildSessionHistoryIntoStore(trimmed, set, trimmedTaskId || undefined);
    void loadContextUsageIntoStore(trimmed, set, get);
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
