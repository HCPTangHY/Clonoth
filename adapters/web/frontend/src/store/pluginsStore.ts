// [AutoC 2026-08-22] Plugin client-contribution store.
// Why: plugins declare frontend contributions (panels/slots/styles) in backend
// PLUGIN_META; the web adapter is one consumer of that manifest and holds no
// plugin state of its own. How: fetch /v1/plugins on startup, normalize the
// three tiers into lookup structures, and group by owner so unload/disappear
// semantics mirror the backend DisposalLedger. Purpose: plugin UI appears and
// disappears with the backend plugin list, with no frontend build involved.
// [AutoC 2026-08-27] Unified panel registry: host built-in overlays and plugin
// panels register into one PanelContribution list. Same overlayId competes by
// priority (host built-ins at 0, plugins at 50 by default); replaces: is just
// "register at that overlayId with higher priority" — no override table.
import { create } from 'zustand';
import type { ComponentType } from 'react';

import {
  listPlugins,
  type PluginListItem,
} from '../api/supervisorClient';
import { refreshAnnotators } from './annotators';
import { subscribePluginEvent } from './pluginRuntime';

export interface ResolvedPanel {
  /** namespaced overlay id: plugin:{owner}:{panel-id} */
  key: string;
  owner: string;
  panelId: string;
  title: string;
  entry: string;
  icon: string;
}

/** Props every react-kind panel contribution receives from the overlay host. */
export interface HostPanelProps {
  sessionId: string;
  onClose: () => void;
}

/** One panel contribution in the unified registry (host or plugin). */
export interface PanelContribution {
  /** unique key: 'host:files' | 'plugin:{owner}:{panel-id}' */
  key: string;
  owner: string;
  /** overlay id this entry serves: built-in id ('files') or the standalone key */
  overlayId: string;
  title: string;
  /** same overlayId: higher wins; ties keep the earlier (host) entry */
  priority: number;
  /** listed as a standalone Header entry */
  standalone: boolean;
  kind: 'react' | 'iframe';
  /** iframe entry URL (kind='iframe') */
  entry?: string;
  /** host react component (kind='react') */
  Component?: ComponentType<HostPanelProps>;
}

export interface SlotContribution {
  slotId: string;
  owner: string;
  slot: string;
  script: string;
  priority: number;
  /**
   * 'replace' takes over the whole slot region: the highest-priority replace
   * contribution renders alone and every other contribution is not mounted.
   * Default 'append' keeps the old behavior of rendering alongside others.
   */
  mode: 'append' | 'replace';
}

interface PluginsState {
  loaded: boolean;
  plugins: PluginListItem[];
  /** host built-in overlays (React components), registered at module init */
  hostPanels: PanelContribution[];
  /** panels from the plugin manifest (iframe entries) */
  manifestPanels: PanelContribution[];
  /** standalone header entries, derived from host + manifest panels */
  standalonePanels: PanelContribution[];
  /** settings-view panels: full-page iframe tabs in the settings sidebar */
  settingsPanels: ResolvedPanel[];
  slotsBySlot: Record<string, SlotContribution[]>;
  stylesByOwner: Record<string, string>;
  clientScriptsEnabled: boolean;
  /**
   * [AutoC 2026-08-28] Active settings-editor target. A plugin settings page
   * (list-mode iframe) asks the host to edit an item via postMessage; the host
   * then renders the same panel page with mode=editor in the settings right
   * rail. null = no editor open (rail stays hidden for plugin tabs).
   */
  pluginEditorTarget: { panelKey: string; params: Record<string, string> } | null;
  refresh: () => Promise<void>;
  panelWinner: (overlayId: string) => PanelContribution | null;
  setClientScripts: (enabled: boolean) => void;
  setPluginEditorTarget: (target: { panelKey: string; params: Record<string, string> } | null) => void;
}

function deriveStandalone(host: PanelContribution[], manifest: PanelContribution[]): PanelContribution[] {
  return [...host, ...manifest].filter((p) => p.standalone);
}

const LS_CLIENT_SCRIPTS = 'clonoth_client_scripts';

function readClientScriptsPref(): boolean {
  try {
    return localStorage.getItem(LS_CLIENT_SCRIPTS) !== 'off';
  } catch {
    return true;
  }
}

