// [AutoC 2026-08-22] Host-side runtime services for plugin slot scripts.
// Why: slot modules run as blob-imported ES modules outside the React tree; the
// bare element contract gives them a place to render but nothing else. Plugins
// previously had no reactivity (data flowed only when the host re-rendered), no
// durable state (closures died with every remount), and no way to reach backend
// capabilities beyond the api whitelist. How: one module-level event bus tapped
// by the WebSocket event pump, plus a slotId-keyed state map with explicit wipe
// on disappearance. Purpose: plugins observe and remember on their own, without
// the host re-rendering for them.
// Context contract note: ctx.state is mutable by the plugin and unobserved by
// the host — the plugin redraws its own DOM after mutating it.

export type PluginEventHandler = (payload: unknown) => void;

const eventListeners = new Map<string, Set<PluginEventHandler>>();

/**
 * Dispatch one supervisor event to plugin subscribers.
 * Called by the event pump for every incoming WebSocket event, before the
 * store reducer runs. Handler errors are isolated per handler.
 */
export function dispatchPluginEvent(type: string, payload: unknown): void {
  const set = eventListeners.get(type);
  if (!set || set.size === 0) return;
  for (const handler of Array.from(set)) {
    try {
      handler(payload);
    } catch (err) {
      console.error(`[plugin-event:${type}] handler failed`, err);
    }
  }
}

/** Subscribe to one event type; returns an unsubscribe function. */
export function subscribePluginEvent(type: string, handler: PluginEventHandler): () => void {
  let set = eventListeners.get(type);
  if (!set) {
    set = new Set();
    eventListeners.set(type, set);
  }
  set.add(handler);
  return () => {
    set?.delete(handler);
  };
}

export interface PluginEventEmitter {
  /** Subscribe to one supervisor event type. Returns an unsubscribe function. */
  on: (type: string, handler: (payload: unknown) => void) => () => void;
}

// ── plugin-private state ───────────────────────────────────────────────────
// Survives host remounts (view switches, conversation switches). Wiped only
// when the contribution disappears for good (plugin unloaded, scripts off).

import { usePluginsStore } from './pluginsStore';
import { useSettingsStore } from './settingsStore';

const pluginStates = new Map<string, Record<string, unknown>>();

/** Return the stable state object for one slot contribution. */
export function getPluginState(slotId: string): Record<string, unknown> {
  let state = pluginStates.get(slotId);
  if (!state) {
    state = {};
    pluginStates.set(slotId, state);
  }
  return state;
}

/** Drop the state of a contribution that disappeared for good. */
export function wipePluginState(slotId: string): void {
  pluginStates.delete(slotId);
}

// ── settings editor bus ────────────────────────────────────────────────────
// [AutoC 2026-08-28] Plugin settings pages are same-origin iframes; their list
// view asks the host to open an item editor in the settings right rail via
// postMessage, and the host renders the same panel page with mode=editor
// there. The page-to-page data refresh after a save uses BroadcastChannel
// directly between the two iframes and never crosses the host.

interface PluginEditorMessage {
  source?: unknown;
  type?: unknown;
  panel?: unknown;
  params?: unknown;
}

let editorBusInstalled = false;

/** Install the single window listener for settings-editor open/close requests. */
export function installPluginEditorBus(): void {
  if (editorBusInstalled) return;
  editorBusInstalled = true;
  window.addEventListener('message', (event: MessageEvent) => {
    if (event.origin !== window.location.origin) return;
    const data = event.data as PluginEditorMessage | null;
    if (!data || data.source !== 'clonoth-plugin') return;
    if (typeof data.panel !== 'string' || !data.panel.startsWith('plugin:')) return;
    const store = usePluginsStore.getState();
    if (data.type === 'open-settings-editor') {
      const raw = data.params;
      const params: Record<string, string> = {};
      if (raw && typeof raw === 'object') {
        for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
          if (typeof v === 'string' || typeof v === 'number') params[k] = String(v);
        }
      }
      store.setPluginEditorTarget({ panelKey: data.panel, params });
      // [AutoC 2026-08-28] 右栏可见性是持久化用户偏好（AppLayout 只在
      // rightPanelOpen 为真时渲染右栏）。只设置编辑器目标而不展开右栏，
      // 用户收起过右栏时编辑器永远不可见——表现为“没有用上右边栏”。
      useSettingsStore.getState().setRightPanelOpen(true);
    } else if (data.type === 'close-settings-editor') {
      const current = store.pluginEditorTarget;
      if (current && current.panelKey === data.panel) store.setPluginEditorTarget(null);
    }
  });
}
