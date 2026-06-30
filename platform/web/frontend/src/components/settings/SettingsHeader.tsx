// [2026-06-01] Header for the full settings view.
// Why: settings mode replaces the chat title with the active settings category.
// How: resolve activeSettingsTab through the same registry used by the sidebar and
// page host. Purpose: the shell header remains a slot and App.tsx stays data-driven.
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { getSettingsTab } from './settingsTabs';

export const SettingsHeader = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const tab = getSettingsTab(activeSettingsTab);

  return (
    <header className="px-3 py-2 sm:px-4 sm:py-3">
      <div className="mx-auto flex max-w-3xl items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="truncate font-mono text-sm font-semibold tracking-[-0.03em]">设置</h2>
          <div className="mt-1 flex items-center gap-1.5 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
            <span className="inline-flex items-center gap-1.5">
              {/* [2026-06-01] Why: header fallback used a literal bullet glyph.
                  How: render the tab's Material Symbol name and fall back to tune.
                  Purpose: the settings header no longer emits decorative Unicode. */}
              <Icon name={tab.icon || 'tune'} size={14} />
              <span>{tab.label}</span>
            </span>
          </div>
        </div>
      </div>
    </header>
  );
};
