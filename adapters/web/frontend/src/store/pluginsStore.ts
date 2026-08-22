// [AutoC 2026-08-22] Plugin client-contribution store.
// Why: plugins declare frontend contributions (panels/slots/styles) in backend
// PLUGIN_META; the web adapter is one consumer of that manifest and holds no
// plugin state of its own. How: fetch /v1/plugins on startup, normalize the
// three tiers into lookup structures, and group by owner so unload/disappear
// semantics mirror the backend DisposalLedger. Purpose: plugin UI appears and
// disappears with the backend plugin list, with no frontend build involved.
import { create } from 'zustand';

import {
  listPlugins,
  type PluginListItem,
} from '../api/supervisorClient';

export interface ResolvedPanel {
  /** namespaced overlay id: plugin:{owner}:{panel-id} */
  key: string;
  owner: string;
  panelId: string;
  title: string;
  entry: string;
}

export interface SlotContribution {
  slotId: string;
  owner: string;
  slot: string;
  script: string;
  priority: number;
}

interface PluginsState {
  loaded: boolean;
  plugins: PluginListItem[];
  panels: ResolvedPanel[];
  slotsBySlot: Record<string, SlotContribution[]>;
  stylesByOwner: Record<string, string>;
  clientScriptsEnabled: boolean;
  refresh: () => Promise<void>;
  panelByKey: (key: string) => ResolvedPanel | null;
  setClientScripts: (enabled: boolean) => void;
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
  panels: [],
  slotsBySlot: {},
  stylesByOwner: {},
  clientScriptsEnabled: readClientScriptsPref(),

  refresh: async () => {
    let plugins: PluginListItem[] = [];
    try {
      plugins = await listPlugins();
    } catch {
      // backend unreachable or not yet authenticated; keep previous state
      return;
    }
    const scriptsOn = get().clientScriptsEnabled;
    const panels: ResolvedPanel[] = [];
    const slotsBySlot: Record<string, SlotContribution[]> = {};
    const stylesByOwner: Record<string, string> = {};
    for (const plugin of plugins) {
      const client = plugin.client;
      if (!client || typeof client !== 'object') continue;
      const owner = plugin.name;
      for (const panel of client.panels || []) {
        if (!panel?.id || !panel.entry) continue;
        // v1 supports the right overlay slot only
        if (panel.slot && panel.slot !== 'right') continue;
        panels.push({
          key: `plugin:${owner}:${panel.id}`,
          owner,
          panelId: panel.id,
          title: panel.title || panel.id,
          entry: panel.entry,
        });
      }
      if (scriptsOn) {
        for (const slot of client.slots || []) {
          if (!slot?.slot_id || !slot.slot || !slot.script) continue;
          const entry: SlotContribution = {
            slotId: slot.slot_id,
            owner,
            slot: slot.slot,
            script: slot.script,
            priority: Number.isFinite(slot.priority) ? Number(slot.priority) : 50,
          };
          (slotsBySlot[entry.slot] ||= []).push(entry);
        }
      }
      if (typeof client.styles === 'string' && client.styles.trim()) {
        stylesByOwner[owner] = client.styles;
      }
    }
    for (const list of Object.values(slotsBySlot)) {
      list.sort((a, b) => b.priority - a.priority);
    }
    set({ loaded: true, plugins, panels, slotsBySlot, stylesByOwner });
  },

  panelByKey: (key) => get().panels.find((p) => p.key === key) || null,

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
