// [2026-06-02] Contextual right panels for expanded Settings tabs.
// Why: the settings right rail is rendered independently from the selected page, but
// new pages need contextual details such as logs, approval parameters, node delegates,
// tool schemas, and MCP connection data. How: read snapshots from the settings
// selection store and render small Chinese summaries. Purpose: each tab can keep its
// main workflow focused while the right rail provides reference details.
import { useEffect, useState, type ReactNode } from 'react';

// [2026-06-02] Add raw MCP and schedule endpoints for the right-panel form editors.
// Why: the second editable Settings batch saves selected MCP clients and schedules
// through full raw YAML reloads. How: import only the existing raw read/write wrappers
// alongside the earlier node, skill, and tool helpers. Purpose: the panel can update
// one selected item without introducing new API contracts or touching other panels.
import { getMcpClientsRaw, getNodeRaw, getSchedulesRaw, getSkillRaw, getToolRaw, reloadTools, updateMcpClientsRaw, updateNodeRaw, updateSchedulesRaw, updateSkillRaw, updateToolRaw } from '../../../api/supervisorClient';
// [2026-06-02] Use shared lightweight structured YAML helpers in the two requested panels.
// Why: MCP clients and automation schedules already have parse and serialize helpers
// that preserve the expected config shape. How: import the form state types and
// parser/serializer functions from settingsStructuredConfig. Purpose: the new forms
// can edit structured fields while writing valid raw YAML back to Supervisor.
import { parseMcpClients, parseNodeConfig, parseSchedules, serializeMcpClients, serializeNodeConfig, serializeSchedules, type McpClientFormState, type NodeConfigFormState, type NodeConfigType, type ScheduleFormState, type ToolAccessMode } from '../settingsStructuredConfig';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { YamlEditor } from '../../common';

const PanelShell = ({ title, eyebrow, children }: { title: string; eyebrow?: string; children: ReactNode }) => (
  <section className="flex h-full min-h-0 flex-col overflow-y-auto p-3">
    <p className="font-mono text-[0.55rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">{eyebrow || '设置详情'}</p>
    <h2 className="mt-1 mb-3 font-mono text-sm font-semibold tracking-[-0.03em]">{title}</h2>
    {children}
  </section>
);

const JsonBlock = ({ value }: { value: unknown }) => (
  <pre className="max-h-80 overflow-auto border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 font-mono text-[0.65rem] leading-4 text-[var(--duties-secondary)]">
    {JSON.stringify(value || {}, null, 2)}
  </pre>
);

// [2026-06-02] Shared classes for the structured settings form controls.
// Why: the node right panel now edits YAML-backed fields through direct inputs and
// must match the requested compact Settings styling. How: keep the exact input and
// label class names in constants used by the new controls. Purpose: new form fields
// stay visually consistent and are easy to audit against the task requirements.
const STRUCTURED_INPUT_CLASS = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const STRUCTURED_LABEL_CLASS = 'block mb-1 text-[var(--duties-tertiary)] text-[0.65rem]';

const EMPTY_NODE_CONFIG_FORM: NodeConfigFormState = {
  id: '',
  name: '',
  description: '',
  type: 'ai',
  model: '',
  provider: '',
  memory_book: '',
  persistent: false,
  delegate_targetsText: '',
  tool_access_mode: 'all',
  tool_access_allowText: '',
  tool_access_denyText: '',
  prompt: '',
};

export const SystemSettingsRightPanel = () => {
  const logs = useSettingsSelectionStore(state => state.systemLogs);
  return (
    <PanelShell eyebrow="系统" title="最近操作日志">
      {logs.length === 0 ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">暂无配置重载或重启操作记录。</p>
      ) : (
        <div className="space-y-2">
          {logs.map((log, index) => (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5 text-[var(--duties-secondary)]" key={`${log}-${index}`}>
              {log}
            </div>
          ))}
        </div>
      )}
    </PanelShell>
  );
};

export const ApprovalsSettingsRightPanel = () => {
  const approval = useSettingsSelectionStore(state => state.selectedApproval);
  return (
    <PanelShell eyebrow="审批" title="审批详情">
      {!approval ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个待审批项后，这里会显示完整参数。</p>
      ) : (
        <div className="space-y-3">
          <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5">
            <p><span className="text-[var(--duties-tertiary)]">审批 ID：</span><span className="font-mono">{approval.approval_id}</span></p>
            <p><span className="text-[var(--duties-tertiary)]">工具调用：</span><span className="font-mono">{approval.tool_call_id || '未提供'}</span></p>
            <p><span className="text-[var(--duties-tertiary)]">节点：</span><span className="font-mono">{approval.node_id || '未提供'}</span></p>
          </div>
          <JsonBlock value={approval.details} />
        </div>
      )}
    </PanelShell>
  );
};

