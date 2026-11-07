import { useEffect } from 'react';
import { useChatStore } from './chatStore';
import { type ApprovalLevel, useClientPrefsStore } from './clientPrefsStore';
import { compileApprovalPreset, identifyApprovalPreset } from './sessionApprovalRules';
import { useSessionApprovalStore } from './sessionApprovalStore';

export function useSessionApproval() {
  const sessionId = useChatStore((state) => state.viewingChildSessionId
    || state.conversations.find((item) => item.id === state.activeConversationId)?.sessionId || '');
  const defaults = useClientPrefsStore();
  const entry = useSessionApprovalStore((state) => state.sessions[sessionId]);
  const load = useSessionApprovalStore((state) => state.load);
  const save = useSessionApprovalStore((state) => state.save);
  useEffect(() => { if (sessionId) void load(sessionId); }, [sessionId, load]);
  const selection = sessionId
    ? identifyApprovalPreset(entry?.value?.effective_rules ?? null)
    : { level: defaults.approvalLevel, tools: defaults.autoApproveTools };
  return {
    sessionId,
    level: selection.level,
    tools: selection.tools,
    busy: Boolean(sessionId && (!entry || entry.loading || entry.saving)),
    error: entry?.error || '',
    effectiveRules: entry?.value?.effective_rules ?? null,
    setLevel: (level: ApprovalLevel) => {
      if (sessionId) void save(sessionId, compileApprovalPreset(level,
        selection.level === 'smart' ? selection.tools : defaults.autoApproveTools));
      else defaults.setApprovalLevel(level);
    },
    setTool: (tool: string, enabled: boolean) => {
      if (sessionId) void save(sessionId, compileApprovalPreset('smart', { ...selection.tools, [tool]: enabled }));
      else defaults.setAutoApproveTool(tool, enabled);
    },
    clear: () => { if (sessionId) void save(sessionId, null); },
    reload: () => { if (sessionId) void load(sessionId); },
  };
}
