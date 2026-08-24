// [2026-05-16] Settings store — admin auth, node selection, model config, connection status.
// [2026-06-01] View mode state moved to viewStore.
// Why: chat/settings navigation is application shell state, while this store owns
// configuration data and the right-panel collapse flag. How: keep session editing
// in Header modal state and leave this store with only shared settings data.
// Purpose: App.tsx can use the view registry without a second routing system here.
import { create } from 'zustand';

import type { NodeDef } from '../types';

const LS_KEY_TOKEN = 'clonoth_admin_token';
const LS_KEY_NODE = 'clonoth_entry_node';
const LS_KEY_SIDEBAR_WIDTH = 'clonoth_sidebar_width';
const LS_KEY_RIGHT_WIDTH = 'clonoth_right_width';

type SessionProviderOverride = Record<string, unknown>;

const initialRightPanelOpen = () => window.innerWidth >= 768;

// [AutoC 2026-08-24] Resizable side columns. Widths persist across sessions;
// out-of-range stored values fall back to defaults. Clamp bounds match the
// drag handlers in AppLayout (left 180–420, right 220–640).
function readStoredWidth(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = Number(localStorage.getItem(key));
    if (Number.isFinite(raw) && raw >= min && raw <= max) return Math.round(raw);
  } catch { /* storage unavailable */ }
  return fallback;
}

interface SettingsState {
  adminToken: string | null;
  isAuthenticated: boolean;
  isConnected: boolean;
  entryNodeId: string;
  availableNodes: NodeDef[];
  modelConfig: { model: string; base_url: string; api_key_present: boolean } | null;
  // Active node tracking
  activeNodeId: string;
  activeNodeIsOverride: boolean;
  defaultNodeId: string;
  activeEffectiveProvider: string;
  activeEffectiveModel: string;
  activeEffectiveBaseUrl: string;
  globalModel: string;
  globalBaseUrl: string;
  providerModels: Record<string, string>;
  // [2026-06-01] Session-level provider override cache.
  // Why: the right panel shows the model/base_url that apply only to the current
  // session. How: store the latest provider_override object returned by Supervisor.
  // Purpose: Header, compact panel, and settings help read the same session-scoped
  // model state without duplicating fetch results.
  sessionProviderOverride: SessionProviderOverride | null;
  // [2026-06-16] Model override chosen on the welcome page (no active session yet).
  // Applied to the session on first message via postInbound, then cleared.
  pendingProviderOverride: SessionProviderOverride | null;
  // [2026-08-10] Session workspace name shared between Header badge and SessionConfigPanel.
  sessionWorkspaceName: string;
  // [2026-06-01] Right-panel visibility remains layout state shared by Header and
  // AppLayout. Why: viewStore selects which app view is active, but the right column
  // still needs an independent collapse flag. How: keep one boolean here. Purpose:
  // settings and chat views can both preserve the user's right-panel visibility.
  rightPanelOpen: boolean;
  // [AutoC 2026-08-24] Side column widths (px). Owned here so AppLayout's drag
  // handles and any future consumers share one source; persisted on change.
  sidebarWidth: number;
  rightPanelWidth: number;
  // [2026-06-12] True when the backend reports no provider has a valid API key.
  needsSetup: boolean;

  setAdminToken: (token: string | null) => void;
  setAuthenticated: (v: boolean) => void;
  setConnected: (v: boolean) => void;
  setEntryNodeId: (id: string) => void;
  setAvailableNodes: (nodes: NodeDef[]) => void;
  setModelConfig: (cfg: { model: string; base_url: string; api_key_present: boolean } | null) => void;
  setActiveNode: (nodeId: string, isOverride: boolean, defaultId: string, effective?: { provider?: string; model?: string; baseUrl?: string }) => void;
  setGlobalConfig: (model: string, baseUrl: string, providerModels?: Record<string, string>) => void;
  setSessionProviderOverride: (override: SessionProviderOverride | null) => void;
  setPendingProviderOverride: (override: SessionProviderOverride | null) => void;
  setSessionWorkspaceName: (name: string) => void;
  setRightPanelOpen: (open: boolean) => void;
  setSidebarWidth: (width: number) => void;
  setRightPanelWidth: (width: number) => void;
  setNeedsSetup: (v: boolean) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  adminToken: localStorage.getItem(LS_KEY_TOKEN),
  isAuthenticated: false,
  isConnected: false,
  entryNodeId: localStorage.getItem(LS_KEY_NODE) || '',
  availableNodes: [],
  modelConfig: null,
  activeNodeId: '',
  activeNodeIsOverride: false,
  defaultNodeId: '',
  activeEffectiveProvider: '',
  activeEffectiveModel: '',
  activeEffectiveBaseUrl: '',
  globalModel: '',
  globalBaseUrl: '',
  providerModels: {},
  sessionProviderOverride: null,
  pendingProviderOverride: null,
  sessionWorkspaceName: '',
  rightPanelOpen: initialRightPanelOpen(),
  sidebarWidth: readStoredWidth(LS_KEY_SIDEBAR_WIDTH, 240, 180, 420),
  rightPanelWidth: readStoredWidth(LS_KEY_RIGHT_WIDTH, 288, 220, 640),
  needsSetup: false,

  setAdminToken: (token) => {
    if (token) {
      localStorage.setItem(LS_KEY_TOKEN, token);
    } else {
      localStorage.removeItem(LS_KEY_TOKEN);
    }
    set({ adminToken: token });
  },
  setAuthenticated: (v) => set({ isAuthenticated: v }),
  setConnected: (v) => set({ isConnected: v }),
  setEntryNodeId: (id) => {
    localStorage.setItem(LS_KEY_NODE, id);
    set({ entryNodeId: id });
  },
  setAvailableNodes: (nodes) => set({ availableNodes: nodes }),
  setModelConfig: (cfg) => set({ modelConfig: cfg }),
  setActiveNode: (nodeId, isOverride, defaultId, effective) => set({
    activeNodeId: nodeId,
    activeNodeIsOverride: isOverride,
    defaultNodeId: defaultId,
    activeEffectiveProvider: effective?.provider || '',
    activeEffectiveModel: effective?.model || '',
    activeEffectiveBaseUrl: effective?.baseUrl || '',
  }),
  setGlobalConfig: (model, baseUrl, providerModels) => set({ globalModel: model, globalBaseUrl: baseUrl, providerModels: providerModels || {} }),
  setSessionProviderOverride: (override) => set({ sessionProviderOverride: override }),
  setPendingProviderOverride: (override) => set({ pendingProviderOverride: override }),
  setSessionWorkspaceName: (name) => set({ sessionWorkspaceName: name }),
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
  setSidebarWidth: (width) => {
    try { localStorage.setItem(LS_KEY_SIDEBAR_WIDTH, String(Math.round(width))); } catch { /* ignore */ }
    set({ sidebarWidth: Math.round(width) });
  },
  setRightPanelWidth: (width) => {
    try { localStorage.setItem(LS_KEY_RIGHT_WIDTH, String(Math.round(width))); } catch { /* ignore */ }
    set({ rightPanelWidth: Math.round(width) });
  },
  setNeedsSetup: (v) => set({ needsSetup: v }),
}));

export type { SessionProviderOverride, SettingsState };