export const AgentsSettingsRightPanel = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const providerModels = useSettingsStore(state => state.providerModels);
  const registeredProviders = Object.keys(providerModels);
  const node = useSettingsSelectionStore(state => state.selectedNode);
  const [nodeYaml, setNodeYaml] = useState('');
  const [nodeConfigForm, setNodeConfigForm] = useState<NodeConfigFormState>(EMPTY_NODE_CONFIG_FORM);
  const [nodeYamlEditable, setNodeYamlEditable] = useState(false);
  const [nodeYamlLoading, setNodeYamlLoading] = useState(false);
  const [nodeYamlSaving, setNodeYamlSaving] = useState(false);
  const [nodeMessage, setNodeMessage] = useState('');

  const updateNodeConfigForm = (patch: Partial<NodeConfigFormState>) => {
    // [2026-06-02] Apply small form patches instead of editing raw YAML directly.
    // Why: the Agents panel now treats structured controls as the primary editor.
    // How: merge individual field changes into the parsed form state. Purpose: saving
    // can serialize the full form back into the selected node YAML.
    setNodeConfigForm((current) => ({ ...current, ...patch }));
  };

  useEffect(() => {
    let cancelled = false;
    setNodeYaml('');
    setNodeConfigForm({ ...EMPTY_NODE_CONFIG_FORM, id: node?.id || '' });
    setNodeYamlEditable(false);
    setNodeMessage('');
    if (!node) return () => { cancelled = true; };
    if (!adminToken) {
      setNodeMessage('缺少管理员令牌，无法加载节点配置。');
      return () => { cancelled = true; };
    }
    setNodeYamlLoading(true);
    // [2026-06-02] Load raw node YAML and immediately parse it into form fields.
    // Why: the right panel must be a structured form editor, not a raw YAML editor.
    // How: fetch the selected file, store the raw fallback, and populate the form via
    // parseNodeConfig. Purpose: users edit common node properties directly while the
    // advanced YAML section remains available for unsupported fields.
    getNodeRaw(adminToken, node.id)
      .then((raw) => {
        if (cancelled) return;
        setNodeYaml(raw);
        setNodeConfigForm(parseNodeConfig(raw, node.id));
        setNodeYamlEditable(true);
      })
      .catch((error) => {
        if (cancelled) return;
        setNodeMessage(error instanceof Error ? error.message : '加载节点配置失败');
        setNodeYamlEditable(false);
      })
      .finally(() => {
        if (!cancelled) setNodeYamlLoading(false);
      });
    return () => { cancelled = true; };
  }, [adminToken, node?.id]);

  const saveNodeConfig = async () => {
    if (!adminToken || !node || !nodeYamlEditable) return;
    setNodeYamlSaving(true);
    setNodeMessage('保存中。');
    try {
      // [2026-06-02] Serialize the structured node form into YAML before saving.
      // Why: raw YAML is only an advanced fallback and must not be the primary save
      // path. How: replace the edited fields in the loaded YAML, submit updateNodeRaw,
      // and update the fallback editor with the exact saved content. Purpose: the
      // panel writes valid YAML while keeping user edits to unrelated fields intact.
      const nextYaml = serializeNodeConfig(nodeYaml, nodeConfigForm);
      await updateNodeRaw(adminToken, node.id, nextYaml);
      setNodeYaml(nextYaml);
      setNodeConfigForm(parseNodeConfig(nextYaml, node.id));
      window.dispatchEvent(new Event('settings:nodes-updated'));
      setNodeMessage('节点配置已保存。');
    } catch (error) {
      setNodeMessage(error instanceof Error ? error.message : '保存节点配置失败');
    } finally {
      setNodeYamlSaving(false);
    }
  };

  const saveNodeYaml = async () => {
    if (!adminToken || !node || !nodeYamlEditable) return;
    setNodeYamlSaving(true);
    setNodeMessage('保存中。');
    try {
      // [2026-06-02] Keep a raw YAML fallback inside the advanced details section.
      // Why: uncommon node fields may not have dedicated form controls yet. How: save
      // the controlled fallback editor and reparse it into the structured form after a
      // successful write. Purpose: advanced edits remain possible without making raw
      // YAML the default editing surface.
      await updateNodeRaw(adminToken, node.id, nodeYaml);
      setNodeConfigForm(parseNodeConfig(nodeYaml, node.id));
      window.dispatchEvent(new Event('settings:nodes-updated'));
      setNodeMessage('节点 YAML 已保存。');
    } catch (error) {
      setNodeMessage(error instanceof Error ? error.message : '保存节点 YAML 失败');
    } finally {
      setNodeYamlSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="节点" title="节点结构化编辑">
      {!node ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个节点后，这里会显示节点属性、委派关系和工具权限表单。</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          {nodeYamlLoading ? (
            <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">正在加载节点配置。</p>
          ) : !nodeYamlEditable ? (
            <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">{nodeMessage || '系统内建节点不可编辑'}</p>
          ) : (
            <>
              <div className="space-y-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                <div>
                  <span className={STRUCTURED_LABEL_CLASS}>节点 ID</span>
                  <p aria-label="节点 ID" className="font-mono text-xs">{nodeConfigForm.id}</p>
                </div>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>名称</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ name: event.target.value })} value={nodeConfigForm.name} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>描述</span>
                  <textarea className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ description: event.target.value })} rows={3} value={nodeConfigForm.description} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>类型</span>
                  <select className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ type: event.target.value as NodeConfigType })} value={nodeConfigForm.type}>
                    <option value="ai">ai</option>
                    <option value="tool">tool</option>
                    <option value="router">router</option>
                  </select>
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>模型</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ model: event.target.value })} value={nodeConfigForm.model} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>供应商（渠道），可选</span>
                  <select
                    className={STRUCTURED_INPUT_CLASS}
                    onChange={(event) => updateNodeConfigForm({ provider: event.target.value })}
                    value={nodeConfigForm.provider}
                  >
                    <option value="">默认（继承全局）</option>
                    {registeredProviders.map(name => (
                      <option key={name} value={name}>
                        {name}{providerModels[name] ? ` — ${providerModels[name]}` : ''}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>记忆簿，可选</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ memory_book: event.target.value })} value={nodeConfigForm.memory_book} />
                </label>
                <label className="flex items-center justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                  <span>持久化</span>
                  <input checked={nodeConfigForm.persistent} onChange={(event) => updateNodeConfigForm({ persistent: event.target.checked })} type="checkbox" />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>系统提示词</span>
                  <textarea className={`${STRUCTURED_INPUT_CLASS} min-h-[12rem]`} onChange={(event) => updateNodeConfigForm({ prompt: event.target.value })} rows={12} value={nodeConfigForm.prompt} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>委派目标，使用英文逗号分隔</span>
                  <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ delegate_targetsText: event.target.value })} value={nodeConfigForm.delegate_targetsText} />
                </label>
                <label className="block">
                  <span className={STRUCTURED_LABEL_CLASS}>工具权限模式</span>
                  <select className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ tool_access_mode: event.target.value as ToolAccessMode })} value={nodeConfigForm.tool_access_mode}>
                    <option value="all">all</option>
                    <option value="allow">allow</option>
                    <option value="deny">deny</option>
                    <option value="none">none</option>
                  </select>
                </label>
                {nodeConfigForm.tool_access_mode === 'allow' && (
                  <label className="block">
                    <span className={STRUCTURED_LABEL_CLASS}>允许工具，使用英文逗号分隔</span>
                    <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ tool_access_allowText: event.target.value })} value={nodeConfigForm.tool_access_allowText} />
                  </label>
                )}
                {nodeConfigForm.tool_access_mode === 'deny' && (
                  <label className="block">
                    <span className={STRUCTURED_LABEL_CLASS}>拒绝工具，使用英文逗号分隔</span>
                    <input className={STRUCTURED_INPUT_CLASS} onChange={(event) => updateNodeConfigForm({ tool_access_denyText: event.target.value })} value={nodeConfigForm.tool_access_denyText} />
                  </label>
                )}
                <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50" disabled={nodeYamlSaving} onClick={saveNodeConfig} type="button">保存节点配置</button>
                {nodeMessage && <p className="text-[var(--duties-tertiary)]">{nodeMessage}</p>}
              </div>
              <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
                <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">高级 YAML 编辑</summary>
                <div className="mt-3 space-y-2">
                  {/* [2026-06-02] Keep raw node YAML only as a collapsed fallback.
                      Why: the task explicitly requires structured form editing as the
                      main surface. How: render YamlEditor inside default-collapsed
                      details and give it a separate YAML save button. Purpose: advanced
                      users can recover unsupported fields without reverting the panel
                      to a raw YAML editor. */}
                  <YamlEditor aria-label="节点 YAML 编辑器" height="16rem" onChange={setNodeYaml} value={nodeYaml} />
                  <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50" disabled={nodeYamlSaving} onClick={saveNodeYaml} type="button">保存 YAML</button>
                </div>
              </details>
            </>
          )}
        </div>
      )}
    </PanelShell>
  );
};

