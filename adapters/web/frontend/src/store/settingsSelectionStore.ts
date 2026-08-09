// [2026-06-02] Cross-panel selection state for expanded settings pages.
// Why: SettingsRightPanel renders separately from the main page, but several new
// tabs need the right column to describe the selected approval, node, tool, skill, or
// config file. How: keep a small Zustand store with serializable snapshots. Purpose:
// pages can update contextual right-panel data without changing the settings host.
import { create } from 'zustand';

import type { AdminApproval, AdminNode, AdminSkill, AdminTool, McpClient } from '../api/supervisorClient';

export interface SettingsSelectionState {
  systemLogs: string[];
  selectedApproval: AdminApproval | null;
  selectedNode: AdminNode | null;
  selectedTool: AdminTool | null;
  allToolNames: string[];
  selectedSkill: AdminSkill | null;
  selectedMcpClient: McpClient | null;
  selectedScheduleId: string | null;
  advancedFile: 'runtime' | 'policy';
  addSystemLog: (message: string) => void;
  setSelectedApproval: (approval: AdminApproval | null) => void;
  setSelectedNode: (node: AdminNode | null) => void;
  setSelectedTool: (tool: AdminTool | null) => void;
  setAllToolNames: (names: string[]) => void;
  setSelectedSkill: (skill: AdminSkill | null) => void;
  setSelectedMcpClient: (client: McpClient | null) => void;
  setSelectedScheduleId: (scheduleId: string | null) => void;
  setAdvancedFile: (file: 'runtime' | 'policy') => void;
}

export const useSettingsSelectionStore = create<SettingsSelectionState>((set) => ({
  systemLogs: [],
  selectedApproval: null,
  selectedNode: null,
  selectedTool: null,
  allToolNames: [],
  selectedSkill: null,
  selectedMcpClient: null,
  // [2026-06-02] Store the selected automation task id for the right-panel form.
  // Why: the Automation page and SettingsRightPanel are mounted as separate siblings.
  // How: keep only the id, while the panel reloads raw schedules before editing.
  // Purpose: the middle page can remain a list and the editor can live in the rail.
  selectedScheduleId: null,
  advancedFile: 'runtime',

  addSystemLog: (message) => set((state) => ({
    // [2026-06-02] Keep only recent operation messages. Why: the right panel should
    // show reload/restart feedback without growing unbounded in local memory. How:
    // prepend a timestamped entry and cap the list at ten rows. Purpose: operations
    // remain visible while the UI stays lightweight.
    systemLogs: [`${new Date().toLocaleTimeString()} ${message}`, ...state.systemLogs].slice(0, 10),
  })),
  setSelectedApproval: (approval) => set({ selectedApproval: approval }),
  setSelectedNode: (node) => set({ selectedNode: node }),
  setSelectedTool: (tool) => set({ selectedTool: tool }),
  setAllToolNames: (names) => set({ allToolNames: names }),
  setSelectedSkill: (skill) => set({ selectedSkill: skill }),
  setSelectedMcpClient: (client) => set({ selectedMcpClient: client }),
  setSelectedScheduleId: (scheduleId) => set({ selectedScheduleId: scheduleId }),
  setAdvancedFile: (file) => set({ advancedFile: file }),
}));
