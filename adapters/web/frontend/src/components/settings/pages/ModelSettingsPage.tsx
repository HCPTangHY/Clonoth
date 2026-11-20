// [2026-06-03] Model & Provider settings — dynamic multi-provider & fallback chain editor.
import { useEffect, useRef, useState } from 'react';

import {
  type FallbackEntryPublic,
  type ProvidersResponse,
  deleteProvider,
  getProviders,
  reloadConfig,
  setActiveProvider,
  updateFallbacks,
  upsertProvider,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';

interface ProviderEditState {
  base_url: string;
  model: string;
  api_key: string;
}

interface FallbackEditItem {
  key: string;
  provider: string;
  model: string;
  sourceIndex?: number;
  hasExtraFields?: boolean;
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

  // fallback chain editor state
  const fbSeqRef = useRef(0);
  const [fallbackEdits, setFallbackEdits] = useState<FallbackEditItem[]>([]);
  const [savingFallbacks, setSavingFallbacks] = useState(false);

  const buildFallbackEdits = (fallbacks: FallbackEntryPublic[] | undefined): FallbackEditItem[] =>
    (fallbacks || []).map((fb, idx) => {
      const extraKeys = Object.keys(fb || {}).filter(k => k !== 'provider' && k !== 'model');
      return {
        key: `fb-${++fbSeqRef.current}`,
        provider: String(fb.provider || ''),
        model: String(fb.model || ''),
        sourceIndex: idx,
        hasExtraFields: extraKeys.length > 0,
      };
    });

  const applyResponse = (resp: ProvidersResponse) => {
    setData(prev => ({
      ...resp,
      registered: resp.registered ?? prev?.registered ?? [],
    }));
    const e: Record<string, ProviderEditState> = {};
    for (const [name, cfg] of Object.entries(resp.providers)) {
      e[name] = { base_url: cfg.base_url, model: cfg.model, api_key: '' };
    }
    setEdits(e);
    setFallbackEdits(buildFallbackEdits(resp.fallbacks));
  };

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    setMsg('');
    try {
      const resp = await getProviders(adminToken);
      applyResponse(resp);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

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
      setMsg('配置已重载');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '重载失败');
    }
  };

  const updateEdit = (name: string, field: keyof ProviderEditState, value: string) => {
    setEdits(prev => ({ ...prev, [name]: { ...prev[name], [field]: value } }));
  };

  const providerNames = data ? Object.keys(data.providers) : [];

  const fallbacksDirty = (() => {
    if (!data) return false;
    const orig = data.fallbacks || [];
    if (orig.length !== fallbackEdits.length) return true;
    return fallbackEdits.some((item, idx) => {
      const o = orig[idx];
      if (!o) return true;
      return (
        item.provider !== String(o.provider || '')
        || item.model.trim() !== String(o.model || '').trim()
        || item.sourceIndex !== idx
      );
    });
  })();

  const handleAddFallback = () => {
    const defaultProvider =
      providerNames.find(name => name !== data?.active_provider)
      || providerNames[0]
      || data?.registered?.[0]
      || '';
    if (!defaultProvider) return;
    setFallbackEdits(prev => [
      ...prev,
      {
        key: `fb-${++fbSeqRef.current}`,
        provider: defaultProvider,
        model: '',
      },
    ]);
  };

  const handleUpdateFallback = (index: number, patch: Partial<Pick<FallbackEditItem, 'provider' | 'model'>>) => {
    setFallbackEdits(prev =>
      prev.map((item, idx) => {
        if (idx !== index) return item;
        const nextProvider = patch.provider !== undefined ? patch.provider : item.provider;
        const providerChanged = nextProvider !== item.provider;
        return {
          ...item,
          ...patch,
          sourceIndex: providerChanged ? undefined : item.sourceIndex,
        };
      }),
    );
  };

  const handleMoveFallback = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    setFallbackEdits(prev => {
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      const [moved] = next.splice(index, 1);
      next.splice(target, 0, moved);
      return next;
    });
  };

  const handleRemoveFallback = (index: number) => {
    setFallbackEdits(prev => prev.filter((_, idx) => idx !== index));
  };

  const handleResetFallbacks = () => {
    if (!data) return;
    setFallbackEdits(buildFallbackEdits(data.fallbacks));
  };

  const handleSaveFallbacks = async () => {
    if (!adminToken) return;
    setSavingFallbacks(true);
    setMsg('');
    try {
      const payload = fallbackEdits.map(item => {
        const trimmedModel = item.model.trim();
        const keepSource = item.hasExtraFields && item.sourceIndex !== undefined;
        return {
          provider: item.provider.trim(),
          ...(trimmedModel ? { model: trimmedModel } : keepSource ? { model: '' } : {}),
          ...(keepSource ? { _source_index: item.sourceIndex } : {}),
        };
      });
      const resp = await updateFallbacks(adminToken, payload);
      applyResponse(resp);
      setMsg('Fallback 链已保存');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存 Fallback 链失败');
    } finally {
      setSavingFallbacks(false);
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型与渠道</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">
          管理 LLM Provider 渠道配置与自动回退顺序。当前活跃渠道会被标记。
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
                        type="button"
                      >
                        设为活跃
                      </button>
                    )}
                    {!isActive && (
                      <button
                        className="text-xs text-red-400 underline hover:text-red-300"
                        onClick={() => handleDelete(name)}
                        type="button"
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

          {/* Fallback chain editor */}
          {data && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
              <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">Fallback 链</p>
                  <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
                    主请求触发限流或上游故障时按顺序依次尝试。模型留空则继承对应渠道默认模型，也可指定独立回退模型。
                  </p>
                </div>
                {fallbacksDirty && (
                  <span className="border border-[var(--duties-border)] px-2 py-0.5 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
                    未保存修改
                  </span>
                )}
              </div>

              {fallbackEdits.length === 0 ? (
                <div className="mb-3 border border-dashed border-[var(--duties-border)] px-3 py-3 text-xs text-[var(--duties-secondary)]">
                  当前未配置 Fallback 链。点击下方“添加回退项”可串联备用渠道或同渠道的备用模型。
                </div>
              ) : (
                <div className="mb-3 flex flex-col gap-2.5">
                  {fallbackEdits.map((item, idx) => {
                    const inheritedModel = data.providers[item.provider]?.model || '';
                    const selectableProviders = providerNames.includes(item.provider) || !item.provider
                      ? providerNames
                      : [item.provider, ...providerNames];
                    const isUnconfigured = Boolean(item.provider) && !providerNames.includes(item.provider);

                    return (
                      <div
                        key={item.key}
                        className="flex flex-col gap-2 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 sm:flex-row sm:items-end"
                      >
                        <div className="flex items-center justify-between sm:pb-2">
                          <span className="w-6 font-mono text-xs font-semibold text-[var(--duties-tertiary)]">
                            {idx + 1}.
                          </span>
                          <div className="flex items-center gap-2 sm:hidden">
                            <button
                              className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:no-underline disabled:opacity-40"
                              disabled={idx === 0}
                              onClick={() => handleMoveFallback(idx, -1)}
                              type="button"
                            >
                              上移
                            </button>
                            <button
                              className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:no-underline disabled:opacity-40"
                              disabled={idx === fallbackEdits.length - 1}
                              onClick={() => handleMoveFallback(idx, 1)}
                              type="button"
                            >
                              下移
                            </button>
                            <button
                              className="text-xs text-red-400 underline hover:text-red-300"
                              onClick={() => handleRemoveFallback(idx)}
                              type="button"
                            >
                              删除
                            </button>
                          </div>
                        </div>

                        <div className="sm:w-48">
                          <label className="mb-1 block text-[0.65rem] text-[var(--duties-tertiary)]">
                            回退渠道 {isUnconfigured ? '（未配置顶层块）' : ''}
                          </label>
                          <select
                            className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2.5 py-1.5 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                            onChange={e => handleUpdateFallback(idx, { provider: e.target.value })}
                            value={item.provider}
                          >
                            {selectableProviders.map(pName => (
                              <option key={pName} value={pName}>
                                {pName}
                              </option>
                            ))}
                          </select>
                        </div>

                        <div className="min-w-0 flex-1">
                          <label className="mb-1 block text-[0.65rem] text-[var(--duties-tertiary)]">
                            覆盖模型（可选）
                          </label>
                          <input
                            className="w-full border border-[var(--duties-border)] bg-transparent px-2.5 py-1.5 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                            onChange={e => handleUpdateFallback(idx, { model: e.target.value })}
                            placeholder={
                              inheritedModel
                                ? `留空继承渠道默认 (${inheritedModel})`
                                : '留空继承渠道默认模型'
                            }
                            value={item.model}
                          />
                        </div>

                        <div className="hidden items-center gap-2 pb-1.5 sm:flex">
                          <button
                            className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:no-underline disabled:opacity-40"
                            disabled={idx === 0}
                            onClick={() => handleMoveFallback(idx, -1)}
                            type="button"
                          >
                            上移
                          </button>
                          <button
                            className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:no-underline disabled:opacity-40"
                            disabled={idx === fallbackEdits.length - 1}
                            onClick={() => handleMoveFallback(idx, 1)}
                            type="button"
                          >
                            下移
                          </button>
                          <button
                            className="text-xs text-red-400 underline hover:text-red-300"
                            onClick={() => handleRemoveFallback(idx)}
                            type="button"
                          >
                            删除
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    disabled={providerNames.length === 0 && (data.registered || []).length === 0}
                    onClick={handleAddFallback}
                  >
                    + 添加回退项
                  </Button>
                  {fallbacksDirty && (
                    <Button disabled={savingFallbacks} onClick={handleResetFallbacks}>
                      撤销修改
                    </Button>
                  )}
                </div>
                <Button
                  disabled={savingFallbacks || !fallbacksDirty}
                  onClick={handleSaveFallbacks}
                  variant="primary"
                >
                  {savingFallbacks ? '保存中...' : '保存 Fallback 链'}
                </Button>
              </div>
            </div>
          )}

          {/* Reload */}
          <div className="flex items-center gap-3">
            <Button onClick={handleReload}>重载配置</Button>
            <p className="text-xs text-[var(--duties-secondary)]">重新读取 config.yaml，不中断服务</p>
          </div>
        </>
      )}
    </section>
  );
};
