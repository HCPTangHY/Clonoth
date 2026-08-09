// [2026-06-02] System settings page for Supervisor status and runtime controls.
import { useCallback, useEffect, useMemo, useState } from 'react';

import { ActiveTasksModal } from '../../dashboard/ActiveTasksModal';
import { SessionBrowserModal } from '../../dashboard/SessionBrowserModal';

import { checkHealth, getAdminState, getRuntimeRaw, reloadConfig, restartEngine, updateRuntimeRaw, type AdminState, type HealthState } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { useViewStore } from '../../../store/viewStore';
import { parseRuntimeConfig, serializeRuntimeConfig, type RuntimeConfigFormState, type RuntimeToolMode, type OutputMode } from '../settingsStructuredConfig';
import { Button } from '../../common';
import { AuthRequired, Card, PageHeader, PageShell, StatusText, countActiveTasks, formatUptime } from './settingsPagePrimitives';

const RUNTIME_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm';
const RUNTIME_LABEL_CLASS = 'block text-xs font-semibold text-[var(--duties-text)]';
const RUNTIME_PATH_CLASS = 'mt-1 block font-mono text-[0.65rem] text-[var(--duties-tertiary)]';

const EMPTY_RUNTIME_FORM: RuntimeConfigFormState = {
  max_steps: '',
  streaming: true,
  retry_max_retries: '',
  compact_threshold_tokens: '',
  compact_keep_recent: '',
  tool_mode: 'json',
  output_mode: 'hybrid',
};

const Stat = ({ label, value, detail, onClick }: { label: string; value: string | number; detail?: string; onClick?: () => void }) => {
  const content = (
    <>
      <p className="font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">{label}</p>
      <p className="mt-1 font-mono text-lg font-semibold tracking-[-0.04em]">{value}</p>
      {detail && <p className="mt-1 text-xs text-[var(--duties-secondary)]">{detail}</p>}
    </>
  );
  const cls = `border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 ${onClick ? 'w-full -pointer text-left transition-colors hover:bg-[var(--duties-muted)]' : ''}`;
  if (onClick) {
    return <button aria-label={`查看${label}详情`} className={cls} onClick={onClick} type="button">{content}</button>;
  }
  return <div className={cls}>{content}</div>;
};