export const ToolsSettingsRightPanel = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const tool = useSettingsSelectionStore(state => state.selectedTool);
  const [toolScript, setToolScript] = useState('');
  const [toolScriptLoaded, setToolScriptLoaded] = useState(false);
  const [toolScriptLoading, setToolScriptLoading] = useState(false);
  const [toolScriptSaving, setToolScriptSaving] = useState(false);
  const [toolReloading, setToolReloading] = useState(false);
  const [toolMessage, setToolMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    setToolScript('');
    setToolScriptLoaded(false);
    setToolMessage('');
    if (!tool) return () => { cancelled = true; };
    if (!adminToken) {
      setToolMessage('缺少管理员令牌，无法加载工具脚本。');
      return () => { cancelled = true; };
    }
    setToolScriptLoading(true);
    // [2026-06-02] Load the raw Python script for the selected tool.
    // Why: the Tools right panel must support direct edits while keeping the current
    // name, risk, schema, and full-name display. How: fetch the backend raw script when
    // the selected tool name changes and store it in a controlled textarea. Purpose:
    // users can update one tool implementation without leaving the context panel.
    getToolRaw(adminToken, tool.name)
      .then((raw) => {
        if (cancelled) return;
        setToolScript(raw);
        setToolScriptLoaded(true);
      })
      .catch((error) => {
        if (cancelled) return;
        setToolMessage(error instanceof Error ? error.message : '加载工具脚本失败');
        setToolScriptLoaded(false);
      })
      .finally(() => {
        if (!cancelled) setToolScriptLoading(false);
      });
    return () => { cancelled = true; };
  }, [adminToken, tool?.name]);

  const saveToolScript = async () => {
    if (!adminToken || !tool || !toolScriptLoaded) return;
    setToolScriptSaving(true);
    setToolMessage('保存中。');
    try {
      // [2026-06-02] Persist edited tool code and notify settings lists.
      // Why: saving from the right rail should update the selected tool file and let
      // the rest of Settings reload parsed metadata. How: PUT the controlled Python
      // script and dispatch settings:tools-updated after success. Purpose: the tool
      // list can refresh after a raw script save without requiring an engine restart.
      await updateToolRaw(adminToken, tool.name, toolScript);
      window.dispatchEvent(new Event('settings:tools-updated'));
      setToolMessage('工具脚本已保存。');
    } catch (error) {
      setToolMessage(error instanceof Error ? error.message : '保存工具脚本失败');
    } finally {
      setToolScriptSaving(false);
    }
  };

  const reloadToolRegistry = async () => {
    if (!adminToken) return;
    setToolReloading(true);
    setToolMessage('正在重载工具。');
    try {
      // [2026-06-02] Expose tool reload beside the raw script editor.
      // Why: edited Python tools usually need the backend registry to reload before
      // calls use the new code. How: call the existing reloadTools admin endpoint from
      // the panel. Purpose: users can save and reload tools without requesting an
      // engine restart.
      await reloadTools(adminToken);
      setToolMessage('工具已重载。');
    } catch (error) {
      setToolMessage(error instanceof Error ? error.message : '重载工具失败');
    } finally {
      setToolReloading(false);
    }
  };

  return (
    <PanelShell eyebrow="工具" title="工具说明">
      <div className="space-y-3">
        {tool ? (
          <div className="space-y-2">
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-xs leading-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono font-semibold">{tool.name}</span>
                <span className={`border px-1.5 py-0.5 font-mono text-[0.55rem] ${riskClassName(inferToolRisk(tool.name))}`}>{riskLabel(inferToolRisk(tool.name))}</span>
              </div>
              <p className="mt-1 text-[var(--duties-secondary)]">{tool.description || '无描述'}</p>
            </div>
            <JsonBlock value={tool.input_schema || {}} />
            <div className="space-y-2 text-xs leading-5">
              <h3 className="mb-2 font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">Python 脚本编辑</h3>
              {/* [2026-06-02] Add the requested raw Python editor below schema details.
                  Why: tool metadata alone cannot change implementation code. How: bind
                  the selected tool's raw script to the specified monospace textarea and
                  keep save and reload actions beside it. Purpose: the first editable
                  Tools batch changes only this panel while preserving current context. */}
              <textarea
                className="w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 font-mono text-xs"
                disabled={!toolScriptLoaded || toolScriptLoading}
                onChange={(event) => setToolScript(event.target.value)}
                rows={16}
                spellCheck={false}
                value={toolScriptLoading ? '正在加载工具脚本。' : toolScript}
              />
              <div className="flex flex-wrap gap-2">
                <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50" disabled={!toolScriptLoaded || toolScriptLoading || toolScriptSaving} onClick={saveToolScript} type="button">保存</button>
                <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50" disabled={!adminToken || toolReloading} onClick={reloadToolRegistry} type="button">重载工具</button>
              </div>
              {toolMessage && <p className="text-[var(--duties-tertiary)]">{toolMessage}</p>}
            </div>
          </div>
        ) : (
          <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个工具后，这里会显示 input_schema 文档。</p>
        )}

      </div>
    </PanelShell>
  );
};

