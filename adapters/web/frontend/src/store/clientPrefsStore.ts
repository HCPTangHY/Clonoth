// [2026-06-01] Browser-local client preferences store.
// Why: auto-approval and rendering defaults are build-local frontend behavior and
// must not modify backend policy or session data. How: keep a small Zustand store
// backed by localStorage with explicit defaults and safe fallback rules. Purpose:
// each deployed frontend can choose its own approval and display preferences.
import { create } from 'zustand';

export type TitleGenerationMode = 'auto' | 'manual' | 'first-message';
export type ApprovalLevel = 'manual' | 'smart' | 'yolo';

export interface ClientPrefs {
  // [2026-06-05] Why: automatic approvals now have a top-level safety mode instead
  // of being controlled only by individual tool checkboxes. How: store the selected
  // approval level beside the refinement map. Purpose: every renderer and submission
  // path can resolve the same manual, smart, or yolo behavior after a page reload.
  approvalLevel: ApprovalLevel;
  autoApproveTools: Record<string, boolean>;
  titleGeneration: TitleGenerationMode;
  thinkingDefaultCollapsed: boolean;
  toolResultsDefaultCollapsed: boolean;
}

interface ClientPrefsState extends ClientPrefs {
  setApprovalLevel: (level: ApprovalLevel) => void;
  setAutoApproveTool: (toolName: string, enabled: boolean) => void;
  setTitleGeneration: (mode: TitleGenerationMode) => void;
  setThinkingDefaultCollapsed: (collapsed: boolean) => void;
  setToolResultsDefaultCollapsed: (collapsed: boolean) => void;
  resetClientPrefs: () => void;
}

const LS_KEY_CLIENT_PREFS = 'clonoth_client_prefs';

export const DEFAULT_AUTO_APPROVE_TOOLS: Record<string, boolean> = {
  // [2026-06-05] Why: Level 2 smart mode now allows normal tools by default and uses
  // this map only as the user's refinement layer. How: keep known common tools set to
  // true and leave request_restart false for display consistency. Purpose: the default
  // Client page matches the smart policy while still letting users opt tools out.
  read_file: true,
  grep: true,
  list_dir: true,
  execute_command: true,
  write_file: true,
  apply_diff: true,
  request_restart: false,
};

export const DEFAULT_CLIENT_PREFS: ClientPrefs = {
  approvalLevel: 'smart',
  autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS },
  titleGeneration: 'first-message',
  thinkingDefaultCollapsed: true,
  toolResultsDefaultCollapsed: true,
};

function isTitleGenerationMode(value: unknown): value is TitleGenerationMode {
  return value === 'auto' || value === 'manual' || value === 'first-message';
}

function isApprovalLevel(value: unknown): value is ApprovalLevel {
  // [2026-06-05] Why: persisted localStorage can contain stale or hand-edited approval
  // levels. How: accept only the three supported string literals. Purpose: malformed
  // values fall back to the smart default without breaking startup.
  return value === 'manual' || value === 'smart' || value === 'yolo';
}

function readStoredPrefs(): Partial<ClientPrefs> {
  // [2026-06-01] Why: localStorage may contain stale or hand-edited JSON.
  // How: parse defensively and keep only fields matching the current interface.
  // Purpose: a bad browser value cannot break application startup.
  try {
    const raw = localStorage.getItem(LS_KEY_CLIENT_PREFS);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<ClientPrefs>;
    return {
      // [2026-06-05] Why: approvalLevel is persisted with the rest of the client
      // preferences. How: validate it before merging with defaults. Purpose: a
      // browser reload restores the selected Level 1, Level 2, or Level 3 mode.
      approvalLevel: isApprovalLevel(parsed.approvalLevel) ? parsed.approvalLevel : undefined,
      autoApproveTools: parsed.autoApproveTools && typeof parsed.autoApproveTools === 'object'
        ? Object.fromEntries(Object.entries(parsed.autoApproveTools).map(([key, value]) => [key, value === true]))
        : undefined,
      titleGeneration: isTitleGenerationMode(parsed.titleGeneration) ? parsed.titleGeneration : undefined,
      thinkingDefaultCollapsed: typeof parsed.thinkingDefaultCollapsed === 'boolean' ? parsed.thinkingDefaultCollapsed : undefined,
      toolResultsDefaultCollapsed: typeof parsed.toolResultsDefaultCollapsed === 'boolean' ? parsed.toolResultsDefaultCollapsed : undefined,
    };
  } catch {
    return {};
  }
}

