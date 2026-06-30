// [2026-06-03] Shared child-node status dot for Sidebar and ChildNodePanel.
// Why: both Phase 2 surfaces must use the same lifecycle colors and accessible
// labels. How: map normalized child-node statuses to Tailwind color classes and
// expose a label helper for surrounding text. Purpose: later child-session UI can
// reuse one status contract instead of drifting between panels.
import type { ChildNodeStatus } from '../../store/chatStore';

const statusColors: Record<ChildNodeStatus, string> = {
  running: 'bg-green-500 animate-pulse',
  awaiting_approval: 'bg-orange-400',
  completed: 'bg-gray-400',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-400',
};

const statusLabels: Record<ChildNodeStatus, string> = {
  running: '运行中',
  awaiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

interface StatusDotProps {
  status: ChildNodeStatus | string;
  label?: string;
  className?: string;
}

export function getChildNodeStatusLabel(status: ChildNodeStatus | string): string {
  // [2026-06-03] Keep unknown backend statuses readable. Why: lifecycle names can be
  // extended before the UI is updated. How: return the normalized Chinese label when
  // known and the raw status otherwise. Purpose: the panel remains diagnosable instead
  // of silently hiding unexpected states.
  return statusLabels[status as ChildNodeStatus] || status;
}

export const StatusDot = ({ status, label, className = '' }: StatusDotProps) => (
  <span
    aria-label={label}
    className={`inline-block h-2 w-2 rounded-full ${statusColors[status as ChildNodeStatus] || 'bg-gray-400'} ${className}`}
  />
);

export type { StatusDotProps };
