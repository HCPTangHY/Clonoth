// [2026-05-16] Login gate — admin token or JWT credentials.
// [2026-06-15] Tri-mode: setup wizard → JWT login → token fallback.
import { useEffect, useState } from 'react';

import {
  checkAdminAuth, getAppConfig, getAuthStatus, getNodes, getProviders,
  loginWithCredentials, setupAuth, type AuthStatus,
} from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Button } from '../common';

type LoginMode = 'loading' | 'setup' | 'jwt' | 'token';

export const LoginPage = () => {
  const {
    adminToken, setAdminToken, setAuthenticated, setAvailableNodes,
    setEntryNodeId, setNeedsSetup, setGlobalConfig,
  } = useSettingsStore();

  const [mode, setMode] = useState<LoginMode>('loading');
  const [jwtAvailable, setJwtAvailable] = useState(false);

  // Shared inputs
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [password2, setPassword2] = useState('');
  const [tokenInput, setTokenInput] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [autoVerifying, setAutoVerifying] = useState(!!adminToken);

  // ── Post-login data loading ──
  const loadNodes = async (token: string) => {
    try {
      const nodes = await getNodes(token);
      const aiNodes = nodes.filter((n: any) => n.type === 'ai' && !n.id.startsWith('system.'));
      setAvailableNodes(aiNodes);
      const savedNode = localStorage.getItem('clonoth_entry_node') || '';
      const savedNodeValid = aiNodes.some((n: any) => n.id === savedNode);
      if ((!savedNode || !savedNodeValid) && aiNodes.length > 0) {
        setEntryNodeId(aiNodes[0].id);
      }
    } catch { /* ignore */ }
  };

  const checkSetupNeeded = async (token: string) => {
    try {
      const config = await getAppConfig();
      setGlobalConfig(
        config.openai?.model || '',
        config.openai?.base_url || '',
        config.provider_models || {},
      );
      const providers = await getProviders(token);
      const anyKeyPresent = Object.values(providers.providers).some(p => p.api_key_present);
      if (!anyKeyPresent) {
        setNeedsSetup(true);
      }
    } catch { /* ignore */ }
  };

  const completeLogin = async (token: string) => {
    setAdminToken(token);
    setAuthenticated(true);
    await loadNodes(token);
    await checkSetupNeeded(token);
  };

  // ── Check auth status + auto-verify on mount ──
  useEffect(() => {
    const init = async () => {
      // 1. Check backend auth mode
      const status: AuthStatus = await getAuthStatus();
      setJwtAvailable(status.jwt_available);

      // 2. Try to auto-verify saved token
      const saved = adminToken;
      if (saved) {
        const ok = await checkAdminAuth(saved);
        if (ok) {
          await completeLogin(saved);
          setAutoVerifying(false);
          return;
        }
        // Saved token invalid — clear it
        setAdminToken(null);
      }

      // 3. Decide login mode
      if (status.mode === 'setup') setMode('setup');
      else if (status.mode === 'jwt') setMode('jwt');
      else setMode('token');

      setAutoVerifying(false);
    };
    init();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Handlers ──
  const handleSetup = async () => {
    setError('');
    if (!username.trim()) { setError('请输入用户名'); return; }
    if (!password) { setError('请输入密码'); return; }
    if (password !== password2) { setError('两次密码不一致'); return; }
    if (password.length < 6) { setError('密码至少6位'); return; }
    setLoading(true);
    const result = await setupAuth(username.trim(), password);
    setLoading(false);
    if (result.ok && result.token) {
      await completeLogin(result.token);
    } else {
      setError(result.error || '设置失败');
    }
  };

  const handleJwtLogin = async () => {
    setError('');
    if (!username.trim() || !password) { setError('请输入用户名和密码'); return; }
    setLoading(true);
    const result = await loginWithCredentials(username.trim(), password);
    setLoading(false);
    if (result.ok && result.token) {
      await completeLogin(result.token);
    } else {
      setError(result.error || '登录失败');
    }
  };

  const handleTokenLogin = async () => {
    setError('');
    const token = tokenInput.trim();
    if (!token) { setError('请输入令牌'); return; }
    setLoading(true);
    const ok = await checkAdminAuth(token);
    setLoading(false);
    if (ok) {
      await completeLogin(token);
    } else {
      setError('令牌无效');
    }
  };

  // ── Render ──
  if (autoVerifying) {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--duties-bg)] text-[var(--duties-text)]">
        <p className="font-mono text-sm text-[var(--duties-tertiary)]">正在验证会话…</p>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center bg-[var(--duties-bg)] text-[var(--duties-text)]">
      <div className="w-full max-w-80 px-4">
        <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-6">
          {/* Logo header */}
          <div className="flex items-center gap-3">
            <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-11 w-11 rounded-lg" />
            <div>
              <h1 className="font-mono text-xl font-semibold tracking-[-0.04em]">Clonoth</h1>
              <p className="text-[0.6rem] text-[var(--duties-tertiary)]">调度器网页界面</p>
            </div>
          </div>

          {error && <p className="mt-3 text-xs text-red-500">{error}</p>}

          {/* Setup mode */}
          {mode === 'setup' && (
            <div className="mt-4 space-y-2">
              <p className="text-xs text-amber-400 mb-2">首次使用，请创建管理账号</p>
              <input
                autoFocus
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setUsername(e.target.value)}
                placeholder="用户名"
                type="text"
                value={username}
              />
              <input
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setPassword(e.target.value)}
                placeholder="密码"
                type="password"
                value={password}
              />
              <input
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setPassword2(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleSetup()}
                placeholder="确认密码"
                type="password"
                value={password2}
              />
              <Button className="w-full" disabled={loading} onClick={handleSetup} variant="primary">
                {loading ? '创建中…' : '创建并进入'}
              </Button>
              <p className="text-[0.6rem] text-[var(--duties-tertiary)]">
                账号信息存储在 data/web_auth.json，安全独立于 API Token
              </p>
            </div>
          )}

          {/* JWT login mode */}
          {mode === 'jwt' && (
            <div className="mt-4 space-y-2">
              <p className="text-xs text-[var(--duties-secondary)] mb-1">请登录管理账号</p>
              <input
                autoFocus
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setUsername(e.target.value)}
                placeholder="用户名"
                type="text"
                value={username}
              />
              <input
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setPassword(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleJwtLogin()}
                placeholder="密码"
                type="password"
                value={password}
              />
              <Button className="w-full" disabled={loading} onClick={handleJwtLogin} variant="primary">
                {loading ? '正在验证…' : '登录'}
              </Button>
              <button
                className="w-full text-center text-[0.65rem] text-[var(--duties-tertiary)] hover:text-[var(--duties-text)] transition-colors mt-1"
                onClick={() => { setMode('token'); setError(''); }}
                type="button"
              >
                Token 登录 →
              </button>
            </div>
          )}

          {/* Token fallback mode */}
          {mode === 'token' && (
            <div className="mt-4 space-y-2">
              <p className="text-xs text-[var(--duties-secondary)] mb-1">请输入管理员令牌以继续</p>
              <input
                autoFocus
                className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                onChange={e => setTokenInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleTokenLogin()}
                placeholder="管理员令牌"
                type="password"
                value={tokenInput}
              />
              <Button className="w-full" disabled={loading} onClick={handleTokenLogin} variant="primary">
                {loading ? '正在验证…' : '登录'}
              </Button>
              {jwtAvailable && (
                <button
                  className="w-full text-center text-[0.65rem] text-[var(--duties-tertiary)] hover:text-[var(--duties-text)] transition-colors mt-1"
                  onClick={() => { setMode('jwt'); setError(''); }}
                  type="button"
                >
                  ← 账号登录
                </button>
              )}
            </div>
          )}

          {/* Loading state */}
          {mode === 'loading' && (
            <p className="mt-4 text-sm text-[var(--duties-tertiary)] text-center">正在检查…</p>
          )}
        </div>
        <p className="mt-3 text-center font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
          调度器网页界面
        </p>
      </div>
    </div>
  );
};