export const SystemSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const setSettingsTab = useViewStore(state => state.setSettingsTab);
  const [adminState, setAdminState] = useState<AdminState | null>(null);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [activeTasksOpen, setActiveTasksOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [runtimeRaw, setRuntimeRaw] = useState('');
  const [runtimeForm, setRuntimeForm] = useState<RuntimeConfigFormState>(EMPTY_RUNTIME_FORM);
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const [runtimeLoaded, setRuntimeLoaded] = useState(false);
  const [runtimeMessage, setRuntimeMessage] = useState('');

  const load = useCallback(async (showSpinner = true) => {
    if (!adminToken || !isAuthenticated) return;
    if (showSpinner) setLoading(true);
    setMessage('');
    try {
      const [state, healthState] = await Promise.all([getAdminState(adminToken), checkHealth()]);
      setAdminState(state);
      setHealth(healthState);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载系统状态失败');
    } finally {
      if (showSpinner) setLoading(false);
    }
  }, [adminToken, isAuthenticated]);

  const loadRuntimeCommon = useCallback(async (showSpinner = true) => {
    if (!adminToken || !isAuthenticated) return;
    if (showSpinner) setRuntimeLoading(true);
    setRuntimeMessage('');
    try {
      const raw = await getRuntimeRaw(adminToken);
      setRuntimeRaw(raw);
      setRuntimeForm(parseRuntimeConfig(raw));
      setRuntimeLoaded(true);
    } catch (error) {
      setRuntimeLoaded(false);
      setRuntimeMessage(error instanceof Error ? error.message : '加载运行参数失败');
    } finally {
      if (showSpinner) setRuntimeLoading(false);
    }
  }, [adminToken, isAuthenticated]);

  const updateRuntimeForm = (patch: Partial<RuntimeConfigFormState>) => {
    setRuntimeForm((current) => ({ ...current, ...patch }));
  };

  const handleSaveRuntimeCommon = async () => {
    if (!adminToken) return;
    if (!runtimeLoaded) {
      await loadRuntimeCommon();
      setRuntimeMessage('已加载最新运行参数。请确认后再次保存。');
      return;
    }
    setRuntimeLoading(true);
    setRuntimeMessage('');
    try {
      const nextRaw = serializeRuntimeConfig(runtimeRaw, runtimeForm);
      await updateRuntimeRaw(adminToken, nextRaw);
      setRuntimeRaw(nextRaw);
      setRuntimeForm(parseRuntimeConfig(nextRaw));
      setRuntimeMessage('已保存。重载配置后生效。');
    } catch (error) {
      setRuntimeMessage(error instanceof Error ? error.message : '保存运行参数失败');
    } finally {
      setRuntimeLoading(false);
    }
  };

  useEffect(() => {
    void load();
    void loadRuntimeCommon(false);
    if (!adminToken || !isAuthenticated) return undefined;
    const timer = window.setInterval(() => { void load(false); }, 15000);
    return () => window.clearInterval(timer);
  }, [adminToken, isAuthenticated, load, loadRuntimeCommon]);

  const engineInfo = useMemo(() => {
    const runtime = adminState?.engine_runtime || {};
    const workerId = typeof runtime.worker_id === 'string' ? runtime.worker_id : '';
    const workers = Array.isArray(runtime.workers) ? runtime.workers.filter((item): item is string => typeof item === 'string') : [];
    if (workerId) return workerId;
    if (workers.length > 0) return workers.join(', ');
    return '无工作进程';
  }, [adminState]);

  const handleReload = async () => {
    if (!adminToken) return;
    try {
      await reloadConfig(adminToken);
      setMessage('配置已重载');
      await load(false);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '配置重载失败');
    }
  };

  const handleRestart = async () => {
    if (!adminToken) return;
    if (!window.confirm('确认要重启引擎吗？这会中断所有运行中的任务。')) return;
    try {
      await restartEngine(adminToken);
      setMessage('已提交重启请求');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '重启失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="查看 Supervisor 运行状态，并执行配置重载或引擎重启。" title="系统" />
      {!isAuthenticated ? <AuthRequired /> : (
        <>
          <Card title="系统状态" description="数据来自管理员状态接口和健康检查接口，每 15 秒自动刷新。">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="会话数" value={adminState?.sessions ?? (loading ? '…' : '无数据')} detail="浏览、进入、删除" onClick={() => setSessionsOpen(true)} />
              <Stat label="待审批数" value={adminState?.approvals?.pending ?? adminState?.pending_approvals?.length ?? 0} detail="进入审批页" onClick={() => setSettingsTab('approvals')} />
              <Stat label="运行中任务数" value={countActiveTasks(adminState?.tasks)} detail="运行中、等待中、已挂起" onClick={() => setActiveTasksOpen(true)} />
              <Stat label="运行时间" value={formatUptime(health?.uptime_seconds)} />
            </div>
            <div className="mt-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 text-xs leading-5">
              <p><span className="text-[var(--duties-tertiary)]">Engine worker：</span><span className="font-mono">{engineInfo}</span></p>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button disabled={loading} onClick={() => load()}>{loading ? '刷新中...' : '刷新状态'}</Button>
            </div>
          </Card>

          <Card title="常用运行参数" description="写入 config/runtime.yaml。保存后需要重载配置。">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>最大步骤数</span>
                <span className={RUNTIME_PATH_CLASS}>engine.max_steps</span>
                <input className={`${RUNTIME_INPUT_CLASS} mt-2`} min="1" onChange={(event) => updateRuntimeForm({ max_steps: event.target.value })} placeholder="64" type="number" value={runtimeForm.max_steps} />
              </label>
              <label className="block border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
                <span className={RUNTIME_LABEL_CLASS}>流式输出</span>
                <span className={RUNTIME_PATH_CLASS}>engine.streaming</span>
                <span className="mt-3 flex items-center gap-2 text-sm text-[var(--duties-text)]">
                  <input checked={runtimeForm.streaming} onChange={(event) => updateRuntimeForm({ streaming: event.target.checked })} type="checkbox" />
                  启用
                </span>
              </label>
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>最大重试次数</span>
                <span className={RUNTIME_PATH_CLASS}>engine.retry.max_retries</span>
                <input className={`${RUNTIME_INPUT_CLASS} mt-2`} min="0" onChange={(event) => updateRuntimeForm({ retry_max_retries: event.target.value })} placeholder="3" type="number" value={runtimeForm.retry_max_retries} />
              </label>
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>压缩阈值 Tokens</span>
                <span className={RUNTIME_PATH_CLASS}>engine.compact.threshold_tokens</span>
                <input className={`${RUNTIME_INPUT_CLASS} mt-2`} min="0" onChange={(event) => updateRuntimeForm({ compact_threshold_tokens: event.target.value })} placeholder="256000" type="number" value={runtimeForm.compact_threshold_tokens} />
              </label>
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>压缩保留最近轮数</span>
                <span className={RUNTIME_PATH_CLASS}>engine.compact.keep_recent</span>
                <input className={`${RUNTIME_INPUT_CLASS} mt-2`} min="0" onChange={(event) => updateRuntimeForm({ compact_keep_recent: event.target.value })} placeholder="6" type="number" value={runtimeForm.compact_keep_recent} />
              </label>
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>工具格式模式</span>
                <span className={RUNTIME_PATH_CLASS}>engine.tool_mode</span>
                <select className={`${RUNTIME_INPUT_CLASS} mt-2`} onChange={(event) => updateRuntimeForm({ tool_mode: event.target.value as RuntimeToolMode })} value={runtimeForm.tool_mode}>
                  <option value="json">json</option>
                  <option value="native">native</option>
                  <option value="fake-native">fake-native</option>
                </select>
                <p className="mt-1 text-[0.65rem] leading-4 text-[var(--duties-secondary)]">控制工具调用格式，不是工具权限。</p>
              </label>
              <label className="block">
                <span className={RUNTIME_LABEL_CLASS}>输出模式</span>
                <span className={RUNTIME_PATH_CLASS}>engine.output_mode</span>
                <select className={`${RUNTIME_INPUT_CLASS} mt-2`} onChange={(event) => updateRuntimeForm({ output_mode: event.target.value as OutputMode })} value={runtimeForm.output_mode}>
                  <option value="hybrid">hybrid</option>
                  <option value="tool_only">tool_only</option>
                </select>
                <p className="mt-1 text-[0.65rem] leading-4 text-[var(--duties-secondary)]">hybrid: free prose = 隐式 finish；tool_only: 必须调用 finish。</p>
              </label>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button disabled={runtimeLoading} onClick={() => loadRuntimeCommon()}>{runtimeLoading ? '读取中...' : '重新读取'}</Button>
              <Button disabled={runtimeLoading || !runtimeLoaded} onClick={handleSaveRuntimeCommon} variant="primary">保存运行参数</Button>
            </div>
            <StatusText message={runtimeMessage} />
          </Card>

          <ActiveTasksModal open={activeTasksOpen} onClose={() => setActiveTasksOpen(false)} />
          <SessionBrowserModal
            open={sessionsOpen}
            onClose={() => setSessionsOpen(false)}
            pendingApprovals={adminState?.pending_approvals || []}
          />

          <Card title="运行控制" description="配置重载会重新读取配置；引擎重启需要二次确认。">
            <div className="flex flex-wrap gap-2">
              <Button onClick={handleReload} variant="primary">重载配置</Button>
              <Button onClick={handleRestart} variant="danger">重启引擎</Button>
            </div>
            <StatusText message={message} />
          </Card>
        </>
      )}
    </PageShell>
  );
};
