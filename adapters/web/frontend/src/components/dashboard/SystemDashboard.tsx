// [2026-06-01] Chat right-rail system dashboard.
// Why: the main chat view needs a persistent operations summary while session
// editing moves into a modal. How: poll Supervisor admin state and health every
// 15 seconds, then combine those results with the local WebSocket connection state.
// Purpose: users can see system health without losing chat context.
import { useEffect, useMemo, useState } from 'react';

import { checkHealth, getAdminState, type AdminState, type HealthState } from '../../api/supervisorClient';
import { useChatStore } from '../../store/chatStore';
import { useSettingsStore } from '../../store/settingsStore';

interface DashboardData {
  adminState: AdminState | null;
  health: HealthState | null;
  error: string;
  loading: boolean;
}

function formatUptime(seconds: number | undefined): string {
  // [2026-06-01] Why: a freshly started Supervisor can legitimately report zero
  // uptime seconds. How: treat only undefined and negative values as unknown.
  // Purpose: the dashboard does not hide a valid just-started health response.
  // [2026-06-01] Why: this value is visible in the dashboard. How: keep the same
  // duration calculation and translate the unit labels. Purpose: the whole user
  // interface reads consistently in Chinese without changing data semantics.
  if (seconds === undefined || seconds < 0) return '未知';
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}天 ${hours}小时`;
  if (hours > 0) return `${hours}小时 ${minutes}分钟`;
  if (minutes > 0) return `${minutes}分钟`;
  return `${total}秒`;
}

function connectionLabel(status: string): string {
  // [2026-06-01] Why: WebSocket status text is shown directly in the system panel.
  // How: preserve the status mapping and translate only the labels. Purpose: users
  // see localized connection state while reducers keep their original enum values.
  if (status === 'open') return 'WebSocket 已连接';
  if (status === 'connecting') return 'WebSocket 连接中';
  if (status === 'reconnecting') return 'WebSocket 重连中';
  if (status === 'closed') return 'WebSocket 已断开';
  return 'WebSocket 空闲';
}

function connectionDotClass(status: string): string {
  if (status === 'open') return 'bg-green-500';
  if (status === 'connecting' || status === 'reconnecting') return 'bg-yellow-500';
  if (status === 'closed') return 'bg-red-500';
  return 'bg-[var(--duties-tertiary)] opacity-50';
}

function engineStatus(adminState: AdminState | null): { label: string; detail: string } {
  const runtime = adminState?.engine_runtime || {};
  const workerId = typeof runtime.worker_id === 'string' ? runtime.worker_id : '';
  const workers = Array.isArray(runtime.workers) ? runtime.workers.filter((item): item is string => typeof item === 'string') : [];
  const lastSeenAt = typeof runtime.last_seen_at === 'string' ? runtime.last_seen_at : '';

  // [2026-06-01] Why: engine worker status is user-facing dashboard copy. How:
  // keep the raw worker identifiers and translate only descriptive text. Purpose:
  // operational identifiers remain exact while the surrounding interface is Chinese.
  if (workerId) return { label: workerId, detail: lastSeenAt ? `最近在线 ${new Date(lastSeenAt).toLocaleTimeString()}` : '工作进程已注册' };
  if (workers.length > 0) return { label: `${workers.length} 个工作进程`, detail: workers.join(', ') };
  return { label: '无工作进程', detail: '最近没有引擎工作进程上报。' };
}

function countRunningTasks(tasks: Record<string, number> | undefined): number {
  // [2026-06-01] Why: suspended tasks are still active downstream work even though
  // the enum separates them from running. How: include running, pending, and
  // suspended counts in the dashboard active-work number. Purpose: the right rail
  // matches the operational meaning of tasks that are not terminal yet.
  return (tasks?.running || 0) + (tasks?.pending || 0) + (tasks?.suspended || 0);
}

const Stat = ({ label, value, detail }: { label: string; value: string | number; detail?: string }) => (
  <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2.5">
    <p className="font-mono text-[0.55rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">{label}</p>
    <p className="mt-1 font-mono text-lg font-semibold tracking-[-0.04em] text-[var(--duties-text)]">{value}</p>
    {detail && <p className="mt-0.5 truncate text-[0.65rem] text-[var(--duties-secondary)]">{detail}</p>}
  </div>
);

export const SystemDashboard = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const isConnected = useSettingsStore(state => state.isConnected);
  const connectionStatus = useChatStore(state => state.connectionStatus);
  const [data, setData] = useState<DashboardData>({ adminState: null, health: null, error: '', loading: true });

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      if (!adminToken) {
        // [2026-06-01] Why: /v1/admin/state is protected but /v1/health is public.
        // How: show health-only data when no token is configured. Purpose: the
        // dashboard remains useful before the user enters admin credentials.
        try {
          const health = await checkHealth();
          if (!cancelled) setData({ adminState: null, health, error: '需要管理员令牌才能读取计数。', loading: false });
        } catch (error) {
          if (!cancelled) setData({ adminState: null, health: null, error: error instanceof Error ? error.message : '健康检查失败。', loading: false });
        }
        return;
      }

      try {
        const [adminState, health] = await Promise.all([getAdminState(adminToken), checkHealth()]);
        if (!cancelled) setData({ adminState, health, error: '', loading: false });
      } catch (error) {
        if (!cancelled) setData((current) => ({ ...current, error: error instanceof Error ? error.message : '仪表盘刷新失败。', loading: false }));
      }
    };

    void load();
    const timer = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [adminToken]);

  const pendingApprovals = data.adminState?.approvals?.pending ?? data.adminState?.pending_approvals?.length ?? 0;
  const activeTasks = countRunningTasks(data.adminState?.tasks);
  const engine = useMemo(() => engineStatus(data.adminState), [data.adminState]);

  return (
    <section aria-label="系统仪表盘" className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <p className="font-mono text-[0.55rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">通用</p>
          <h2 className="mt-1 font-mono text-sm font-semibold tracking-[-0.03em]">系统仪表盘</h2>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-sm border border-[var(--duties-border)] px-2 py-1 font-mono text-[0.55rem] text-[var(--duties-secondary)]">
          <span className={`h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isConnected ? 'HTTP 正常' : 'HTTP 断开'}
        </span>
      </div>

      <div className="mb-3 inline-flex items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2.5 py-2 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
        <span className={`h-2 w-2 rounded-full ${connectionDotClass(connectionStatus)}`} />
        <span>{connectionLabel(connectionStatus)}</span>
      </div>

      {data.error && (
        <div className="mb-3 border border-orange-200 bg-orange-50 px-2.5 py-2 text-[0.65rem] text-orange-700">
          {data.error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <Stat label="会话数" value={data.adminState?.sessions ?? (data.loading ? '…' : '无数据')} />
        <Stat label="待审批" value={pendingApprovals} />
        <Stat label="运行中任务" value={activeTasks} detail="运行中、等待中、已挂起" />
        <Stat label="运行时间" value={formatUptime(data.health?.uptime_seconds)} />
      </div>

      <div className="mt-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2.5">
        <p className="font-mono text-[0.55rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">引擎工作进程</p>
        <p className="mt-1 truncate font-mono text-xs font-semibold text-[var(--duties-text)]">{engine.label}</p>
        <p className="mt-1 text-[0.65rem] leading-4 text-[var(--duties-secondary)]">{engine.detail}</p>
      </div>

    </section>
  );
};
