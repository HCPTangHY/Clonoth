// [2026-06-01] Upper right panel host for settings mode.
// [2026-06-02] Settings now uses the full right column. Why: contextual editors need
// more room than the old upper 60 percent slot. How: AppLayout gives this host full
// height when settings omits rightBottom. Purpose: tabs can own full-height contextual
// content without changing App.tsx or each individual tab panel.
import { useViewStore } from '../../store/viewStore';
import { getSettingsTab } from './settingsTabs';

export const SettingsRightPanel = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const tab = getSettingsTab(activeSettingsTab);
  const RightPanel = tab.RightPanel;

  if (RightPanel) return <RightPanel />;

  return (
    <section className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
      <h2 className="mb-3 font-mono text-[0.65rem] font-semibold uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">设置帮助</h2>
      <div className="space-y-3 text-xs leading-5 text-[var(--duties-secondary)]">
        <p>
          {/* [2026-06-02] Keep help text aligned with the full-height settings rail.
              Why: Settings no longer renders EventLogPanel below this host. How: describe
              the panel as a dedicated contextual area. Purpose: users are not told to look
              for a lower event-log section that no longer exists in settings mode. */}
          此面板显示当前设置页面的说明。设置视图会把右侧区域留给当前分区的说明和编辑内容。
        </p>
        <p>
          当前分区： <span className="font-mono text-[var(--duties-text)]">{tab.label}</span>
        </p>
      </div>
    </section>
  );
};
