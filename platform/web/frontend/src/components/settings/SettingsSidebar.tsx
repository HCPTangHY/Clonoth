// [2026-06-01] Sidebar for the full settings view.
// Why: settings mode replaces the conversation list with settings categories while
// keeping the right panel independent. How: render tabs from settingsTabs and route
// all selection through viewStore. Purpose: new settings pages only need registry
// entries and do not require Sidebar or App changes.
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { settingsTabs } from './settingsTabs';

export const SettingsSidebar = () => {
  const { isConnected } = useSettingsStore();
  const { activeSettingsTab, closeSettings, setSettingsTab } = useViewStore();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-[var(--duties-border)] p-3">
        <button
          className="mb-3 flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)] hover:text-[var(--duties-text)]"
          onClick={closeSettings}
          type="button"
        >
          {/* [2026-06-01] Why: replace the back arrow glyph with Material Symbols.
              How: render the shared Icon using arrow_back. Purpose: settings navigation
              uses the same icon font as the rest of the interface. */}
          <Icon name="arrow_back" size={16} />
          <span>返回聊天</span>
        </button>
        <div className="flex items-center gap-2.5">
          <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-8 w-8 rounded-lg" />
          <div>
            <h1 className="font-mono text-base font-semibold tracking-[-0.04em]">设置</h1>
            <p className="text-[0.6rem] text-[var(--duties-tertiary)]">配置分类</p>
          </div>
        </div>
      </div>

      <nav aria-label="设置分区" className="min-h-0 flex-1 overflow-y-auto p-2">
        {settingsTabs.map((tab) => {
          const isActive = tab.id === activeSettingsTab;
          return (
            <button
              aria-current={isActive ? 'page' : undefined}
              aria-label={tab.label}
              // [2026-06-01] Keep the accessible tab name free of decorative icons.
              // Why: the visible label includes an icon for visual scanning, but
              // tests and assistive technology should target the stable text label.
              // How: set aria-label to the registered tab label. Purpose: adding or
              // changing icons does not change the navigation contract.
              className={`mb-1 flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs transition-colors hover:bg-[var(--duties-accent)] ${
                isActive ? 'bg-[var(--duties-muted)] text-[var(--duties-text)]' : 'text-[var(--duties-secondary)]'
              }`}
              key={tab.id}
              onClick={() => setSettingsTab(tab.id)}
              type="button"
            >
              {tab.icon && (
                <span className="w-4 text-center text-[var(--duties-tertiary)]">
                  {/* [2026-06-01] Why: settings tab icons are now Material Symbol names.
                      How: pass tab.icon into the shared Icon instead of printing a
                      stored glyph. Purpose: the registry remains data-driven without
                      keeping emoji or Unicode symbols in visible UI. */}
                  <Icon name={tab.icon} size={16} />
                </span>
              )}
              <span className="font-mono">{tab.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="border-t border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2 text-[0.7rem] text-[var(--duties-tertiary)]">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isConnected ? '已连接' : '已断开'}
        </div>
      </div>
    </div>
  );
};
