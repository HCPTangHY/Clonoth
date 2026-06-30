// [2026-06-02] Advanced raw configuration settings page.
// Why: runtime and policy are cross-cutting YAML files. How: show the raw YAML after
// the operator opens a section, with save disabled until the latest file is loaded.
// Purpose: Advanced stays a raw editor and cannot overwrite config with placeholders.
import { useState } from 'react';

import { getPolicyRaw, getRuntimeRaw, updatePolicyRaw, updateRuntimeRaw } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button, YamlEditor } from '../../common';
import { AuthRequired, Card, PageHeader, PageShell, StatusText, hasLikelyYamlSyntaxIssue } from './settingsPagePrimitives';

type FileKey = 'runtime' | 'policy';

interface RawFileState {
  value: string;
  message: string;
  loading: boolean;
  loaded: boolean;
}

const FILES: Array<{ key: FileKey; title: string; filename: string; description: string; height: string }> = [
  { key: 'runtime', title: '运行时配置 (runtime.yaml)', filename: 'runtime.yaml', description: '运行时参数、入口节点、工具模式、记忆和进程配置。', height: '30rem' },
  { key: 'policy', title: '安全策略 (policy.yaml)', filename: 'policy.yaml', description: '工具、文件、命令等安全策略配置。', height: '30rem' },
];

export const AdvancedSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { setAdvancedFile } = useSettingsSelectionStore();
  const [files, setFiles] = useState<Record<FileKey, RawFileState>>({
    runtime: { value: '', message: '', loading: false, loaded: false },
    policy: { value: '', message: '', loading: false, loaded: false },
  });

  const setFileState = (key: FileKey, patch: Partial<RawFileState>) => {
    setFiles((current) => ({ ...current, [key]: { ...current[key], ...patch } }));
  };

  const fetchRaw = async (key: FileKey): Promise<string> => {
    if (!adminToken) return '';
    if (key === 'runtime') return getRuntimeRaw(adminToken);
    return getPolicyRaw(adminToken);
  };

  const saveRaw = async (key: FileKey, value: string): Promise<void> => {
    if (!adminToken) return;
    if (key === 'runtime') { await updateRuntimeRaw(adminToken, value); return; }
    await updatePolicyRaw(adminToken, value);
  };

  const loadOne = async (key: FileKey) => {
    setFileState(key, { loading: true, message: '' });
    try {
      const raw = await fetchRaw(key);
      setFileState(key, { value: raw, message: '', loaded: true });
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '加载失败', loaded: false });
    } finally {
      setFileState(key, { loading: false });
    }
  };

  const saveOne = async (key: FileKey) => {
    const state = files[key];
    if (!state.loaded) {
      await loadOne(key);
      setFileState(key, { message: '已自动加载最新内容。请确认内容后再次保存。' });
      return;
    }
    if (key === 'policy' && !window.confirm('修改安全策略可能影响系统安全性')) return;
    const issue = hasLikelyYamlSyntaxIssue(state.value);
    if (issue) { setFileState(key, { message: issue }); return; }
    setFileState(key, { loading: true, message: '' });
    try {
      await saveRaw(key, state.value);
      setFileState(key, { message: '已保存' });
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '保存失败' });
    } finally {
      setFileState(key, { loading: false });
    }
  };

  return (
    <PageShell>
      <PageHeader description="直接编辑 runtime.yaml 和 policy.yaml。展开文件时会自动读取最新内容，未加载前不能保存。" title="高级配置" />
      {!isAuthenticated ? <AuthRequired /> : (
        <div className="space-y-4">
          {FILES.map((file) => {
            const state = files[file.key];
            return (
              <Card description={file.description} key={file.key}>
                <details onToggle={(event) => {
                  if (!(event.currentTarget as HTMLDetailsElement).open) return;
                  setAdvancedFile(file.key);
                  if (!state.loaded && !state.loading) void loadOne(file.key);
                }}>
                  <summary className="cursor-pointer font-mono text-xs font-semibold text-[var(--duties-text)]">{file.title}</summary>
                  <div className="mt-3 space-y-3">
                    {file.key === 'policy' && <p className="text-xs leading-5 text-orange-400">警告：修改安全策略可能影响系统安全性。</p>}
                    <div className="flex flex-wrap gap-2">
                      <Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : state.loaded ? '重新加载' : '加载'}</Button>
                      <Button disabled={state.loading || !state.loaded} onClick={() => saveOne(file.key)} variant="primary">保存 YAML</Button>
                    </div>
                    <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height={file.height} onChange={(value) => setFileState(file.key, { value })} value={state.value} />
                    {!state.loaded && !state.loading && <p className="text-xs leading-5 text-[var(--duties-secondary)]">展开后会自动加载。加载完成前不会保存。</p>}
                    <StatusText message={state.message} />
                  </div>
                </details>
              </Card>
            );
          })}
        </div>
      )}
    </PageShell>
  );
};