interface SkillFrontmatterEdit {
  enabled: boolean;
  strategy: string;
  keywords: string;
  order: string;
  priority: string;
  scan_depth: string;
}

function replaceFrontmatterValue(raw: string, key: string, value: string): string {
  // [2026-06-02] Update one YAML frontmatter scalar while preserving Markdown body.
  // Why: the Skills right panel offers quick metadata edits without replacing the
  // whole editor content. How: change an existing key line inside the leading
  // frontmatter block, or insert it before the closing delimiter. Purpose: users can
  // adjust common activation fields quickly while raw Markdown remains authoritative.
  if (!raw.startsWith('---\n')) return raw;
  const end = raw.indexOf('\n---\n', 4);
  if (end < 0) return raw;
  const head = raw.slice(0, end);
  const body = raw.slice(end);
  const line = `${key}: ${value}`;
  if (new RegExp(`^${key}:`, 'm').test(head)) return `${head.replace(new RegExp(`^${key}:.*$`, 'm'), line)}${body}`;
  return `${head}\n${line}${body}`;
}

function frontmatterScalar(raw: string, key: string, fallback: string): string {
  // [2026-06-02] Read one scalar field from a skill frontmatter block.
  // Why: the parsed skills list currently omits order, priority, and scan_depth. How:
  // scan the raw header text for a simple key-value line and strip surrounding quotes.
  // Purpose: the quick editor can show the actual current values before saving.
  if (!raw.startsWith('---\n')) return fallback;
  const end = raw.indexOf('\n---\n', 4);
  if (end < 0) return fallback;
  const head = raw.slice(0, end);
  const match = head.match(new RegExp(`^${key}:\\s*(.*)$`, 'm'));
  return match?.[1]?.trim().replace(/^['\"]|['\"]$/g, '') || fallback;
}

function applySkillFrontmatter(raw: string, edit: SkillFrontmatterEdit): string {
  const keywordItems = edit.keywords.split(',').map((item) => item.trim()).filter(Boolean);
  let next = raw;
  next = replaceFrontmatterValue(next, 'enabled', edit.enabled ? 'true' : 'false');
  next = replaceFrontmatterValue(next, 'strategy', edit.strategy || 'normal');
  next = replaceFrontmatterValue(next, 'keywords', `[${keywordItems.map((item) => `"${item.replace(/"/g, '\\"')}"`).join(', ')}]`);
  next = replaceFrontmatterValue(next, 'order', edit.order || '0');
  next = replaceFrontmatterValue(next, 'priority', edit.priority || '0');
  next = replaceFrontmatterValue(next, 'scan_depth', edit.scan_depth || '0');
  return next;
}

export const SkillsSettingsRightPanel = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const skill = useSettingsSelectionStore(state => state.selectedSkill);
  const [edit, setEdit] = useState<SkillFrontmatterEdit>({ enabled: true, strategy: 'normal', keywords: '', order: '0', priority: '0', scan_depth: '0' });
  const [rawMarkdown, setRawMarkdown] = useState('');
  const [rawMarkdownLoaded, setRawMarkdownLoaded] = useState(false);
  const [rawMarkdownLoading, setRawMarkdownLoading] = useState(false);
  const [rawMarkdownSaving, setRawMarkdownSaving] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    const baseEdit: SkillFrontmatterEdit = {
      enabled: skill?.enabled !== false,
      strategy: skill?.strategy || 'normal',
      keywords: (skill?.keywords || []).join(', '),
      order: '0',
      priority: '0',
      scan_depth: '0',
    };
    setEdit(baseEdit);
    setRawMarkdown('');
    setRawMarkdownLoaded(false);
    setMessage('');
    if (!adminToken || !skill) return () => { cancelled = true; };
    setRawMarkdownLoading(true);
    getSkillRaw(adminToken, skill.name)
      .then((raw) => {
        if (cancelled) return;
        // [2026-06-02] Load the full skill Markdown beside the quick fields.
        // Why: frontmatter editing remains convenient, but advanced users need the
        // authoritative SKILL.md content in the same context panel. How: store the raw
        // response for both scalar extraction and the collapsed Markdown textarea.
        // Purpose: one fetch initializes quick metadata and the advanced raw editor.
        setRawMarkdown(raw);
        setRawMarkdownLoaded(true);
        setEdit({
          ...baseEdit,
          order: frontmatterScalar(raw, 'order', baseEdit.order),
          priority: frontmatterScalar(raw, 'priority', baseEdit.priority),
          scan_depth: frontmatterScalar(raw, 'scan_depth', baseEdit.scan_depth),
        });
      })
      .catch(() => { if (!cancelled) setRawMarkdownLoaded(false); /* keep parsed list defaults if the raw file cannot be read */ })
      .finally(() => { if (!cancelled) setRawMarkdownLoading(false); });
    return () => { cancelled = true; };
  }, [adminToken, skill?.name, skill?.enabled, skill?.strategy, skill?.keywords?.join('|')]);

  const save = async () => {
    if (!adminToken || !skill) return;
    try {
      // [2026-06-02] Keep the quick frontmatter save in sync with the raw editor.
      // Why: users may open the advanced Markdown section after saving quick fields.
      // How: apply the frontmatter patch to the loaded raw Markdown when available,
      // otherwise fetch it once as before. Purpose: both editors show the same saved
      // content without requiring a full Settings refresh.
      const raw = rawMarkdownLoaded ? rawMarkdown : await getSkillRaw(adminToken, skill.name);
      const next = applySkillFrontmatter(raw, edit);
      await updateSkillRaw(adminToken, skill.name, next);
      setRawMarkdown(next);
      setRawMarkdownLoaded(true);
      window.dispatchEvent(new Event('settings:skills-updated'));
      setMessage('快捷字段已保存。请刷新技能列表查看解析结果。');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存快捷字段失败');
    }
  };

  const saveRawMarkdown = async () => {
    if (!adminToken || !skill) return;
    setRawMarkdownSaving(true);
    setMessage('保存中。');
    try {
      // [2026-06-02] Save the complete SKILL.md content from the advanced editor.
      // Why: some skill changes affect body text or complex frontmatter that the quick
      // form cannot represent. How: write the controlled raw Markdown textarea through
      // the existing skill raw endpoint and notify the list page to reload metadata.
      // Purpose: advanced editing remains available while hidden until requested.
      await updateSkillRaw(adminToken, skill.name, rawMarkdown);
      setRawMarkdownLoaded(true);
      window.dispatchEvent(new Event('settings:skills-updated'));
      setMessage('Raw Markdown 已保存。');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存 Raw Markdown 失败');
    } finally {
      setRawMarkdownSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="技能" title="Frontmatter 快捷编辑">
      {!skill ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个技能后，这里会显示启用状态、策略和关键词。</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">名称：</span><span className="font-mono">{skill.name}</span></p>
          <label className="flex items-center justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
            <span>启用</span>
            <input checked={edit.enabled} onChange={(event) => setEdit((current) => ({ ...current, enabled: event.target.checked }))} type="checkbox" />
          </label>
          <label className="block">
            <span className="mb-1 block text-[var(--duties-tertiary)]">策略</span>
            <select className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono" onChange={(event) => setEdit((current) => ({ ...current, strategy: event.target.value }))} value={edit.strategy}>
              <option value="normal">normal</option>
              <option value="constant">constant</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-[var(--duties-tertiary)]">关键词，使用英文逗号分隔</span>
            <input className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono" onChange={(event) => setEdit((current) => ({ ...current, keywords: event.target.value }))} value={edit.keywords} />
          </label>
          <div className="grid grid-cols-3 gap-2">
            {(['order', 'priority', 'scan_depth'] as const).map((key) => (
              <label className="block" key={key}>
                <span className="mb-1 block text-[var(--duties-tertiary)]">{key}</span>
                <input className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono" onChange={(event) => setEdit((current) => ({ ...current, [key]: event.target.value }))} value={edit[key]} />
              </label>
            ))}
          </div>
          <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)]" onClick={save} type="button">保存快捷字段</button>
          <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
            <summary className="cursor-pointer font-mono text-[0.65rem] font-semibold text-[var(--duties-tertiary)]">Raw Markdown 编辑（高级）</summary>
            <div className="mt-3 space-y-2">
              {/* [2026-06-02] Put full SKILL.md editing behind a collapsed details row.
                  Why: the quick form should stay the primary workflow, while raw body
                  edits are advanced and take more space. How: render a monospace
                  controlled textarea with twelve rows only inside the opened details
                  section. Purpose: users can edit the complete Markdown file without
                  making the right panel noisy by default. */}
              {rawMarkdownLoading && !rawMarkdownLoaded ? (
                <p className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 text-[var(--duties-secondary)]">正在加载 Raw Markdown。</p>
              ) : (
                <textarea
                  aria-label="技能 Raw Markdown 编辑器"
                  className="w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] disabled:opacity-70"
                  disabled={!rawMarkdownLoaded && rawMarkdownLoading}
                  onChange={(event) => setRawMarkdown(event.target.value)}
                  rows={12}
                  spellCheck={false}
                  value={rawMarkdown}
                />
              )}
              <button className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50" disabled={!rawMarkdownLoaded || rawMarkdownLoading || rawMarkdownSaving} onClick={saveRawMarkdown} type="button">保存 Raw Markdown</button>
            </div>
          </details>
          {message && <p className="text-[var(--duties-tertiary)]">{message}</p>}
        </div>
      )}
    </PanelShell>
  );
};

