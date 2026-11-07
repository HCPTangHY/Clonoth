// [2026-06-01] General settings page split from the legacy SettingsPanel.
// Why: the settings view now renders one focused page at a time. How: this page
// owns only connection status and admin-token authentication UI. Purpose: changing
// authentication no longer requires editing App.tsx or unrelated settings pages.
import { useEffect, useState } from 'react';

import { checkAdminAuth, checkHealth, getModelConfig, getNodes, pluginApiRequest } from '../../../api/supervisorClient';
import { useChatStore } from '../../../store/chatStore';
import { useSettingsStore } from '../../../store/settingsStore';
import type { NodeDef } from '../../../types';
import { Button } from '../../common';

export const GeneralSettingsPage = () => {
  const {
    adminToken,
    isAuthenticated,
    isConnected,
    setAdminToken,
    setAuthenticated,
    setAvailableNodes,
    setConnected,
    setModelConfig,
  } = useSettingsStore();
  const [tokenInput, setTokenInput] = useState(adminToken || '');
  const [authError, setAuthError] = useState('');
  const [authLoading, setAuthLoading] = useState(false);

  const loadAdminData = async (token: string) => {
    // [2026-06-01] Keep successful login side effects together.
    // Why: nodes and global model config are admin-protected data used by other
    // settings pages. How: refresh both caches after a token is verified. Purpose:
    // navigating to Node or Model settings does not require another login action.
    try {
      const nodes = await getNodes(token);
      setAvailableNodes(nodes.filter((n: NodeDef) => n.type === 'ai' && !n.id.startsWith('system.')));
    } catch { /* keep existing cached nodes if refresh fails */ }
    try {
      const cfg = await getModelConfig(token);
      setModelConfig(cfg);
    } catch { /* keep existing cached model config if refresh fails */ }
  };

  useEffect(() => {
    // [2026-06-01] Settings refreshes health once when opened.
    // Why: App.tsx already performs periodic health checks, but the General page
    // should not show a stale status immediately after navigation. How: call the
    // same health endpoint on mount and update the shared connection flag. Purpose:
    // the status card reflects the current Supervisor state.
    checkHealth()
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
  }, [setConnected]);

  useEffect(() => {
    // [2026-06-01] Reuse a saved admin token when the settings page is opened.
    // Why: users can land on settings after LoginPage or after a page reload. How:
    // verify the stored token only when authentication is not already confirmed.
    // Purpose: the settings page remains self-contained without owning the login gate.
    if (!adminToken || isAuthenticated) return;
    checkAdminAuth(adminToken).then(async (ok) => {
      setAuthenticated(ok);
      if (ok) await loadAdminData(adminToken);
    });
  }, [adminToken, isAuthenticated, setAuthenticated]);

  const handleAuth = async () => {
    setAuthError('');
    const token = tokenInput.trim();
    if (!token) { setAuthError('请输入令牌'); return; }
    setAuthLoading(true);
    const ok = await checkAdminAuth(token);
    setAuthLoading(false);
    if (ok) {
      setAdminToken(token);
      setAuthenticated(true);
      await loadAdminData(token);
    } else {
      setAuthError('令牌无效');
    }
  };

  const handleLogout = async () => {
    // [2026-06-01] Logout clears admin-only caches with the token.
    // Why: node and model lists are protected data and should not remain visible
    // after authentication is removed. How: reset token, auth flag, nodes, and model
    // config together. Purpose: later settings pages correctly show the login need.
    // [2026-09-12] 退出时必须同时回收面板短期 Cookie。Why: Cookie 只在打开面板期间
    // 需要，退出后若残留，任何访问者仍能加载插件静态页面。How: 先在本地清掉 token
    // （已挂载面板的续期 effect 立即中止，不再续期），再用事先捕获的 Bearer 凭据
    // 调服务端撤销接口。Purpose: 本地续期在退出瞬间停止，服务端 Cookie 也被回收；
    // 撤销失败只降级提示，不阻断本地退出。插件 API 的 boot token 用途不变（见
    // PluginPanel）。
    // [2026-10-02] 登出必须整体复位 chat store，而不是只停事件泵。
    // Why: stopEventPump() 只断开 socket 与定时器，chatStore 的 startupLoaded 仍是
    // true，登录后 MainApp 重新挂载时 loadStartup() 被直接短路 —— 会话列表与
    // 私有缓存（messages/childSessionMessages/childNodes/contextUsage）都停留在
    // 上一个账号的状态，且事件流再也不会启动（页面看起来“登录了但完全不刷新”）。
    // How: 复用 chatStore.resetState()：它内部 stopEventPump + resetEventPumpState、
    // 清空会话/子会话/工具/审批等私有缓存，并把 startupLoaded 复位为 false。
    // Purpose: 重新登录后 MainApp 的 loadStartup() 会重新拉会话列表并通过
    // startEventPump() 建立一条新的 socket + children 轮询（旧定时器已在
    // resetState 里清掉，不会重复创建）。
    const token = adminToken;
    useChatStore.getState().resetState();
    setAdminToken(null);
    setAuthenticated(false);
    setAvailableNodes([]);
    setModelConfig(null);
    setTokenInput('');
    if (token) {
      try {
        await pluginApiRequest('/admin/auth/panel-session', {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` },
        });
      } catch {
        console.warn('已退出本地登录，但无法确认面板 Cookie 已撤销。');
      }
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-3xl flex-col overflow-y-auto p-4 sm:p-6">
      <div className="mb-5">
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">通用</h2>
        <p className="mt-2 max-w-xl text-sm text-[var(--duties-secondary)]">管理调度器连接和管理员访问。</p>
      </div>

      {window.location.protocol === 'http:' && (
        <p className="mb-4 text-sm text-[var(--duties-secondary)]" role="note">
          当前使用 HTTP，连接未加密。可用于本地部署；通过不可信网络访问时请使用 HTTPS。
        </p>
      )}
      <div className="mb-4 border border-[var(--duties-border)] p-4">
        <p className="mb-2 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">连接</p>
        <div className="flex items-center gap-2 text-sm">
          <span className={`inline-block h-2 w-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          <span>{isConnected ? '调度器已连接' : '已断开'}</span>
        </div>
      </div>

      <div className="border border-[var(--duties-border)] p-4">
        <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">管理员令牌</p>
        {isAuthenticated ? (
          <div>
            <div className="mb-3 flex items-center gap-2 text-sm">
              <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
              <span>已认证</span>
            </div>
            <Button onClick={handleLogout} variant="ghost">退出登录</Button>
          </div>
        ) : (
          <div className="max-w-md">
            <input
              className="mb-2 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={e => setTokenInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleAuth()}
              placeholder="输入管理员令牌..."
              type="password"
              value={tokenInput}
            />
            {authError && <p className="mb-2 text-xs text-red-500">{authError}</p>}
            <Button disabled={authLoading} onClick={handleAuth} variant="primary">
              {authLoading ? '验证中...' : '登录'}
            </Button>
          </div>
        )}
      </div>
    </section>
  );
};
