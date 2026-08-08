// [2026-06-02] Advanced raw configuration settings page.
// Runtime common params are now on the System page. This page keeps raw YAML
// editing for both runtime.yaml and policy.yaml as a fallback for advanced users.
import { useEffect, useState } from 'react';

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
}

const FILES: Array<{ key: FileKey; title: string; filename: string; description: string }> = [
  { key: 'runtime', title: '运行时配置 (runtime.yaml)', filename: 'runtime.yaml', description: '运行时参数的完整 YAML。常用参数请在系统页编辑。' },
  { key: 'policy', title: '安全策略 (policy.yaml)', filename: 'policy.yaml', description: '工具、文件、命令等安全策略配置。' },
];

export const AdvancedSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { setAdvancedFile } = useSettingsSelectionStore();
  const [files, setFiles] = useState<Record<FileKey, RawFileState>>({
    runtime: { value: '', message: '', loading: false },
    policy: { value: '', message: '', loading: false },
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
      setFileState(key, { value: raw, message: '' });
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '加载失败' });
    } finally {
      setFileState(key, { loading: false });
    }
  };

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return;
  }, [adminToken, isAuthenticated]);

  const saveOne = async (key: FileKey) => {
    if (key === 'policy' && !window.confirm('修改安全策略可能影响系统安全性')) return;
    const value = files[key].value;
    const issue = hasLikelyYamlSyntaxIssue(value);
    if (issue) { setFileState(key, { message: issue }); return; }
    setFileState(key, { loading: true, message: '' });
    try {
      await saveRaw(key, value);
      setFileState(key, { value, message: '已保存' });
    } catch (error) {
      setFileState(key, { message: error instanceof Error ? error.message : '保存失败' });
    } finally {
      setFileState(key, { loading: false });
    }
  };

  return (
    <PageShell>
      <PageHeader description="运行时和安全策略的原始 YAML 编辑。常用运行参数请在系统页编辑。" title="高级配置" />
      {!isAuthenticated ? <AuthRequired /> : (
        <div className="space-y-4">
          {FILES.map((file) => {
            const state = files[file.key];
            return (
              <Card description={file.description} key={file.key}>
                <details onToggle={(event) => { if ((event.currentTarget as HTMLDetailsElement).open) setAdvancedFile(file.key); }}>
                  <summary className="-pointer font-mono text-xs font-semibold text-[var(--duties-text)]">{file.title}</summary>
                  <div className="mt-3 space-y-3">
                    {file.key === 'policy' && <p className="text-xs leading-5 text-orange-400">警告：修改安全策略可能影响系统安全性。</p>}
                    <div className="flex flex-wrap gap-2">
                      <Button disabled={state.loading} onClick={() => loadOne(file.key)}>{state.loading ? '处理中...' : '加载'}</Button>
                      <Button disabled={state.loading} onClick={() => saveOne(file.key)} variant="primary">保存</Button>
                    </div>
                    <YamlEditor aria-label={`${file.filename} YAML 编辑器`} height="26rem" onChange={(value) => setFileState(file.key, { value })} value={state.value} />
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