const fieldClass = 'w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1 font-mono text-xs';
const labelClass = 'block mb-1 text-[var(--duties-tertiary)] text-[0.65rem]';
const buttonClass = 'border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-[0.65rem] hover:border-[var(--duties-text)] disabled:opacity-50';

function displayMcpArgs(argsText: string): string {
  // [2026-06-02] Convert parsed newline-separated MCP args into the requested comma field.
  // Why: serializeMcpClients stores argsText as newline-delimited text, but this right
  // panel must present a single comma-separated input. How: split existing lines,
  // trim empty entries, and join them with comma-space for editing. Purpose: selected
  // stdio clients reset into the format requested by the user without changing the
  // shared serializer contract.
  return argsText.split('\n').map((item) => item.trim()).filter(Boolean).join(', ');
}

function serializeMcpArgsForYaml(argsText: string): string {
  // [2026-06-02] Convert the comma-separated MCP args input back for serialization.
  // Why: the shared MCP serializer expects newline-delimited argsText and then writes
  // a YAML list. How: split the input on commas, trim whitespace, remove empty items,
  // and rejoin with newlines. Purpose: the UI follows the requested comma input while
  // saved YAML still contains an args array.
  return argsText.split(',').map((item) => item.trim()).filter(Boolean).join('\n');
}

