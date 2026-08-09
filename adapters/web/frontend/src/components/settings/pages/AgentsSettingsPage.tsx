// [2026-06-02] Agent and node management settings page.
// Why: node YAML files are core runtime assets and need a first-class editor. How:
// list configured nodes, support template-based creation and deletion, and keep the
// raw YAML editor in the independent Settings right panel. Purpose: the center page
// stays focused on selection and operations while the right rail owns editing.
import { useEffect, useState } from 'react';

import { createNode, deleteNode, getNodes, type AdminNode } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, SelectInput, StatusText, TextInput } from './settingsPagePrimitives';

function nodeTemplate(id: string, template: string): string {
  // [2026-06-02] Build a minimal node YAML from the selected template type. Why: the
  // backend create endpoint accepts complete content, not a template identifier. How:
  // provide safe starter YAML with comments explaining required follow-up edits.
  // Purpose: users can create a valid file and then refine it in the right panel.
  const base = `# [2026-06-02] Created from Settings. Why: the web UI creates raw node YAML from a compact template. How: edit this file after creation to match the node role. Purpose: keep node creation explicit and reviewable.\nid: ${id}\nname: ${id}\ndescription: 通过设置页面创建的节点。\n`;
  if (template === 'tool') return `${base}type: tool\nmodel: ""\ntool_access:\n  mode: none\ndelegate_targets: []\n`;
  return `${base}type: ai\nmodel: ""\ntool_access:\n  mode: all\ndelegate_targets: []\n`;
}

export const AgentsSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // node on mobile should reveal the YAML editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedNode, setSelectedNode } = useSettingsSelectionStore();
  const [nodes, setNodes] = useState<AdminNode[]>([]);
  const [newId, setNewId] = useState('');
  const [template, setTemplate] = useState('ai');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const loadNodes = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const items = await getNodes(adminToken);
      setNodes(items);
      if (selectedNode && !items.some((item) => item.id === selectedNode.id)) setSelectedNode(null);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载节点失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadNodes(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh the list after the right panel saves raw YAML. Why: the
    // editor lives outside this page, so a browser event is the smallest shared signal.
    // How: listen for a local settings event and reload parsed node metadata. Purpose:
    // saved names, models, and delegate summaries can appear without leaving the tab.
    const handler = () => { void loadNodes(); };
    window.addEventListener('settings:nodes-updated', handler);
    return () => window.removeEventListener('settings:nodes-updated', handler);
  }, [adminToken, isAuthenticated, selectedNode?.id]);

  const create = async () => {
    if (!adminToken) return;
    const id = newId.trim();
    if (!id) { setMessage('请输入节点 ID'); return; }
    try {
      await createNode(adminToken, { id, content: nodeTemplate(id, template) });
      setNewId('');
      setMessage('节点已创建，请在右栏继续编辑 YAML。');
      await loadNodes();
      setSelectedNode({ id, name: id, type: template === 'tool' ? 'tool' : 'ai', model: '', description: '通过设置页面创建的节点。', delegate_targets: [] } as AdminNode);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建节点失败');
    }
  };

  const remove = async () => {
    if (!adminToken || !selectedNode) return;
    if (!window.confirm(`确定要删除节点 ${selectedNode.id} 吗？此操作会删除对应 YAML 文件。`)) return;
    try {
      await deleteNode(adminToken, selectedNode.id);
      setSelectedNode(null);
      setMessage('节点已删除');
      await loadNodes();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '删除节点失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="查看、创建和删除节点配置文件。选择节点后，请在右侧面板编辑对应 YAML。" title="节点管理" />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="节点列表" description="中间区域只负责列表和操作，节点 YAML 编辑器显示在右栏。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={loadNodes}>{loading ? '刷新中...' : '刷新节点'}</Button>
            <Button disabled={!selectedNode} onClick={remove} variant="danger">删除选中节点</Button>
          </div>
          <div className="max-h-[34rem] space-y-2 overflow-y-auto">
            {/* 用户节点 */}
            {nodes.filter(n => n.source !== 'system').length > 0 && (
              <p className="font-mono text-[0.6rem] uppercase tracking-[0.15em] text-[var(--duties-tertiary)]">User Nodes</p>
            )}
            {nodes.filter(n => n.source !== 'system').map((node) => (
              <button className={`w-full border p-3 text-left ${selectedNode?.id === node.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={node.id} onClick={() => { setSelectedNode(node); setMessage(''); setRightPanelOpen(true); }} type="button">
                <p className="font-mono text-xs font-semibold">{node.id}</p>
                <p className="mt-1 text-xs text-[var(--duties-secondary)]">{node.name || '未命名'} · {node.type || '未知类型'} · {node.model || '未设置模型'}</p>
                {node.description && <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--duties-secondary)]">{node.description}</p>}
              </button>
            ))}
            {/* 系统节点 */}
            {nodes.filter(n => n.source === 'system').length > 0 && (
              <p className="mt-3 font-mono text-[0.6rem] uppercase tracking-[0.15em] text-[var(--duties-tertiary)]">System Nodes</p>
            )}
            {nodes.filter(n => n.source === 'system').map((node) => (
              <button className={`w-full border p-3 text-left ${selectedNode?.id === node.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={node.id} onClick={() => { setSelectedNode(node); setMessage(''); setRightPanelOpen(true); }} type="button">
                <div className="flex items-center gap-2">
                  <p className="font-mono text-xs font-semibold">{node.id}</p>
                  <span className="rounded-sm bg-[var(--duties-border)] px-1.5 py-0.5 font-mono text-[0.55rem] text-[var(--duties-tertiary)]">system</span>
                </div>
                <p className="mt-1 text-xs text-[var(--duties-secondary)]">{node.name || '未命名'} · {node.type || '未知类型'} · {node.model || '未设置模型'}</p>
                {node.description && <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--duties-secondary)]">{node.description}</p>}
              </button>
            ))}
          </div>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <FieldLabel htmlFor="new-node-id">创建节点</FieldLabel>
            <TextInput id="new-node-id" onChange={(event) => setNewId(event.target.value)} placeholder="节点 ID" value={newId} />
            <SelectInput className="mt-2" onChange={(event) => setTemplate(event.target.value)} value={template}>
              <option value="ai">AI 节点模板</option>
              <option value="tool">工具节点模板</option>
            </SelectInput>
            <Button className="mt-2" onClick={create} variant="primary">创建节点</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
