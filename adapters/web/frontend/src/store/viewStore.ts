// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';

import { usePluginsStore } from './pluginsStore';
import { useSettingsStore } from './settingsStore';

export type ViewMode = 'chat' | 'settings';

// Panel overlay — temporarily replaces the default left/right panel content.
// id is a free-form string matched by viewRegistry; null = show default content.
// intent is an opaque payload forwarded to the panel page on mount (postMessage).
export interface PanelOverlayState {
  left: string | null;
  right: string | null;
  leftIntent?: unknown;
  rightIntent?: unknown;
}

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  panelOverlay: PanelOverlayState;
  /** [AutoC 2026-08-25] Opaque open-intent handed to the panel page on mount.
   * Plugins pass any object; the host forwards it via postMessage and never
   * inspects it. Cleared on overlay switch/close. */
  openSettings: (tab?: string) => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string) => void;
  setPanelOverlay: (panel: 'left' | 'right', id: string | null, intent?: unknown) => void;
  clearPanelOverlays: () => void;
}

const DEFAULT_SETTINGS_TAB = 'general';

const _emptyOverlay: PanelOverlayState = { left: null, right: null };

export const useViewStore = create<ViewState>((set) => ({
  viewMode: 'chat',
  activeSettingsTab: DEFAULT_SETTINGS_TAB,
  panelOverlay: { ..._emptyOverlay },

  openSettings: (tab) => set({
    viewMode: 'settings',
    activeSettingsTab: tab || DEFAULT_SETTINGS_TAB,
    panelOverlay: { ..._emptyOverlay },
  }),

  closeSettings: () => set({
    viewMode: 'chat',
  }),

  setSettingsTab: (tab) => {
    // [AutoC 2026-08-28] 切换页签时清除上一页签的编辑器目标，插件页右栏
    // 回到提示文本（与内置页签的“未选中显示帮助”行为一致）。
    try {
      usePluginsStore.getState().setPluginEditorTarget(null);
    } catch { /* store 不可用时忽略 */ }
    // [AutoC 2026-08-28] 移动端：插件页签无编辑器时右栏常驻提示块会以
    // 全屏遮罩盖住主区。Why: AppLayout 在 <768px 把右栏渲染为 fixed
    // overlay，常驻提示块没有信息量却每次盖屏。How: 进入插件页签时
    // 主动收起；点开条目时由 editor bus 重新展开。桌面不受影响。
    if (typeof tab === 'string' && tab.startsWith('plugin:')
      && window.matchMedia('(max-width: 767px)').matches) {
      try { useSettingsStore.getState().setRightPanelOpen(false); } catch { /* ignore */ }
    }
    set({ activeSettingsTab: tab });
  },

  setPanelOverlay: (panel, id, intent) => set((s) => {
    const next: PanelOverlayState = { ...s.panelOverlay, [panel]: id };
    if (panel === 'right') next.rightIntent = intent;
    else next.leftIntent = intent;
    return { panelOverlay: next };
  }),

  clearPanelOverlays: () => set({
    panelOverlay: { ..._emptyOverlay },
  }),
}));

export { DEFAULT_SETTINGS_TAB };
