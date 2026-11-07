import { create } from 'zustand';
import { getSessionApprovalRules, updateSessionApprovalRules } from '../api/supervisorClient';
import type { SessionApprovalRules, SessionApprovalState } from './sessionApprovalRules';

interface Entry {
  value?: SessionApprovalState;
  loading: boolean;
  saving: boolean;
  error: string;
}
interface ApprovalStore {
  sessions: Record<string, Entry>;
  load: (sessionId: string) => Promise<void>;
  save: (sessionId: string, rules: SessionApprovalRules | null) => Promise<void>;
  refresh: () => void;
  reset: () => void;
}

const revisions = new Map<string, number>();
let generation = 0;
export const useSessionApprovalStore = create<ApprovalStore>((set, get) => {
  const request = async (sessionId: string, saving: boolean, rules?: SessionApprovalRules | null) => {
    if (!sessionId || get().sessions[sessionId]?.saving) return;
    const revision = (revisions.get(sessionId) || 0) + 1;
    revisions.set(sessionId, revision);
    const startedGeneration = generation;
    const current = () => startedGeneration === generation && revisions.get(sessionId) === revision;
    const patch = (entry: Partial<Entry>) => set((state) => ({
      sessions: {
        ...state.sessions,
        [sessionId]: { ...(state.sessions[sessionId] ?? { loading: false, saving: false, error: '' }), ...entry },
      },
    }));
    patch({ loading: !saving, saving, error: '' });
    try {
      const value = saving
        ? await updateSessionApprovalRules(sessionId, rules ?? null)
        : await getSessionApprovalRules(sessionId);
      if (current()) patch({ value, loading: false, saving: false });
    } catch (error) {
      if (current()) patch({ loading: false, saving: false, error: error instanceof Error ? error.message : '审批规则请求失败' });
    }
  };
  return {
    sessions: {},
    load: (sessionId) => request(sessionId, false),
    save: (sessionId, rules) => request(sessionId, true, rules),
    // Refresh cached children as well: they may inherit the changed parent's rules.
    refresh: () => { for (const sessionId of Object.keys(get().sessions)) void get().load(sessionId); },
    reset: () => { generation += 1; revisions.clear(); set({ sessions: {} }); },
  };
});
