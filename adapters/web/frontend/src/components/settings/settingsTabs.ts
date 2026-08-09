// [2026-06-01] Settings tab registry for the full settings view.
// Why: adding settings pages should be a registration change, not another App.tsx
// conditional. How: each tab provides a label, order, main Page, and optional right
// panel component. Purpose: the settings sidebar, host, and right panel all resolve
// pages from the same data source.
import { createElement, type ComponentType } from 'react';

import { ClientSettingsPage } from './pages/ClientSettingsPage';
import { GeneralSettingsPage } from './pages/GeneralSettingsPage';
import { ModelSettingsPage } from './pages/ModelSettingsPage';
import { NodeSettingsPage } from './pages/NodeSettingsPage';
import { AdvancedSettingsPage } from './pages/AdvancedSettingsPage';
import { AgentsSettingsPage } from './pages/AgentsSettingsPage';
import { ApprovalsSettingsPage } from './pages/ApprovalsSettingsPage';
import { AutomationSettingsPage } from './pages/AutomationSettingsPage';
import { McpSettingsPage } from './pages/McpSettingsPage';
import { SkillsSettingsPage } from './pages/SkillsSettingsPage';
import { SystemSettingsPage } from './pages/SystemSettingsPage';
import { ToolsSettingsPage } from './pages/ToolsSettingsPage';
import {
  AgentsSettingsRightPanel,
  AutomationSettingsRightPanel,
  McpSettingsRightPanel,
  SkillsSettingsRightPanel,
  ToolsSettingsRightPanel,
} from './panels/SettingsContextPanels';
import { SessionConfigPanel } from './SessionConfigPanel';

export interface SettingsTabDefinition {
  id: string;
  label: string;
  // [2026-06-01] Why: settings icon values used to be literal Unicode glyphs.
  // How: keep the field as a string but store Material Symbol names instead.
  // Purpose: renderers can pass the value directly to the shared Icon component.
  icon?: string;
  order: number;
  Page: ComponentType;
  RightPanel?: ComponentType;
}

const ModelSettingsRightPanel = () => {
  // [2026-06-01] Reuse the existing session model helper for the Model tab's
  // right column. Why: the main Model page edits global defaults, while the right
  // column should still explain or edit the current session's inherited override.
  // How: create the element without JSX so this registry can remain the requested
  // settingsTabs.ts file. Purpose: settings mode keeps the requested 60/40 right-
  // panel structure without reintroducing the old full-height model override.
  return createElement(SessionConfigPanel, { focus: 'model', sessionId: 'no-session' });
};

export const settingsTabs: SettingsTabDefinition[] = [
  { id: 'general', label: '通用', icon: 'tune', order: 0, Page: GeneralSettingsPage },
  // [2026-06-01] Register browser-only preferences as a first-class settings tab.
  // Why: auto-approval and render defaults are local frontend choices, not backend
  // policy. How: point the new Client tab to ClientSettingsPage. Purpose: future
  // client preferences can be added without editing App.tsx or settings hosts.
  { id: 'client', label: '客户端', icon: 'display_settings', order: 1, Page: ClientSettingsPage },
  { id: 'model', label: '模型', icon: 'model_training', order: 2, Page: ModelSettingsPage, RightPanel: ModelSettingsRightPanel },
  // [2026-06-02] Register the full P0/P1 settings surface requested by operators.
  // Why: system, approvals, agents, tools, skills, MCP, automation, and advanced raw
  // config are independent settings domains. How: add each page with a Material
  // Symbol icon, explicit order, and contextual right panel. Purpose: future settings
  // navigation remains data-driven through this one registry.
  { id: 'system', label: '系统', icon: 'settings_power', order: 4, Page: SystemSettingsPage },
  { id: 'approvals', label: '审批', icon: 'approval', order: 5, Page: ApprovalsSettingsPage },
  { id: 'agents', label: '节点', icon: 'smart_toy', order: 6, Page: AgentsSettingsPage, RightPanel: AgentsSettingsRightPanel },
  { id: 'tools', label: '工具', icon: 'build', order: 7, Page: ToolsSettingsPage, RightPanel: ToolsSettingsRightPanel },
  { id: 'skills', label: '技能', icon: 'menu_book', order: 8, Page: SkillsSettingsPage, RightPanel: SkillsSettingsRightPanel },
  { id: 'mcp', label: 'MCP', icon: 'cable', order: 9, Page: McpSettingsPage, RightPanel: McpSettingsRightPanel },
  { id: 'automation', label: '自动化', icon: 'schedule', order: 10, Page: AutomationSettingsPage, RightPanel: AutomationSettingsRightPanel },
  { id: 'advanced', label: '高级', icon: 'code', order: 11, Page: AdvancedSettingsPage },
].sort((a, b) => a.order - b.order);

export function getSettingsTab(tabId: string): SettingsTabDefinition {
  // [2026-06-01] Unknown tab ids fall back to the first registered settings page.
  // Why: stale links or future removed tabs should not blank the settings view. How:
  // resolve through the registry and return settingsTabs[0] as a safe default.
  // Purpose: the host and right panel share identical fallback behavior.
  return settingsTabs.find(tab => tab.id === tabId) || settingsTabs[0];
}
