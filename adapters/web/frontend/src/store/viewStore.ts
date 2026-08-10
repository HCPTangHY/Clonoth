// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';

export type ViewMode = 'chat' | 'settings';

// Right panel overlay — temporarily replaces the default right panel content
// in chat mode. Used by workspace file tree, diff preview, etc.
export type RightPanelOverlay = 'files' | null;

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  rightPanelOverlay: RightPanelOverlay;
  openSettings: (tab?: string) => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string) => void;
  openRightPanelOverlay: (overlay: RightPanelOverlay) => void;
  closeRightPanelOverlay: () => void;
}

const DEFAULT_SETTINGS_TAB = 'general';

export const useViewStore = create<ViewState>((set) => ({
  viewMode: 'chat',
  activeSettingsTab: DEFAULT_SETTINGS_TAB,
  rightPanelOverlay: null,

  openSettings: (tab) => set({
    viewMode: 'settings',
    activeSettingsTab: tab || DEFAULT_SETTINGS_TAB,
    rightPanelOverlay: null,
  }),

  closeSettings: () => set({
    viewMode: 'chat',
  }),

  setSettingsTab: (tab) => set({
    activeSettingsTab: tab,
  }),

  openRightPanelOverlay: (overlay) => set({
    rightPanelOverlay: overlay,
  }),

  closeRightPanelOverlay: () => set({
    rightPanelOverlay: null,
  }),
}));

export { DEFAULT_SETTINGS_TAB };
