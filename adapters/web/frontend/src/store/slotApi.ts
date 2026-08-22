// [AutoC 2026-08-22] Host action surface for plugin slot scripts.
// Why: slot contributions need to trigger host behaviors that only the web app
// can perform — retry depends on the message-id credential and JSONL fallback
// logic living in chatStore, and blob-imported plugin modules cannot reach
// bundled stores. How: a stable singleton resolving actions from
// useChatStore.getState() at call time, passed to every slot module as ctx.api.
// Purpose: plugins act on the host through an explicit surface instead of
// reaching into internals.
// [AutoC 2026-08-22] The whitelist problem: every new capability used to require
// host code and a rebuild. `request` opens the full supervisor API (same
// authentication as the web UI), so curated actions below stay small.
import { useChatStore } from './chatStore';
import { pluginApiRequest } from '../api/supervisorClient';

export interface PluginSlotApi {
  /** Retry from the last user message of the active conversation (quick reroll). */
  reroll: () => Promise<void>;
  /** Send a text message to the active conversation. */
  send: (text: string) => Promise<void>;
  /**
   * Authenticated request to any supervisor /v1 endpoint.
   * Example: api.request('/sessions'). Non-ok responses reject.
   */
  request: (path: string, init?: RequestInit) => Promise<unknown>;
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
    request: async (path, init) => pluginApiRequest(path, init),
  };
  return cached;
}
