// [2026-06-12] First-deployment setup wizard.
// Shown after login when no provider has a valid API key.
// Guides the user through configuring their first LLM provider.
import { useState } from 'react';

import { getAppConfig, setActiveProvider, upsertProvider } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Button } from '../common';

type Step = 'provider' | 'config' | 'done';

const PRESETS: Record<string, { label: string; base_url: string; model: string; key_hint: string }> = {
  openai: {
    label: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    model: 'gpt-4o',
    key_hint: 'sk-...',
  },
  anthropic: {
    label: 'Anthropic (via OpenAI-compatible proxy)',
    base_url: '',
    model: 'claude-sonnet-4-20250514',
    key_hint: 'sk-ant-...',
  },
  deepseek: {
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    key_hint: 'sk-...',
  },
  custom: {
    label: '自定义 OpenAI 兼容端点',
    base_url: '',
    model: '',
    key_hint: '',
  },
};

export const SetupWizard = () => {
  const { adminToken, setGlobalConfig } = useSettingsStore();
  const [step, setStep] = useState<Step>('provider');
  const [providerKey, setProviderKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState('');
  const [providerName, setProviderName] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSelectProvider = (key: string) => {
    const preset = PRESETS[key];
    setProviderKey(key);
    setBaseUrl(preset.base_url);
    setModel(preset.model);
    setProviderName(key === 'custom' ? '' : key);
    setApiKey('');
    setError('');
    setStep('config');
  };

  const handleSave = async () => {
    if (!adminToken) return;
    const name = providerName.trim() || providerKey;
    if (!name) { setError('请输入渠道名称'); return; }
    if (!apiKey.trim()) { setError('请输入 API Key'); return; }

    setSaving(true);
    setError('');
    try {
      await upsertProvider(adminToken, name, {
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        model: model.trim(),
      });
      await setActiveProvider(adminToken, name);
      // Refresh global config so the app picks up the new provider
      const config = await getAppConfig();
      setGlobalConfig(
        config.openai?.model || '',
        config.openai?.base_url || '',
        config.provider_models || {},
      );
      setStep('done');
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleFinish = () => {
    // Update needsSetup to false so the main app renders
    useSettingsStore.getState().setNeedsSetup(false);
  };

  return (
    <div className="flex h-screen items-center justify-center bg-[var(--duties-bg)] text-[var(--duties-text)]">
      <div className="w-full max-w-lg px-4">
        <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-6">
          {/* Header */}
          <div className="flex items-center gap-3 mb-4">
            <img
              src={`${import.meta.env.BASE_URL}logo-sm.jpg`}
              alt="Clonoth"
              className="h-11 w-11 rounded-lg"
            />
            <div>
              <h1 className="font-mono text-xl font-semibold tracking-[-0.04em]">初始设置</h1>
              <p className="text-[0.6rem] text-[var(--duties-tertiary)]">
                配置你的第一个 LLM 渠道
              </p>
            </div>
          </div>

          {step === 'provider' && (
            <div className="space-y-2">
              <p className="text-xs text-[var(--duties-secondary)] mb-3">
                选择一个 LLM 提供商开始。所有渠道使用 OpenAI 兼容 API 格式。
              </p>
              {Object.entries(PRESETS).map(([key, preset]) => (
                <button
                  key={key}
                  className="w-full border border-[var(--duties-border)] bg-transparent px-4 py-3 text-left font-mono text-sm text-[var(--duties-text)] transition-colors hover:bg-[var(--duties-hover)]"
                  onClick={() => handleSelectProvider(key)}
                  type="button"
                >
                  <span className="font-semibold">{preset.label}</span>
                  {preset.base_url && (
                    <span className="block text-[0.65rem] text-[var(--duties-tertiary)] mt-0.5">
                      {preset.base_url}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}

          {step === 'config' && (
            <div className="space-y-3">
              <button
                className="text-[0.65rem] text-[var(--duties-tertiary)] hover:text-[var(--duties-text)] transition-colors"
                onClick={() => setStep('provider')}
                type="button"
              >
                ← 返回选择
              </button>

              {providerKey === 'custom' && (
                <div>
                  <label className="block text-[0.65rem] text-[var(--duties-tertiary)] mb-1">
                    渠道名称
                  </label>
                  <input
                    className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                    onChange={(e) => setProviderName(e.target.value)}
                    placeholder="my-provider"
                    value={providerName}
                  />
                </div>
              )}

              <div>
                <label className="block text-[0.65rem] text-[var(--duties-tertiary)] mb-1">
                  Base URL
                </label>
                <input
                  className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                  value={baseUrl}
                />
              </div>

              <div>
                <label className="block text-[0.65rem] text-[var(--duties-tertiary)] mb-1">
                  API Key
                </label>
                <input
                  className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={PRESETS[providerKey]?.key_hint || 'API Key'}
                  type="password"
                  value={apiKey}
                />
              </div>

              <div>
                <label className="block text-[0.65rem] text-[var(--duties-tertiary)] mb-1">
                  模型名称
                </label>
                <input
                  className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="gpt-4o"
                  value={model}
                />
              </div>

              {error && <p className="text-xs text-red-500">{error}</p>}

              <Button
                className="w-full mt-2"
                disabled={saving}
                onClick={handleSave}
                variant="primary"
              >
                {saving ? '正在保存…' : '保存并继续'}
              </Button>

              <p className="text-[0.6rem] text-[var(--duties-tertiary)]">
                稍后可以在设置页面添加更多渠道或修改配置。
              </p>
            </div>
          )}

          {step === 'done' && (
            <div className="text-center py-4">
              <p className="text-sm text-[var(--duties-text)] mb-2">渠道配置完成</p>
              <p className="text-[0.65rem] text-[var(--duties-tertiary)] mb-4">
                现在可以开始对话了。
              </p>
              <Button className="w-full" onClick={handleFinish} variant="primary">
                进入应用
              </Button>
            </div>
          )}
        </div>

        <p className="mt-3 text-center font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
          Clonoth 初始设置向导
        </p>
      </div>
    </div>
  );
};