function mergePrefs(stored: Partial<ClientPrefs>): ClientPrefs {
  return {
    ...DEFAULT_CLIENT_PREFS,
    ...stored,
    autoApproveTools: {
      ...DEFAULT_AUTO_APPROVE_TOOLS,
      ...(stored.autoApproveTools || {}),
    },
  };
}

function persistPrefs(prefs: ClientPrefs) {
  // [2026-06-01] Why: Zustand persist middleware is unnecessary for this tiny store.
  // How: write the serialized public preference object after every setter. Purpose:
  // tests and runtime code can inspect one stable localStorage key.
  localStorage.setItem(LS_KEY_CLIENT_PREFS, JSON.stringify(prefs));
}

function publicPrefs(state: ClientPrefsState): ClientPrefs {
  return {
    approvalLevel: state.approvalLevel,
    autoApproveTools: state.autoApproveTools,
    titleGeneration: state.titleGeneration,
    thinkingDefaultCollapsed: state.thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed: state.toolResultsDefaultCollapsed,
  };
}

export function shouldAutoApproveTool(
  toolName: string,
  rules: Record<string, boolean>,
  approvalLevel: ApprovalLevel,
): boolean {
  // [2026-06-05] Why: the frontend approval policy is now selected by level. How:
  // manual denies every automatic approval, yolo allows every request, and smart lets
  // all non-restart tools pass unless the refinement map explicitly disables them.
  // Purpose: UI badges and actual approval submission cannot drift apart.
  if (approvalLevel === 'manual') return false;
  if (approvalLevel === 'yolo') return true;
  if (toolName === 'request_restart' || toolName === 'restart') return false;
  if (Object.prototype.hasOwnProperty.call(rules, toolName) && rules[toolName] === false) return false;
  return true;
}

export const useClientPrefsStore = create<ClientPrefsState>((set, get) => ({
  ...mergePrefs(readStoredPrefs()),

  setApprovalLevel: (level) => set((state) => {
    // [2026-06-05] Why: approval level changes affect every future approval request.
    // How: persist the new level with the existing public preference snapshot. Purpose:
    // a reload preserves the selected safety mode before the next WebSocket event.
    const nextState = { ...state, approvalLevel: level };
    persistPrefs(publicPrefs(nextState));
    return { approvalLevel: level };
  }),

  setAutoApproveTool: (toolName, enabled) => set((state) => {
    const nextState = {
      ...state,
      autoApproveTools: { ...state.autoApproveTools, [toolName]: enabled },
    };
    persistPrefs(publicPrefs(nextState));
    return { autoApproveTools: nextState.autoApproveTools };
  }),

  setTitleGeneration: (mode) => set((state) => {
    const nextState = { ...state, titleGeneration: mode };
    persistPrefs(publicPrefs(nextState));
    return { titleGeneration: mode };
  }),

  setThinkingDefaultCollapsed: (collapsed) => set((state) => {
    const nextState = { ...state, thinkingDefaultCollapsed: collapsed };
    persistPrefs(publicPrefs(nextState));
    return { thinkingDefaultCollapsed: collapsed };
  }),

  setToolResultsDefaultCollapsed: (collapsed) => set((state) => {
    const nextState = { ...state, toolResultsDefaultCollapsed: collapsed };
    persistPrefs(publicPrefs(nextState));
    return { toolResultsDefaultCollapsed: collapsed };
  }),

  resetClientPrefs: () => {
    const next = { ...DEFAULT_CLIENT_PREFS, autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS } };
    persistPrefs(next);
    set(next);
  },
}));

export { LS_KEY_CLIENT_PREFS };
export type { ClientPrefsState };
