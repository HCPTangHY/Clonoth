// [AutoC 2026-08-23] Host action registry for plugin slot scripts.
// Why: the slot api used to be a curated whitelist — every migrated feature
// (reroll, then retryMessage) required editing slotApi and rebuilding the
// host, which made plugin capabilities host-granted one by one. How: host
// actions are registered once in this module by name, and plugins invoke them
// through a generic call surface (api.call('retryMessage', ...)). The set of
// actions is core-owned — a plugin cannot invent host behavior — but adding a
// core feature registers its actions here once, not per plugin. Purpose: the
// frontend action surface follows the same shape as the backend contributions
// container (mount by name, consume generically), ending per-plugin whitelist
// edits.
import { useChatStore } from './chatStore';

export type HostAction = (...args: unknown[]) => unknown;

const actions = new Map<string, HostAction>();

/**
 * Register one host action. Returns a disposer (identity-checked) so a future
 * hot-unload of the registering feature can retract it without removing a
 * same-name replacement.
 */
export function registerHostAction(name: string, fn: HostAction): () => void {
  const prev = actions.get(name);
  actions.set(name, fn);
  let active = true;
  return () => {
    if (!active || actions.get(name) !== fn) return;
    active = false;
    if (prev) actions.set(name, prev);
    else actions.delete(name);
  };
}

/** Invoke one registered host action. Unknown names warn and resolve null. */
export async function callHostAction(name: string, ...args: unknown[]): Promise<unknown> {
  const fn = actions.get(name);
  if (!fn) {
    console.warn(`[host-action] unknown action "${name}"; registered: ${listHostActions().join(', ')}`);
    return null;
  }
  return await fn(...args);
}

export function listHostActions(): string[] {
  return Array.from(actions.keys()).sort();
}

// ── built-in chat actions ───────────────────────────────────────────────────
// Registered at module load; the registry is module-level so registration
// order is irrelevant to consumers.

registerHostAction('send', async (text) => {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return;
  await useChatStore.getState().sendMessage(trimmed);
});

registerHostAction('retryMessage', async (messageId, newText) => {
  const id = String(messageId ?? '');
  if (!id) return;
  await useChatStore.getState().retryMessage(id, newText == null ? undefined : String(newText));
});

registerHostAction('insertComposerText', async (text) => {
  // [AutoC 2026-08-24] Insert text at the composer textarea's cursor (replaces the
  // current selection). Why: plugin scripts cannot cleanly write a React-controlled
  // textarea — programmatic value assignment needs the prototype-chain setter trick
  // plus a manual input event, which would be duplicated by every completer plugin.
  // How: the host owns the composer, so it performs the write once here: locate the
  // anchored textarea, splice the value across the selection range, and dispatch a
  // bubbling input event so React's onChange picks it up. Purpose: completion
  // selection (@file, /command, future triggers) writes back through one host action.
  const insertText = String(text ?? '');
  if (!insertText) return;
  const ta = document.querySelector<HTMLTextAreaElement>('[data-composer-textarea]');
  if (!ta) return;
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? start;
  const nextValue = ta.value.slice(0, start) + insertText + ta.value.slice(end);
  const valueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value',
  )?.set;
  valueSetter?.call(ta, nextValue);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  const cursor = start + insertText.length;
  requestAnimationFrame(() => {
    ta.setSelectionRange(cursor, cursor);
    ta.focus();
  });
});

registerHostAction('reroll', async () => {
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
});
