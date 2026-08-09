// [2026-06-01] Entry-node settings page split from the legacy SettingsPanel.
// Why: node selection is a separate settings domain from authentication and model
// configuration. How: read and write entryNodeId through settingsStore while loading
// admin-visible nodes when needed. Purpose: future node settings can expand here
// without touching the application shell.
import { useEffect, useState } from 'react';

import { getNodes } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import type { NodeDef } from '../../../types';

export const NodeSettingsPage = () => {
  const {
    adminToken,
    availableNodes,
    entryNodeId,
    isAuthenticated,
    setAvailableNodes,
    setEntryNodeId,
  } = useSettingsStore();
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    // [2026-06-01] Load node choices inside the Node page when the cache is empty.
    // Why: the page may be opened directly from the Header before another settings
    // page has loaded admin data. How: fetch nodes with the saved admin token and
    // keep only user-selectable AI nodes. Purpose: the tab is independently useful.
    if (!adminToken || !isAuthenticated || availableNodes.length > 0) return;
    getNodes(adminToken)
      .then(nodes => {
        setLoadError('');
        setAvailableNodes(nodes.filter((n: NodeDef) => n.type === 'ai' && !n.id.startsWith('system.')));
      })
      .catch(err => setLoadError(err instanceof Error ? err.message : '加载节点失败'));
  }, [adminToken, availableNodes.length, isAuthenticated, setAvailableNodes]);

  return (
    <section className="mx-auto flex h-full max-w-3xl flex-col overflow-y-auto p-4 sm:p-6">
      <div className="mb-5">
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">入口节点</h2>
        <p className="mt-2 max-w-xl text-sm text-[var(--duties-secondary)]">选择新聊天消息未指定覆盖节点时使用的默认节点。</p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          修改节点设置前需要完成管理员令牌认证。
        </div>
      ) : (
        <div className="border border-[var(--duties-border)] p-4">
          <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">入口节点</p>
          <select
            className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={e => setEntryNodeId(e.target.value)}
            value={entryNodeId}
          >
            {availableNodes.length === 0 && <option value={entryNodeId}>{entryNodeId || '暂无可用入口节点'}</option>}
            {availableNodes.map(n => (
              <option key={n.id} value={n.id}>{n.name || n.id}{n.description ? ` — ${n.description}` : ''}</option>
            ))}
          </select>
          {loadError && <p className="mt-2 text-xs text-red-500">{loadError}</p>}
          <p className="mt-3 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">当前值：{entryNodeId || '（未设置）'}</p>
        </div>
      )}
    </section>
  );
};
