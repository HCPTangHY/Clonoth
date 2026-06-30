// [2026-06-02] MCP client settings page.
// Why: MCP clients are configured in one YAML file but operators need a parsed list
// and a field editor. How: show only the client list and selection actions in the
// center page, then let the Settings right panel edit and save the selected client.
// Purpose: MCP configuration is easier to scan while raw writeback remains available.
import { useEffect, useState } from 'react';

import { getMcpClients, getMcpClientsRaw, updateMcpClientsRaw, type McpClient } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, StatusText, TextInput } from './settingsPagePrimitives';
import { parseMcpClients, serializeMcpClients, type McpClientFormState } from '../settingsStructuredConfig';

function emptyMcpClient(id: string): McpClientFormState {
  // [2026-06-02] Create a valid MCP client draft for the form writer. Why: the
  // backend has only raw YAML endpoints for MCP clients. How: append a normalized
  // streamable_http client with empty URL and headers. Purpose: new clients can be
  // created from the list page and then completed in the right panel.
  return { id, description: '通过设置页面创建的 MCP Client。', enabled: true, transport: 'streamable_http', command: '', argsText: '', envText: '', url: '', headersText: '' };
}

export const McpSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting an
  // MCP client on mobile should reveal the connection editor immediately. How: call
  // the shared settings-store setter from each row click. Purpose: users do not need a
  // second tap on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedMcpClient, setSelectedMcpClient } = useSettingsSelectionStore();
  const [clients, setClients] = useState<McpClient[]>([]);
  const [newId, setNewId] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const items = await getMcpClients(adminToken);
      setClients(items);
      if (selectedMcpClient && !items.some((item) => item.id === selectedMcpClient.id)) setSelectedMcpClient(null);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载 MCP 配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh parsed MCP list after the right-panel form saves raw YAML.
    // Why: the page no longer owns the MCP YAML editor. How: reload on a local browser
    // event emitted by the right panel. Purpose: transport, URL, and enabled previews
    // stay current after editing.
    const handler = () => { void load(); };
    window.addEventListener('settings:mcp-updated', handler);
    return () => window.removeEventListener('settings:mcp-updated', handler);
  }, [adminToken, isAuthenticated, selectedMcpClient?.id]);

  const create = async () => {
    if (!adminToken) return;
    const id = newId.trim();
    if (!id) { setMessage('请输入 MCP Client ID'); return; }
    try {
      const raw = await getMcpClientsRaw(adminToken);
      const forms = parseMcpClients(raw);
      if (forms.some((client) => client.id === id)) { setMessage('该 MCP Client 已存在'); return; }
      const nextForms = [...forms, emptyMcpClient(id)];
      await updateMcpClientsRaw(adminToken, serializeMcpClients(nextForms));
      setNewId('');
      setMessage('MCP Client 已创建，请在右栏填写连接信息。');
      await load();
      setSelectedMcpClient({ id, enabled: true, transport: 'streamable_http', description: '通过设置页面创建的 MCP Client。' });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建 MCP Client 失败');
    }
  };

  const remove = async () => {
    if (!adminToken || !selectedMcpClient) return;
    if (!window.confirm(`确定要删除 MCP Client ${selectedMcpClient.id} 吗？`)) return;
    try {
      const raw = await getMcpClientsRaw(adminToken);
      const nextForms = parseMcpClients(raw).filter((client) => client.id !== selectedMcpClient.id);
      await updateMcpClientsRaw(adminToken, serializeMcpClients(nextForms));
      setSelectedMcpClient(null);
      setMessage('MCP Client 已删除');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '删除 MCP Client 失败');
    }
  };

  const preview = (client: McpClient): string => {
    if (client.transport === 'stdio') return String(client.command || '未设置命令');
    return String(client.url || '未设置 URL');
  };

  return (
    <PageShell>
      <PageHeader description="查看 MCP Client 列表，选择后在右栏编辑连接字段和保存。" title="MCP" />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="Client 列表" description="展示 ID、传输方式、启用状态和连接预览。编辑表单位于右栏。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={load}>{loading ? '刷新中...' : '刷新 MCP'}</Button>
            <Button disabled={!selectedMcpClient} onClick={remove} variant="danger">删除选中 Client</Button>
          </div>
          <div className="max-h-[34rem] space-y-2 overflow-y-auto">
            {clients.length === 0 ? <p className="text-sm text-[var(--duties-secondary)]">暂无 MCP Client。</p> : clients.map((client) => (
              <button className={`w-full border p-3 text-left ${selectedMcpClient?.id === client.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={client.id} onClick={() => { setSelectedMcpClient(client); setMessage(''); setRightPanelOpen(true); }} type="button">
                <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs font-semibold">{client.id}</span><span className={`border px-1.5 py-0.5 text-[0.55rem] ${client.enabled === false ? 'border-red-200 bg-red-50 text-red-700' : 'border-green-200 bg-green-50 text-green-700'}`}>{client.enabled === false ? '禁用' : '启用'}</span><span className="font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{client.transport || '未知传输'}</span></div>
                <p className="mt-1 truncate text-xs text-[var(--duties-secondary)]">{preview(client)}</p>
              </button>
            ))}
          </div>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <FieldLabel htmlFor="new-mcp-id">创建 MCP Client</FieldLabel>
            <TextInput id="new-mcp-id" onChange={(event) => setNewId(event.target.value)} placeholder="Client ID" value={newId} />
            <Button className="mt-2" onClick={create} variant="primary">创建 Client</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
