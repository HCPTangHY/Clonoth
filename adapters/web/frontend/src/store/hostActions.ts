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
import {
  clearSessionProviderOverride,
  getSessionProviderOverride,
  updateSessionProviderOverride,
} from '../api/supervisorClient';
import { useChatStore } from './chatStore';
import { dispatchPluginEvent } from './pluginRuntime';
import { useViewStore } from './viewStore';
import { useSettingsStore } from './settingsStore';

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

// [AutoC 2026-08-25] Generic panel-open action with opaque intent.
// Why: annotators and slot plugins need to open overlay panels and hand them a
// payload without the host parsing it. How: set the overlay with an optional
// intent; PluginPanel forwards the intent to the panel page on load via
// postMessage. Opening the overlay also forces the right rail open — an intent
// that navigates inside a hidden panel would be invisible otherwise. Purpose:
// the host owns panel routing, plugins own the payload.
registerHostAction('setRightPanelOpen', open => {
  if (typeof open !== 'boolean') return false;
  useSettingsStore.getState().setRightPanelOpen(open);
  return true;
});

registerHostAction('openPanel', (panelId, intent) => {
  const id = String(panelId ?? '');
  if (!id) return;
  useViewStore.getState().setPanelOverlay('right', id, intent);
  useSettingsStore.getState().setRightPanelOpen(true);
});

// Broadcast session/pending provider_override mutations so slot contributions stay in sync
useSettingsStore.subscribe((state, prev) => {
  if (
    state.sessionProviderOverride !== prev.sessionProviderOverride ||
    state.pendingProviderOverride !== prev.pendingProviderOverride
  ) {
    dispatchPluginEvent('session_provider_override_changed', {
      sessionProviderOverride: state.sessionProviderOverride,
      pendingProviderOverride: state.pendingProviderOverride,
    });
  }
});

registerHostAction('getSessionProviderOverride', async (sessionId) => {
  const sid = String(sessionId ?? '').trim();
  const settings = useSettingsStore.getState();
  if (!sid) {
    settings.setSessionProviderOverride(settings.pendingProviderOverride);
    return settings.pendingProviderOverride;
  }
  if (!settings.adminToken) return settings.sessionProviderOverride;
  try {
    const override = await getSessionProviderOverride(sid, settings.adminToken);
    useSettingsStore.getState().setSessionProviderOverride(override);
    return override;
  } catch {
    useSettingsStore.getState().setSessionProviderOverride(null);
    return null;
  }
});

registerHostAction('patchSessionProviderOverride', async (sessionId, patch) => {
  if (!patch || typeof patch !== 'object') return null;
  const sid = String(sessionId ?? '').trim();
  const settings = useSettingsStore.getState();
  const base = sid ? settings.sessionProviderOverride : settings.pendingProviderOverride;
  const next: Record<string, unknown> = { ...(base || {}) };

  for (const [key, val] of Object.entries(patch as Record<string, unknown>)) {
    if (val === null || val === undefined || val === '') {
      delete next[key];
      if (key === 'reasoning_effort' && next.provider_options && typeof next.provider_options === 'object') {
        const po = { ...(next.provider_options as Record<string, unknown>) };
        delete po.reasoning_effort;
        if (Object.keys(po).length > 0) next.provider_options = po;
        else delete next.provider_options;
      }
    } else {
      next[key] = val;
    }
  }

  if (!sid) {
    const normalized = Object.keys(next).length > 0 ? next : null;
    settings.setPendingProviderOverride(normalized);
    settings.setSessionProviderOverride(normalized);
    return normalized;
  }

  if (!settings.adminToken) throw new Error('未登录');
  const saved = Object.keys(next).length === 0
    ? await clearSessionProviderOverride(sid, settings.adminToken)
    : await updateSessionProviderOverride(sid, settings.adminToken, next);
  useSettingsStore.getState().setSessionProviderOverride(saved);
  return saved;
});
