// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';

export type ViewMode = 'chat' | 'settings';

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  openSettings: (tab?: string) => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string) => void;
}

const DEFAULT_SETTINGS_TAB = 'general';

export const useViewStore = create<ViewState>((set) => ({
  viewMode: 'chat',
  activeSettingsTab: DEFAULT_SETTINGS_TAB,

  openSettings: (tab) => set({
    // [2026-06-01] Opening settings optionally selects the requested tab first.
    // Why: Header node/model labels should land directly on the related settings
    // page. How: set both the view mode and tab in one store update. Purpose: the
    // shell swaps left and center content atomically.
    viewMode: 'settings',
    activeSettingsTab: tab || DEFAULT_SETTINGS_TAB,
  }),

  closeSettings: () => set({
    // [2026-06-01] Closing settings returns to chat without erasing the tab.
    // Why: preserving the tab makes a later Settings click reopen where the user
    // left off if a caller does not request a tab. How: only change viewMode.
    // Purpose: the navigation state stays predictable and compact.
    viewMode: 'chat',
  }),

  setSettingsTab: (tab) => set({
    // [2026-06-01] Tab changes are local to the settings view.
    // Why: adding future settings pages should not require App.tsx changes. How:
    // store only the tab id and let SettingsPageHost resolve it through the tab
    // registry. Purpose: tab routing remains data-driven.
    activeSettingsTab: tab,
  }),
}));

export { DEFAULT_SETTINGS_TAB };
