// [2026-06-02] External tool management settings page.
// Why: custom Python tools need list, create, delete, reload, and risk display in one
// operations screen. How: keep only the list and action buttons here while the Python
// script editor is rendered by the Settings right panel. Purpose: tool management is
// clearer and the editor no longer stacks below the list.
import { useEffect, useState } from 'react';

import { createTool, deleteTool, getAllToolNames, getTools, reloadTools, type AdminTool } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, StatusText, TextInput } from './settingsPagePrimitives';

function defaultToolScript(name: string): string {
  // [2026-06-02] Provide a safe external-tool starter file. Why: Supervisor expects
  // Python files with SPEC and output/fail helpers. How: create a minimal script that
  // returns its input and includes a modification note. Purpose: new tools are valid
  // placeholders until the operator edits their actual implementation in the right panel.
  return `# [2026-06-02] Created from Settings. Why: the web UI creates a minimal external tool. How: edit SPEC and the script body before enabling real behavior. Purpose: keep new tool files syntactically valid.\nSPEC = {\n    "name": "${name}",\n    "description": "通过设置页面创建的工具。",\n    "input_schema": {"type": "object", "properties": {}}\n}\nTIMEOUT_SEC = 30\n\noutput({"ok": True, "args": args})\n`;
}

export const ToolsSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // tool on mobile should reveal the Python editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedTool, setAllToolNames, setSelectedTool } = useSettingsSelectionStore();
  const [tools, setTools] = useState<AdminTool[]>([]);
  const [newName, setNewName] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const [items, names] = await Promise.all([getTools(adminToken), getAllToolNames(adminToken)]);
      setTools(items);
      setAllToolNames(names);
      if (selectedTool && !items.some((item) => item.name === selectedTool.name)) setSelectedTool(null);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载工具失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Let the right-panel editor refresh parsed tool metadata after save.
    // Why: the editor is no longer inside this page. How: reload on a local browser
    // event emitted after saving raw Python. Purpose: SPEC description and timeout in
    // the list stay aligned with the file on disk.
    const handler = () => { void load(); };
    window.addEventListener('settings:tools-updated', handler);
    return () => window.removeEventListener('settings:tools-updated', handler);
  }, [adminToken, isAuthenticated, selectedTool?.name]);

  const create = async () => {
    if (!adminToken) return;
    const name = newName.trim();
    if (!name) { setMessage('请输入工具名称'); return; }
    try {
      await createTool(adminToken, { id: name, content: defaultToolScript(name) });
      setNewName('');
      setMessage('工具已创建，请在右栏编辑 Python 脚本。');
      await load();
      setSelectedTool({ name, description: '通过设置页面创建的工具。', input_schema: { type: 'object', properties: {} }, timeout_sec: 30, has_spec: true });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建工具失败');
    }
  };

  const remove = async () => {
    if (!adminToken || !selectedTool) return;
    if (!window.confirm(`确定要删除工具 ${selectedTool.name} 吗？`)) return;
    try {
      await deleteTool(adminToken, selectedTool.name);
      setSelectedTool(null);
      setMessage('工具已删除');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '删除工具失败');
    }
  };

  const reload = async () => {
    if (!adminToken) return;
    try {
      const result = await reloadTools(adminToken);
      setMessage(`工具已重载，序号 ${result.seq ?? '未知'}`);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '重载工具失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="管理外部 Python 工具，查看风险等级，并触发工具重载。选择工具后，请在右栏编辑脚本。" title="工具管理" />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="工具列表" description="风险等级根据工具名前缀自动推断。Python 编辑器位于右栏。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={load}>{loading ? '刷新中...' : '刷新工具'}</Button>
            <Button onClick={reload} variant="primary">重载工具</Button>
            <Button disabled={!selectedTool} onClick={remove} variant="danger">删除选中工具</Button>
          </div>
          <div className="max-h-[34rem] space-y-2 overflow-y-auto">
            {tools.map((tool) => {
              const risk = inferToolRisk(tool.name);
              return (
                <button className={`w-full border p-3 text-left ${selectedTool?.name === tool.name ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={tool.name} onClick={() => { setSelectedTool(tool); setMessage(''); setRightPanelOpen(true); }} type="button">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs font-semibold">{tool.name}</span><span className={`border px-1.5 py-0.5 font-mono text-[0.55rem] ${riskClassName(risk)}`}>{riskLabel(risk)}</span></div>
                  <p className="mt-1 text-xs text-[var(--duties-secondary)]">{tool.description || '无描述'} · timeout {tool.timeout_sec ?? '未设置'}</p>
                </button>
              );
            })}
          </div>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <FieldLabel htmlFor="new-tool-name">创建工具</FieldLabel>
            <TextInput id="new-tool-name" onChange={(event) => setNewName(event.target.value)} placeholder="工具名称" value={newName} />
            <Button className="mt-2" onClick={create} variant="primary">创建工具</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