function displayEnvText(envText: string): string {
  // [2026-06-02] Convert parsed MCP env rows from object style to KEY=VALUE rows.
  // Why: parseMcpClients returns loose objects as `key: value`, while this panel must
  // show stdio env as one KEY=VALUE pair per line. How: replace only the first colon
  // separator on each non-empty row. Purpose: users can edit environment variables in
  // the requested shell-like format without changing the shared parser.
  return envText.split('\n').map((line) => {
    const index = line.indexOf(':');
    if (index < 0) return line;
    return `${line.slice(0, index).trim()}=${line.slice(index + 1).trim()}`;
  }).join('\n');
}

function serializeEnvTextForYaml(envText: string): string {
  // [2026-06-02] Convert KEY=VALUE MCP env rows back to loose object rows.
  // Why: serializeMcpClients parses envText with colon separators. How: replace the
  // first equals sign in each non-empty row with `: ` and leave already-colon rows
  // unchanged for operator tolerance. Purpose: environment variables save as a YAML
  // mapping while the visible form remains KEY=VALUE based.
  return envText.split('\n').map((line) => {
    const trimmed = line.trim();
    if (!trimmed) return '';
    if (trimmed.includes(':') && !trimmed.includes('=')) return trimmed;
    const index = trimmed.indexOf('=');
    if (index < 0) return trimmed;
    return `${trimmed.slice(0, index).trim()}: ${trimmed.slice(index + 1).trim()}`;
  }).join('\n');
}

