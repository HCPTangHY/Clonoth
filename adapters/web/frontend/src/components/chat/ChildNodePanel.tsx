// [2026-06-03] Floating child-node panel for the active conversation.
// Why: delegated child agents now live in separate Supervisor sessions and need a
// compact runtime view beside the chat stream. How: read normalized childNodes from
// chatStore, gate the panel on active child work, and refresh runtime text once per
// second only while a running child exists. Purpose: Phase 2 exposes child activity
// without implementing Phase 3 child-session navigation.
import { useEffect, useMemo, useState } from 'react';

import { useChatStore, type ChildNodeState } from '../../store/chatStore';
import { getChildNodeStatusLabel, StatusDot } from '../common';

interface ChildNodePanelProps {
  conversationId: string;
}

function selectChildNodesForConversation(
  childNodes: Readonly<Record<string, ChildNodeState>>,
  conversationId: string,
): ChildNodeState[] {
  // [2026-06-03] Keep panel filtering local to the renderer. Why: the data layer is
  // already complete for Phase 1 and should not be changed for UI grouping. How:
  // filter the normalized childNodes map by parentConversationId and sort by start
  // time. Purpose: the floating panel mirrors Sidebar order while avoiding store edits.
  return Object.values(childNodes)
    .filter((child) => {
      if (child.parentConversationId !== conversationId) return false;
      // [AutoC 2026-06-04] Why: system nodes (turn_summarizer, memory_extractor,
      // compactor, dream) are internal housekeeping and should not clutter the
      // operator-facing panel. How: exclude any node whose id starts with 'system.'.
      // Purpose: only user-visible child agents appear in the floating panel.
      if (child.nodeId.startsWith('system.')) return false;
      // [AutoC 2026-06-04] Why: completed/failed/cancelled child nodes should not
      // persist in the panel indefinitely. How: hide terminal nodes whose completedAt
      // is older than 30 seconds. Purpose: brief flash of completion then auto-hide.
      if (child.completedAt) {
        const elapsed = Date.now() - new Date(child.completedAt).getTime();
        if (elapsed > 30_000) return false;
      }
      return true;
    })
    .sort((a, b) => (a.startedAt || '').localeCompare(b.startedAt || ''));
}

function formatRuntime(startedAt: string | undefined, now: number): string | null {
  if (!startedAt) return null;
  const started = new Date(startedAt).getTime();
  if (Number.isNaN(started)) return null;

  const totalSeconds = Math.max(0, Math.floor((now - started) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  // [2026-06-03] Use compact runtime labels in the row subtitle. Why: the panel is
  // only 14rem wide and should keep node id, duration, and status on one readable
  // row. How: include hours only when needed. Purpose: short tasks show as "2m 15s"
  // while long tasks remain understandable.
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

const ChildNodeRow = ({ child, now }: { child: ChildNodeState; now: number }) => {
  const runtime = formatRuntime(child.startedAt, now);
  const statusLabel = getChildNodeStatusLabel(child.status);
  const viewChildSession = useChatStore(state => state.viewChildSession);

  return (
    <button
      aria-label={`打开子节点 ${child.nodeId}`}
      className="flex w-full items-center gap-1.5 border-b border-[var(--duties-border)] px-2 py-1.5 text-left transition-colors last:border-b-0 hover:bg-[var(--duties-muted)]"
      onClick={() => {
        // [2026-06-03] Why: Phase 3 needs the floating child row to open the child's
        // own ConversationStore stream. How: delegate to chatStore.viewChildSession,
        // which switches the view and hydrates history. Purpose: this panel becomes a
        // navigation entry instead of a development-only placeholder.
        viewChildSession(child.sessionId);
      }}
      type="button"
    >
      <StatusDot
        label={`子节点 ${child.nodeId} 状态：${statusLabel}`}
        status={child.status}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[0.7rem] font-medium text-[var(--duties-text)]">
          {child.nodeId}{runtime ? ` · ${runtime}` : ''}
        </span>
      </span>
      <span className="flex-shrink-0 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
        {statusLabel}
      </span>
    </button>
  );
};

export const ChildNodePanel = ({ conversationId }: ChildNodePanelProps) => {
  const childNodeMap = useChatStore(state => state.childNodes);
  const hasActiveChildNodes = useChatStore(state => state.selectHasActiveChildNodes(conversationId));
  const [now, setNow] = useState(() => Date.now());
  const childNodes = useMemo(
    () => selectChildNodesForConversation(childNodeMap, conversationId),
    [childNodeMap, conversationId],
  );
  const hasRunningChildNode = childNodes.some((child) => child.status === 'running');

  useEffect(() => {
    // [2026-06-03] Refresh runtime labels only while work is actually running.
    // Why: completed and approval-only rows do not need a ticking timer. How: start a
    // one-second interval when at least one child has status "running" and clear it
    // on status changes or unmount. Purpose: live duration stays current without a
    // permanent app-wide timer.
    if (!hasRunningChildNode) {
      setNow(Date.now());
      return undefined;
    }

    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningChildNode]);

  if (!hasActiveChildNodes || childNodes.length === 0) return null;

  return (
    <div className="absolute right-2 top-2 z-30 w-44 rounded-md border border-[var(--duties-border)] bg-[var(--duties-surface)] shadow-md">
      <div className="border-b border-[var(--duties-border)] px-2 py-1.5">
        <span className="font-mono text-[0.65rem] font-semibold">子节点</span>
      </div>
      <div className="max-h-48 overflow-y-auto">
        {childNodes.map((child) => (
          <ChildNodeRow child={child} key={child.sessionId} now={now} />
        ))}
      </div>
    </div>
  );
};

export type { ChildNodePanelProps };
