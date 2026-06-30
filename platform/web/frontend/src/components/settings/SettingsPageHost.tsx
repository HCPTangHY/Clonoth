// [2026-06-01] Main content host for registered settings tabs.
// Why: App.tsx should not know which concrete settings page is selected. How: read
// activeSettingsTab from viewStore and resolve the component through settingsTabs.
// Purpose: settings pages can be added, removed, or reordered without changing the
// root application component.
import { useViewStore } from '../../store/viewStore';
import { getSettingsTab } from './settingsTabs';

export const SettingsPageHost = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const tab = getSettingsTab(activeSettingsTab);
  const Page = tab.Page;

  return <Page />;
};