export const McpSettingsRightPanel = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const client = useSettingsSelectionStore(state => state.selectedMcpClient);
  const [form, setForm] = useState<McpClientFormState | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    setForm(null);
    setMessage('');
    if (!client) return () => { cancelled = true; };
    if (!adminToken) {
      setMessage('缺少管理员令牌，无法加载 MCP Client。');
      return () => { cancelled = true; };
    }
    setLoading(true);
    // [2026-06-02] Reload raw MCP YAML whenever the selected client changes.
    // Why: the form must edit the latest persisted YAML rather than only the list
    // snapshot stored in selection state. How: fetch all raw clients, parse them, and
    // copy the matching id into local component state with display-format conversions.
    // Purpose: switching selections resets the editor and avoids saving stale fields.
    getMcpClientsRaw(adminToken)
      .then((raw) => {
        if (cancelled) return;
        const selected = parseMcpClients(raw).find((item) => item.id === client.id) || null;
        if (!selected) {
          setMessage('未在 MCP YAML 中找到此 Client。');
          setForm(null);
          return;
        }
        setForm({ ...selected, argsText: displayMcpArgs(selected.argsText), envText: displayEnvText(selected.envText) });
      })
      .catch((error) => {
        if (cancelled) return;
        setMessage(error instanceof Error ? error.message : '加载 MCP Client 失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [adminToken, client?.id]);

  const save = async () => {
    if (!adminToken || !client || !form) return;
    setSaving(true);
    setMessage('保存中。');
    try {
      // [2026-06-02] Save one MCP client by replacing it inside the parsed raw list.
      // Why: the backend stores one YAML file, so saving a single form must preserve
      // the other clients. How: reload raw YAML, parse all forms, replace the selected
      // id with the local form converted back to serializer format, serialize, save,
      // and emit the settings:mcp-updated event. Purpose: the main MCP list can refresh
      // after a successful edit without an engine restart.
      const raw = await getMcpClientsRaw(adminToken);
      const forms = parseMcpClients(raw);
      const index = forms.findIndex((item) => item.id === client.id);
      if (index < 0) throw new Error('未在 MCP YAML 中找到此 Client。');
      forms[index] = { ...form, argsText: serializeMcpArgsForYaml(form.argsText), envText: serializeEnvTextForYaml(form.envText) };
      await updateMcpClientsRaw(adminToken, serializeMcpClients(forms));
      window.dispatchEvent(new Event('settings:mcp-updated'));
      setMessage('MCP Client 已保存。');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存 MCP Client 失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="MCP" title="连接信息">
      {!client ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个 MCP Client 后，这里会显示连接配置。</p>
      ) : loading ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">正在加载 MCP Client。</p>
      ) : !form ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">{message || '未找到可编辑的 MCP Client。'}</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">ID：</span><span className="font-mono">{form.id}</span></p>
          <label className="block">
            <span className={labelClass}>description</span>
            <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, description: event.target.value }))} value={form.description} />
          </label>
          <label className="flex items-center gap-2">
            <input checked={form.enabled} onChange={(event) => setForm((current) => current && ({ ...current, enabled: event.target.checked }))} type="checkbox" />
            <span>enabled</span>
          </label>
          <label className="block">
            <span className={labelClass}>transport</span>
            <select className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, transport: event.target.value as McpClientFormState['transport'] }))} value={form.transport}>
              <option value="stdio">stdio</option>
              <option value="sse">sse</option>
              <option value="streamable_http">streamable_http</option>
            </select>
          </label>
          {form.transport === 'stdio' ? (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>command</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, command: event.target.value }))} value={form.command} />
              </label>
              <label className="block">
                <span className={labelClass}>args，使用英文逗号分隔</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, argsText: event.target.value }))} value={form.argsText} />
              </label>
              <label className="block">
                <span className={labelClass}>env，每行 KEY=VALUE</span>
                <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, envText: event.target.value }))} rows={6} value={form.envText} />
              </label>
            </div>
          ) : (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>url</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, url: event.target.value }))} value={form.url} />
              </label>
              <label className="block">
                <span className={labelClass}>headers，每行 Key: Value</span>
                <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, headersText: event.target.value }))} rows={6} value={form.headersText} />
              </label>
            </div>
          )}
          <button className={buttonClass} disabled={saving} onClick={save} type="button">保存</button>
          {message && <p className="text-[var(--duties-tertiary)]">{message}</p>}
        </div>
      )}
    </PanelShell>
  );
};