export const usePluginsStore = create<PluginsState>((set, get) => ({
  loaded: false,
  plugins: [],
  hostPanels: [],
  manifestPanels: [],
  standalonePanels: [],
  settingsPanels: [],
  slotsBySlot: {},
  stylesByOwner: {},
  clientScriptsEnabled: readClientScriptsPref(),
  pluginEditorTarget: null,

  setPluginEditorTarget: (target) => set({ pluginEditorTarget: target }),

  refresh: async () => {
    let plugins: PluginListItem[] = [];
    try {
      plugins = await listPlugins();
    } catch {
      // backend unreachable or not yet authenticated; keep previous state
      return;
    }
    const scriptsOn = get().clientScriptsEnabled;
    const manifestPanels: PanelContribution[] = [];
    const settingsPanels: ResolvedPanel[] = [];
    const slotsBySlot: Record<string, SlotContribution[]> = {};
    const stylesByOwner: Record<string, string> = {};
    for (const plugin of plugins) {
      const web = plugin.web;
      if (!web || typeof web !== 'object') continue;
      const owner = plugin.name;
      for (const panel of web.panels || []) {
        if (!panel?.id || !panel.entry) continue;
        const key = `plugin:${owner}:${panel.id}`;
        const title = panel.title || panel.id;
        const priority = Number.isFinite(panel.priority) ? Number(panel.priority) : 50;
        // [AutoC 2026-08-27] Three destinations, one registry: a replaces-
        // declaration registers at the built-in overlayId with its own priority
        // and competes with the host entry; settings panels stay tab-shaped;
        // everything else is a standalone overlay under its own key.
        const replaces = typeof panel.replaces === 'string' ? panel.replaces.trim() : '';
        if (replaces && (!panel.slot || panel.slot === 'right')) {
          manifestPanels.push({ key, owner, overlayId: replaces, title, priority, standalone: false, kind: 'iframe', entry: panel.entry });
        } else if (panel.slot === 'settings') {
          settingsPanels.push({ key, owner, panelId: panel.id, title, entry: panel.entry, icon: typeof panel.icon === 'string' && panel.icon ? panel.icon : 'extension' });
        } else if (!panel.slot || panel.slot === 'right') {
          manifestPanels.push({ key, owner, overlayId: key, title, priority, standalone: true, kind: 'iframe', entry: panel.entry });
        }
      }
      if (scriptsOn) {
        for (const slot of web.slots || []) {
          if (!slot?.slot_id || !slot.slot || !slot.script) continue;
          const entry: SlotContribution = {
            slotId: slot.slot_id,
            owner,
            slot: slot.slot,
            script: slot.script,
            priority: Number.isFinite(slot.priority) ? Number(slot.priority) : 50,
            mode: slot.mode === 'replace' ? 'replace' : 'append',
          };
          (slotsBySlot[entry.slot] ||= []).push(entry);
        }
      }
      if (typeof web.styles === 'string' && web.styles.trim()) {
        stylesByOwner[owner] = web.styles;
      }
    }
    for (const list of Object.values(slotsBySlot)) {
      list.sort((a, b) => b.priority - a.priority);
    }
    set((s) => ({
      loaded: true,
      plugins,
      manifestPanels,
      settingsPanels,
      standalonePanels: deriveStandalone(s.hostPanels, manifestPanels),
      slotsBySlot,
      stylesByOwner,
    }));
    // annotators are rebuilt from the same manifest fetch; fire-and-forget so a
    // slow annotator script never blocks slot/panel rendering.
    void refreshAnnotators();
  },

  panelWinner: (overlayId) => {
    const { hostPanels, manifestPanels } = get();
    let winner: PanelContribution | null = null;
    for (const entry of hostPanels) {
      if (entry.overlayId !== overlayId) continue;
      if (winner === null || entry.priority > winner.priority) winner = entry;
    }
    for (const entry of manifestPanels) {
      if (entry.overlayId !== overlayId) continue;
      // strict > keeps the host entry on ties (host listed first)
      if (winner === null || entry.priority > winner.priority) winner = entry;
    }
    return winner;
  },

  setClientScripts: (enabled) => {
    try {
      localStorage.setItem(LS_CLIENT_SCRIPTS, enabled ? 'on' : 'off');
    } catch {
      /* storage unavailable; in-memory flag still flips */
    }
    set({ clientScriptsEnabled: enabled });
    void get().refresh();
  },
}));

// [plugin-admin 2026-08-23] Live manifest updates. Why: the backend now emits
// plugin_loaded / plugin_unloaded events (SupervisorState._emit_plugin_event);
// the store should follow without polling. How: subscribe once on the module
// level — pluginRuntime imports nothing from stores, so there is no cycle — and
// re-pull the manifest whenever a plugin lifecycle event arrives.
subscribePluginEvent('plugin_loaded', () => void usePluginsStore.getState().refresh());
subscribePluginEvent('plugin_unloaded', () => void usePluginsStore.getState().refresh());

/**
 * [AutoC 2026-08-27] Register a host built-in overlay into the same panel
 * registry the plugin manifest feeds. Why: built-in overlays and plugin
 * panels should travel one path — the host is just the first contributor, its
 * entries carry priority 0 so any plugin entry (default 50) outranks them and
 * unloading the plugin falls back automatically. How: idempotent append keyed
 * by contribution key; standalone entries refresh alongside. Purpose: kill the
 * hardcoded overlay switch and the replaces override table.
 */
export function registerHostPanel(entry: Omit<PanelContribution, 'owner' | 'kind'>): void {
  const full: PanelContribution = { ...entry, owner: 'host', kind: 'react' };
  usePluginsStore.setState((s) => {
    if (s.hostPanels.some((p) => p.key === full.key)) return s;
    const hostPanels = [...s.hostPanels, full];
    return { hostPanels, standalonePanels: deriveStandalone(hostPanels, s.manifestPanels) };
  });
}
