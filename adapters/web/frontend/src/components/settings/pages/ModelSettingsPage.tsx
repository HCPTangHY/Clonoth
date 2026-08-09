// [2026-06-03] Model & Provider settings — dynamic multi-provider editor.
import { useEffect, useState } from 'react';

import {
  type ProviderConfigPublic,
  type ProvidersResponse,
  deleteProvider,
  getProviders,
  reloadConfig,
  setActiveProvider,
  upsertProvider,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';

interface ProviderEditState {
  base_url: string;
  model: string;
  api_key: string;
}

const emptyEdit = (): ProviderEditState => ({ base_url: '', model: '', api_key: '' });

export const ModelSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();

  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  // per-provider edit states, keyed by provider name
  const [edits, setEdits] = useState<Record<string, ProviderEditState>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);

  // new provider: select from registered list
  const [newName, setNewName] = useState('');
  const [addingNew, setAddingNew] = useState(false);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    setMsg('');
    try {
      const resp = await getProviders(adminToken);
      setData(resp);
      // init edit states from loaded data
      const e: Record<string, ProviderEditState> = {};
      for (const [name, cfg] of Object.entries(resp.providers)) {
        e[name] = { base_url: cfg.base_url, model: cfg.model, api_key: '' };
      }
      setEdits(e);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

  const applyResponse = (resp: ProvidersResponse) => {
    setData(resp);
    const e: Record<string, ProviderEditState> = {};
    for (const [name, cfg] of Object.entries(resp.providers)) {
      e[name] = { base_url: cfg.base_url, model: cfg.model, api_key: '' };
    }
    setEdits(e);
  };

  const handleSave = async (name: string) => {
    if (!adminToken) return;
    const edit = edits[name];
    if (!edit) return;
    setSavingKey(name);
    setMsg('');
    try {
      const params: Record<string, string> = {};
      if (edit.base_url) params.base_url = edit.base_url;
      if (edit.model) params.model = edit.model;
      if (edit.api_key) params.api_key = edit.api_key;
      const resp = await upsertProvider(adminToken, name, params);
      applyResponse(resp);
      setMsg(`${name} 已保存`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSavingKey(null);
    }
  };

  const handleSetActive = async (name: string) => {
    if (!adminToken) return;
    setMsg('');
    try {
      const resp = await setActiveProvider(adminToken, name);
      applyResponse(resp);
      setMsg(`已切换到 ${name}`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '切换失败');
    }
  };

  const handleDelete = async (name: string) => {
    if (!adminToken) return;
    if (!window.confirm(`确定要删除渠道 "${name}"？`)) return;
    setMsg('');
    try {
      const resp = await deleteProvider(adminToken, name);
      applyResponse(resp);
      setMsg(`${name} 已删除`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleAddProvider = async () => {
    if (!adminToken || !newName) return;
    const name = newName;
    setAddingNew(true);
    setMsg('');
    try {
      const resp = await upsertProvider(adminToken, name, {});
      applyResponse(resp);
      setNewName('');
      setMsg(`渠道 ${name} 已添加`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '添加失败');
    } finally {
      setAddingNew(false);
    }
  };

  const handleReload = async () => {
    if (!adminToken) return;
    setMsg('');
    try {
      await reloadConfig(adminToken);
      await load();
      setMsg('✅ 配置已重载');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '重载失败');
    }
  };

  const updateEdit = (name: string, field: keyof ProviderEditState, value: string) => {
    setEdits(prev => ({ ...prev, [name]: { ...prev[name], [field]: value } }));
  };

  const providerNames = data ? Object.keys(data.providers) : [];

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型与渠道</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">
          管理 LLM Provider 渠道配置。当前活跃渠道会被标记。
        </p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          修改模型设置前需要完成管理员令牌认证。
        </div>
      ) : loading && !data ? (
        <div className="p-4 text-sm text-[var(--duties-secondary)]">加载中...</div>
      ) : (
        <>
          {/* Status bar */}
          {msg && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-2 text-xs text-[var(--duties-tertiary)]">
              {msg}
            </div>
          )}

          {/* Provider cards */}
          {providerNames.map(name => {
            const cfg = data!.providers[name];
            const edit = edits[name] || emptyEdit();
            const isActive = data!.active_provider === name;
            const isSaving = savingKey === name;

            return (
              <div
                key={name}
                className={`border bg-[var(--duties-panel)] p-4 ${
                  isActive
                    ? 'border-[var(--duties-text)]'
                    : 'border-[var(--duties-border)]'
                }`}
              >
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <p className="font-mono text-sm font-semibold text-[var(--duties-text)]">{name}</p>
                    {isActive && (
                      <span className="rounded bg-[var(--duties-text)] px-1.5 py-0.5 text-[0.6rem] font-bold text-[var(--duties-bg)]">
                        ACTIVE
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {!isActive && (
                      <button
                        className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
                        onClick={() => handleSetActive(name)}
                      >
                        设为活跃
                      </button>
                    )}
                    {!isActive && (
                      <button
                        className="text-xs text-red-400 underline hover:text-red-300"
                        onClick={() => handleDelete(name)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>

                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'model', e.target.value)}
                  value={edit.model}
                />
                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'base_url', e.target.value)}
                  value={edit.base_url}
                />
                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">
                  API 密钥 {cfg?.api_key_present ? `（已设置: ${cfg.api_key_redacted}）` : '（未设置）'}
                </label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'api_key', e.target.value)}
                  placeholder="留空以保留当前值"
                  type="password"
                  value={edit.api_key}
                />
                <Button disabled={isSaving} onClick={() => handleSave(name)} variant="primary">
                  {isSaving ? '保存中...' : '保存'}
                </Button>
              </div>
            );
          })}

          {/* Add new provider from registered list */}
          {(() => {
            const available = (data?.registered || []).filter(r => !providerNames.includes(r));
            if (available.length === 0) return null;
            return (
              <div className="border border-dashed border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
                <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">添加渠道</p>
                <div className="flex items-center gap-3">
                  <select
                    className="flex-1 border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                    onChange={e => setNewName(e.target.value)}
                    value={newName}
                  >
                    <option value="">选择渠道类型...</option>
                    {available.map(name => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                  <Button disabled={addingNew || !newName} onClick={handleAddProvider} variant="primary">
                    {addingNew ? '添加中...' : '添加'}
                  </Button>
                </div>
              </div>
            );
          })()}

          {/* Fallback display */}
          {data && data.fallbacks.length > 0 && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
              <p className="mb-2 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">Fallback 链</p>
              <div className="flex flex-wrap gap-2">
                {data.fallbacks.map((fb, i) => (
                  <span
                    key={i}
                    className="border border-[var(--duties-border)] px-2 py-1 font-mono text-xs text-[var(--duties-secondary)]"
                  >
                    {i + 1}. {fb.provider || JSON.stringify(fb)}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Reload */}
          <div className="flex items-center gap-3">
            <Button onClick={handleReload}>🔄 重载配置</Button>
            <p className="text-xs text-[var(--duties-secondary)]">重新读取 config.yaml，不中断服务</p>
          </div>
        </>
      )}
    </section>
  );
};