export const AutomationSettingsRightPanel = () => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const selectedScheduleId = useSettingsSelectionStore(state => state.selectedScheduleId);
  const [form, setForm] = useState<ScheduleFormState | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    setForm(null);
    setMessage('');
    if (!selectedScheduleId) return () => { cancelled = true; };
    if (!adminToken) {
      setMessage('缺少管理员令牌，无法加载定时任务。');
      return () => { cancelled = true; };
    }
    setLoading(true);
    // [2026-06-02] Load the selected schedule from raw schedules.yaml.
    // Why: the right rail only stores the selected id, and the editable form must
    // reflect the latest persisted file. How: fetch raw YAML, parse all schedules,
    // and copy the matching id into local state. Purpose: changing selected schedules
    // resets fields and avoids editing an outdated list snapshot.
    getSchedulesRaw(adminToken)
      .then((raw) => {
        if (cancelled) return;
        const selected = parseSchedules(raw).find((item) => item.id === selectedScheduleId) || null;
        setForm(selected);
        if (!selected) setMessage('未在 schedules.yaml 中找到此定时任务。');
      })
      .catch((error) => {
        if (cancelled) return;
        setMessage(error instanceof Error ? error.message : '加载定时任务失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [adminToken, selectedScheduleId]);

  const save = async () => {
    if (!adminToken || !selectedScheduleId || !form) return;
    setSaving(true);
    setMessage('保存中。');
    try {
      // [2026-06-02] Save one automation schedule inside the raw YAML list.
      // Why: schedules.yaml contains multiple tasks and the selected form should not
      // overwrite unrelated entries. How: reload raw YAML, parse all schedules, replace
      // the matching id, serialize the complete list, write it back, and emit the
      // settings:schedules-updated event. Purpose: the Automation page refreshes after
      // save while the operation stays limited to the requested right-panel editor.
      const raw = await getSchedulesRaw(adminToken);
      const schedules = parseSchedules(raw);
      const index = schedules.findIndex((item) => item.id === selectedScheduleId);
      if (index < 0) throw new Error('未在 schedules.yaml 中找到此定时任务。');
      schedules[index] = form;
      await updateSchedulesRaw(adminToken, serializeSchedules(schedules));
      window.dispatchEvent(new Event('settings:schedules-updated'));
      setMessage('定时任务已保存。');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存定时任务失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <PanelShell eyebrow="自动化" title="定时任务编辑">
      {!selectedScheduleId ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">选择一个定时任务后，这里会显示可编辑字段。</p>
      ) : loading ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">正在加载定时任务。</p>
      ) : !form ? (
        <p className="text-xs leading-5 text-[var(--duties-secondary)]">{message || '未找到可编辑的定时任务。'}</p>
      ) : (
        <div className="space-y-3 text-xs leading-5">
          <p><span className="text-[var(--duties-tertiary)]">ID：</span><span className="font-mono">{form.id}</span></p>
          <label className="block">
            <span className={labelClass}>cron</span>
            <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, cron: event.target.value }))} placeholder="0 0 * * *" value={form.cron} />
          </label>
          <label className="block">
            <span className={labelClass}>type</span>
            <select className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, type: event.target.value as ScheduleFormState['type'] }))} value={form.type}>
              <option value="message">message</option>
              <option value="script">script</option>
            </select>
          </label>
          <label className="block">
            <span className={labelClass}>text</span>
            <textarea className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, text: event.target.value }))} rows={4} value={form.text} />
          </label>
          {form.type === 'script' && (
            <div className="space-y-3">
              <label className="block">
                <span className={labelClass}>command</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, command: event.target.value }))} value={form.command} />
              </label>
              <label className="block">
                <span className={labelClass}>timeout</span>
                <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, timeout: event.target.value }))} value={form.timeout} />
              </label>
              <label className="flex items-center gap-2">
                <input checked={form.silent} onChange={(event) => setForm((current) => current && ({ ...current, silent: event.target.checked }))} type="checkbox" />
                <span>silent</span>
              </label>
            </div>
          )}
          <label className="flex items-center gap-2">
            <input checked={form.enabled} onChange={(event) => setForm((current) => current && ({ ...current, enabled: event.target.checked }))} type="checkbox" />
            <span>enabled</span>
          </label>
          <label className="flex items-center gap-2">
            <input checked={form.once} onChange={(event) => setForm((current) => current && ({ ...current, once: event.target.checked }))} type="checkbox" />
            <span>once</span>
          </label>
          <details className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">
            <summary className="cursor-pointer font-mono text-[0.65rem] text-[var(--duties-tertiary)]">高级字段</summary>
            <div className="mt-3 space-y-3">
              {(['conversation_key', 'entry_node_id', 'workflow_id'] as const).map((key) => (
                <label className="block" key={key}>
                  <span className={labelClass}>{key}</span>
                  <input className={fieldClass} onChange={(event) => setForm((current) => current && ({ ...current, [key]: event.target.value }))} value={form[key]} />
                </label>
              ))}
            </div>
          </details>
          <button className={buttonClass} disabled={saving} onClick={save} type="button">保存</button>
          {message && <p className="text-[var(--duties-tertiary)]">{message}</p>}
        </div>
      )}
    </PanelShell>
  );
};

export const AdvancedSettingsRightPanel = () => {
  const file = useSettingsSelectionStore(state => state.advancedFile);
  const descriptions: Record<typeof file, string> = {
    runtime: 'Runtime Config 控制引擎运行参数、入口节点、工具模式、记忆整理和进程管理等运行时行为。',
    policy: 'Policy 控制工具调用、文件访问、命令执行等安全策略。错误配置可能导致审批行为改变。',
  };
  return (
    <PanelShell eyebrow="高级" title="当前编辑文件说明">
      <p className="text-xs leading-5 text-[var(--duties-secondary)]">{descriptions[file]}</p>
      <p className="mt-3 text-xs leading-5 text-[var(--duties-secondary)]">保存前会执行轻量检查；完整 YAML 与字段校验由后端读取配置时完成。</p>
    </PanelShell>
  );
};
