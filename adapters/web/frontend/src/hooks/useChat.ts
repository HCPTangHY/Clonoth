// [2026-05-31] Selector hook for the reducer-backed chat store.
// Why: components should not read the normalized reducer tables directly. How: derive
// the active conversation and ordered WsMessage list from chatStore with selectors.
// Purpose: later UI migration can swap useChat to this hook without changing store internals.
// [2026-05-31] Step 3 splits metadata and message subscriptions. Why: selectMessages
// creates a fresh array, which makes React's external-store snapshot unstable if it is
// nested inside the main object selector. How: subscribe to messages separately with
// useShallow so equal message arrays reuse their previous reference. Purpose: prevent
// infinite rerender loops when App mounts the canonical hook.
import { useShallow } from 'zustand/react/shallow';

import { selectMessages } from '../store/eventSelectors';
import { useChatStore } from '../store/chatStore';
import type { WsMessage } from '../types/message';

const EMPTY_MESSAGES: WsMessage[] = [];

export const useChat = () => {
  const base = useChatStore(
    useShallow((state) => {
      const activeConversation = state.activeConversationId
        ? state.conversations.find((conversation) => conversation.id === state.activeConversationId) ?? null
        : null;

      return {
        conversations: state.conversations,
        activeConversationId: state.activeConversationId,
        activeConversation,
        isGenerating: state.isGenerating,
        connectionStatus: state.connectionStatus,
        selectConversation: state.selectConversation,
        createConversation: state.createConversation,
        deleteConversation: state.deleteConversation,
        renameConversation: state.renameConversation,
        sendMessage: state.sendMessage,
        cancelCurrentTask: state.cancelCurrentTask,
        resetState: state.resetState,
        viewChildSession: state.viewChildSession,
        exitChildSession: state.exitChildSession,
        viewingChildSessionId: state.viewingChildSessionId,
        loadStartup: state.loadStartup,
      };
    }),
  );
  const messages = useChatStore(
    useShallow((state) => {
      // [2026-06-03] Why: Phase 3 can replace the parent timeline with a child
      // session's independent stream. How: prefer the cached child messages while a
      // child is being viewed, otherwise keep the existing active parent selector.
      // Purpose: MessageList does not need to know whether it renders parent or child.
      if (state.viewingChildSessionId) {
        return state.childSessionMessages[state.viewingChildSessionId] || EMPTY_MESSAGES;
      }
      return state.activeConversationId ? selectMessages(state, state.activeConversationId) : EMPTY_MESSAGES;
    }),
  );

  return { ...base, messages };
};
