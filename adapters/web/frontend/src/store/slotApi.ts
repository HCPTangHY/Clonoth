// [AutoC 2026-08-22] Host action surface for plugin slot scripts.
// Why: slot contributions need to trigger host behaviors (reroll, send) that only
// the web app can perform — retry depends on the message-id credential and JSONL
// fallback logic living in chatStore, and blob-imported plugin modules cannot
// reach bundled stores. How: a stable singleton resolving actions from
// useChatStore.getState() at call time, passed to every slot module as ctx.api.
// Purpose: plugins act on the host through an explicit allowlist instead of
// reaching into internals.
import { useChatStore } from './chatStore';

export interface PluginSlotApi {
  /** Retry from the last user message of the active conversation (quick reroll). */
  reroll: () => Promise<void>;
  /** Send a text message to the active conversation. */
  send: (text: string) => Promise<void>;
}

let cached: PluginSlotApi | null = null;

export function getPluginSlotApi(): PluginSlotApi {
  if (cached) return cached;
  cached = {
    reroll: async () => {
      const state = useChatStore.getState();
      // child-session view: which conversation to rewind is ambiguous — refuse.
      if (state.viewingChildSessionId) return;
      const cid = state.activeConversationId;
      if (!cid) return;
      const order = state.messageOrderByConversation[cid] || [];
      for (let i = order.length - 1; i >= 0; i--) {
        const message = state.messagesById[order[i]];
        if (message?.role === 'user') {
          await state.retryMessage(order[i]);
          return;
        }
      }
    },
    send: async (text) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      await useChatStore.getState().sendMessage(trimmed);
    },
  };
  return cached;
}
