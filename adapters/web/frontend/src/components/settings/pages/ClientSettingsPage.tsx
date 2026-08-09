// [2026-06-01] Client-only settings page.
// Why: users need frontend-local controls for approval automation, title behavior,
// and rendering defaults without changing Supervisor policy. How: bind form controls
// directly to clientPrefsStore, which persists in browser localStorage. Purpose:
// each build or browser profile can keep independent preferences.
import { useEffect, useMemo, useState } from 'react';

import { getAllToolNames, getConfig, getNodes } from '../../../api/supervisorClient';
import {
  type ApprovalLevel,
  shouldAutoApproveTool,
  type TitleGenerationMode,
  useClientPrefsStore,
} from '../../../store/clientPrefsStore';
import { useSettingsStore } from '../../../store/settingsStore';
import type { NodeDef } from '../../../types';
import { inferToolRisk, riskClassName, riskLabel, type RiskLevel } from '../../../utils/toolRisk';

export function parseNodeList(nodes: NodeDef[]): NodeDef[] {
  // [2026-06-02] Parse real Supervisor nodes into selectable entry nodes.
  // Why: the Client page must not show system nodes or delegated child workers as
  // default conversation entries. How: collect every delegate_targets reference, then
  // keep only AI nodes whose id is not system.* and is not referenced as a delegate.
  // Purpose: the first Client setting presents the actual root entry-point choices.
  const delegated = new Set<string>();
  for (const n of nodes) {
    if (n.delegate_targets) {
      for (const t of n.delegate_targets) delegated.add(t);
    }
  }
  return nodes.filter(n =>
    n.type === 'ai' &&
    !n.id.startsWith('system.') &&
    !delegated.has(n.id)
  );
}

function configuredEntryNodeId(config: Awaited<ReturnType<typeof getConfig>> | null, storedEntryNodeId: string): string {
  // [2026-06-02] Prefer the backend's configured entry node when present.
  // Why: browser localStorage can be stale or empty after a new deployment. How: read
  // direct, legacy/default, and nested shell entry-node fields from /v1/config, then
  // fall back to the settings store value. Purpose: the selected option reflects real
  // Supervisor configuration whenever that endpoint exposes it.
  return String(config?.entry_node_id || config?.default_entry_node_id || config?.shell?.entry_node_id || storedEntryNodeId || '').trim();
}

interface ToolRuleRow {
  toolName: string;
  label: string;
  risk: RiskLevel;
  description: string;
}

interface KnownToolInfo {
  toolName: string;
  label: string;
  description: string;
}

interface ToolCategoryDef {
  id: string;
  title: string;
  description: string;
  toolNames: string[];
}

const KNOWN_TOOL_RULES: KnownToolInfo[] = [
  // [2026-06-05] Keep curated labels and Chinese descriptions for common tools.
  // Why: the settings page now groups tools by smart-mode refinement categories, and
  // many users see this page before the authenticated backend tool list is loaded.
  // How: include the common built-in tools with descriptions that match the new smart
  // default. Purpose: the fallback UI is useful while dynamic backend tools still join
  // the same categories when they arrive.
  { toolName: 'read_file', label: 'read_file', description: '读取项目文件。智能模式默认自动放行；后端仍会按路径策略拦截不允许的读取。' },
  { toolName: 'write_file', label: 'write_file', description: '创建或覆盖文件。智能模式默认自动放行；后端仍会按工作区和信任目录策略审批。' },
  { toolName: 'apply_diff', label: 'apply_diff', description: '修改现有文件。智能模式默认自动放行；后端仍会按写入策略审批敏感路径。' },
  { toolName: 'list_dir', label: 'list_dir', description: '列出目录内容。智能模式默认自动放行。' },
  { toolName: 'grep', label: 'grep', description: '搜索源码文件。智能模式默认自动放行。' },
  { toolName: 'execute_command', label: 'execute_command', description: '执行 Shell 命令。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'save_memory', label: 'save_memory', description: '保存长期记忆。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'list_memories', label: 'list_memories', description: '列出长期记忆。智能模式默认自动放行。' },
  { toolName: 'delete_memory', label: 'delete_memory', description: '删除长期记忆。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'request_restart', label: 'request_restart', description: '请求重启服务。智能模式固定需要手动审批；只有全部放行模式会自动允许。' },
  { toolName: 'switch_node', label: 'switch_node', description: '切换当前会话节点。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'compact_context', label: 'compact_context', description: '压缩当前对话上下文。智能模式默认自动放行。' },
  { toolName: 'clear_context', label: 'clear_context', description: '清空指定频道上下文。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'cancel_active_tasks', label: 'cancel_active_tasks', description: '取消当前会话中的活跃下游任务。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'get_context_window', label: 'get_context_window', description: '读取当前上下文窗口使用量。智能模式默认自动放行。' },
  { toolName: 'clonoth_debug', label: 'clonoth_debug', description: '查询内部调试信息。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'create_or_update_skill', label: 'create_or_update_skill', description: '创建或更新本地技能。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'list_skills', label: 'list_skills', description: '列出本地技能。智能模式默认自动放行。' },
  { toolName: 'delete_skill', label: 'delete_skill', description: '删除本地技能。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'create_schedule', label: 'create_schedule', description: '创建定时任务。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'list_schedules', label: 'list_schedules', description: '列出定时任务。智能模式默认自动放行。' },
  { toolName: 'delete_schedule', label: 'delete_schedule', description: '删除定时任务。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'create_or_update_tool', label: 'create_or_update_tool', description: '创建或更新外部工具。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'reload_tools', label: 'reload_tools', description: '重载工具目录和 MCP 工具。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'create_or_update_mcp_client', label: 'create_or_update_mcp_client', description: '创建或更新 MCP 客户端配置。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'list_mcp_clients', label: 'list_mcp_clients', description: '列出 MCP 客户端配置。智能模式默认自动放行。' },
  { toolName: 'delete_mcp_client', label: 'delete_mcp_client', description: '删除 MCP 客户端配置。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'create_agent', label: 'create_agent', description: '创建持久化子节点。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'preempt_task', label: 'preempt_task', description: '向运行中的子任务追加指令。智能模式默认自动放行，可以在这里关闭。' },
  { toolName: 'read_image', label: 'read_image', description: '分析本地图片。智能模式默认自动放行。' },
  { toolName: 'discord_manage', label: 'discord_manage', description: '执行 Discord Bot 管理代码。智能模式默认自动放行，可以在这里关闭。' },
];

const KNOWN_TOOL_INFO_BY_NAME = new Map(KNOWN_TOOL_RULES.map((rule) => [rule.toolName, rule]));
const KNOWN_TOOL_NAMES = KNOWN_TOOL_RULES.map((rule) => rule.toolName);

const TOOL_CATEGORY_DEFS: ToolCategoryDef[] = [
  // [2026-06-05] Why: the old flat checkbox list did not scale once backend tools grew.
  // How: keep fixed category order and seed each category with known tools, while the
  // classifier below handles dynamic names. Purpose: the smart refinement panel stays
  // compact and predictable.
  { id: 'files', title: '📄 文件操作', description: '读取、搜索、列目录和修改文件。', toolNames: ['read_file', 'write_file', 'apply_diff', 'list_dir', 'grep'] },
  { id: 'commands', title: '💻 命令执行', description: '本地命令执行。', toolNames: ['execute_command'] },
  { id: 'memory', title: '🔖 记忆管理', description: '保存、查看和删除长期记忆。', toolNames: ['save_memory', 'list_memories', 'delete_memory'] },
  { id: 'system', title: '⚙️ 系统管理', description: '上下文、节点切换、工具、技能、定时任务和服务管理。', toolNames: ['request_restart', 'switch_node', 'compact_context', 'clear_context', 'cancel_active_tasks', 'get_context_window', 'clonoth_debug', 'create_or_update_skill', 'list_skills', 'delete_skill', 'create_schedule', 'list_schedules', 'delete_schedule', 'create_or_update_tool', 'reload_tools', 'create_or_update_mcp_client', 'list_mcp_clients', 'delete_mcp_client'] },
  { id: 'delegation', title: '🤖 节点委派', description: '创建节点、委派任务和向运行任务追加指令。', toolNames: ['create_agent', 'preempt_task'] },
  { id: 'external', title: '🧩 外部工具', description: 'MCP、图片分析、Discord 和其他外部服务工具。', toolNames: ['read_image', 'discord_manage'] },
  { id: 'other', title: '🔧 其他', description: '无法归入上述分类的工具。', toolNames: [] },
];

const APPROVAL_LEVEL_OPTIONS: Array<{ value: ApprovalLevel; label: string; description: string }> = [
  // [2026-06-05] Why: users now choose one of three approval safety levels. How: keep
  // the display labels and descriptions in a typed list used by the radio group.
  // Purpose: copy and behavior stay aligned when the settings section renders.
  { value: 'manual', label: '手动审批', description: '所有工具操作都需要手动确认。最安全。' },
  { value: 'smart', label: '智能放行', description: '除重启外自动放行；后端路径策略仍然生效。推荐。' },
  { value: 'yolo', label: '全部放行', description: '包括重启在内全部自动放行。' },
];

const TITLE_OPTIONS: Array<{ value: TitleGenerationMode; label: string; description: string }> = [
  { value: 'auto', label: '由模型生成', description: '在支持此模式时，请助手为对话生成标题。' },
  { value: 'manual', label: '手动输入', description: '保持标题不变，直到用户手动编辑。' },
  { value: 'first-message', label: '首条消息', description: '使用首条消息文本，最多保留 50 个字符。' },
];

function toolListFromApi(names: string[]): string[] {
  // [2026-06-01] Why: the backend may return duplicated, empty, or unsorted names
  // as tools are registered from multiple sources. How: trim, de-duplicate, and sort
  // in one small helper. Purpose: the approval settings list remains stable and does
  // not show malformed rows from transient registry data.
  return Array.from(new Set(names.map((name) => name.trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b));
}

function getToolCategoryId(toolName: string): string {
  // [2026-06-05] Why: backend tools can arrive with names that were not known when
  // this frontend was built. How: first honor the fixed category tool lists, then use
  // stable prefixes for delegated and external tools. Purpose: newly registered tools
  // land in a useful collapsed group instead of recreating the old flat list.
  const directCategory = TOOL_CATEGORY_DEFS.find((category) => category.toolNames.includes(toolName));
  if (directCategory) return directCategory.id;
  if (toolName.startsWith('dispatch_to_')) return 'delegation';
  if (toolName.startsWith('mcp_')) return 'external';
  if (toolName.startsWith('x_') || toolName.endsWith('_image') || toolName.includes('gelbooru') || toolName.includes('github')) return 'external';
  return 'other';
}

function sortToolRowsForCategory(category: ToolCategoryDef, rows: ToolRuleRow[]): ToolRuleRow[] {
  // [2026-06-05] Why: each category should keep important built-in tools near the
  // top while dynamic tools remain easy to scan. How: use the category seed order
  // before falling back to alphabetical sorting. Purpose: counts and rows stay stable
  // across backend registry reloads.
  const order = new Map(category.toolNames.map((toolName, index) => [toolName, index]));
  return [...rows].sort((a, b) => {
    const aOrder = order.get(a.toolName) ?? Number.MAX_SAFE_INTEGER;
    const bOrder = order.get(b.toolName) ?? Number.MAX_SAFE_INTEGER;
    if (aOrder !== bOrder) return aOrder - bOrder;
    return a.toolName.localeCompare(b.toolName);
  });
}

function createToolRuleRow(toolName: string): ToolRuleRow {
  // [2026-06-05] Why: dynamic backend names need the same row shape as curated tools.
  // How: merge known copy when available and otherwise generate a conservative generic
  // description. Purpose: every registered tool can be refined without a frontend edit.
  const known = KNOWN_TOOL_INFO_BY_NAME.get(toolName);
  return {
    toolName,
    label: known?.label || toolName,
    risk: inferToolRisk(toolName),
    description: known?.description || '后端返回的工具。智能模式默认自动放行，可以在这里关闭。',
  };
}

function ToolRuleToggle({ rule, checked, disabled = false, onChange }: { rule: ToolRuleRow; checked: boolean; disabled?: boolean; onChange: (enabled: boolean) => void }) {
  return (
    <label
      className={`flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 ${disabled ? 'opacity-70' : ''}`}
      key={rule.toolName}
    >
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs font-semibold text-[var(--duties-text)]">{rule.label}</span>
          <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[0.55rem] uppercase tracking-[0.12em] ${riskClassName(rule.risk)}`}>
            {riskLabel(rule.risk)}
          </span>
        </span>
        <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">{rule.description}</span>
      </span>
      <input
        aria-label={`自动放行 ${rule.toolName}`}
        checked={checked}
        className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
        disabled={disabled}
        onChange={(event) => {
          // [2026-06-05] Why: request_restart is intentionally locked off in smart
          // mode even if a stale stored rule says otherwise. How: ignore checkbox
          // changes while disabled. Purpose: the UI cannot imply that Level 2 will
          // auto-approve restarts.
          if (!disabled) onChange(event.target.checked);
        }}
        type="checkbox"
      />
    </label>
  );
}

export const ClientSettingsPage = () => {
  const {
    approvalLevel,
    autoApproveTools,
    titleGeneration,
    thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed,
    setApprovalLevel,
    setAutoApproveTool,
    setTitleGeneration,
    setThinkingDefaultCollapsed,
    setToolResultsDefaultCollapsed,
  } = useClientPrefsStore();
  const { adminToken, entryNodeId, setEntryNodeId } = useSettingsStore();
  const [allToolNames, setAllToolNames] = useState<string[]>([]);
  const [entryNodes, setEntryNodes] = useState<NodeDef[]>([]);
  const [expandedApprovalCategories, setExpandedApprovalCategories] = useState<Record<string, boolean>>({});

  useEffect(() => {
    // [2026-06-01] Why: approval rules must reflect every tool registered by the
    // running Supervisor, but the API requires admin auth and can fail. How: fetch
    // on mount/token change, keep only valid names, and clear dynamic names on
    // failure. Purpose: authenticated users see the complete tool list while
    // unauthenticated users safely fall back to the built-in categorized rows.
    if (!adminToken) {
      setAllToolNames([]);
      setEntryNodes([]);
      return;
    }
    let cancelled = false;
    getAllToolNames(adminToken)
      .then((names) => {
        if (!cancelled) setAllToolNames(toolListFromApi(names));
      })
      .catch(() => {
        if (!cancelled) setAllToolNames([]);
      });
    Promise.all([
      getNodes(adminToken),
      getConfig().catch(() => null),
    ])
      .then(([nodes, config]) => {
        if (cancelled) return;
        const parsedNodes = parseNodeList(nodes);
        setEntryNodes(parsedNodes);
        const configured = configuredEntryNodeId(config, entryNodeId);
        // [2026-06-02] Sync the selected entry node after loading the real list.
        // Why: the select should display the backend or store default only when that
        // id exists in the filtered entry-node list. How: choose the matching id, or
        // fall back to the first root entry when no stored value exists. Purpose: the
        // first Client setting never points at a hidden child/system node.
        if (configured && parsedNodes.some((node) => node.id === configured)) {
          setEntryNodeId(configured);
        } else if (!configured && parsedNodes[0]) {
          setEntryNodeId(parsedNodes[0].id);
        }
      })
      .catch(() => {
        if (!cancelled) setEntryNodes([]);
      });
    return () => {
      cancelled = true;
    };
  }, [adminToken, setEntryNodeId]);

  const allToolRules = useMemo(
    () => toolListFromApi([...KNOWN_TOOL_NAMES, ...allToolNames]).map(createToolRuleRow),
    [allToolNames],
  );

  const categorizedToolRules = useMemo(() => {
    // [2026-06-05] Why: smart-mode refinements need collapsed category panels instead
    // of one long list. How: group every built-in and backend tool by category id, then
    // sort each group with the fixed seed order. Purpose: the UI can show N/M counts
    // while keeping each category closed by default.
    const grouped = new Map<string, ToolRuleRow[]>();
    for (const rule of allToolRules) {
      const categoryId = getToolCategoryId(rule.toolName);
      grouped.set(categoryId, [...(grouped.get(categoryId) || []), rule]);
    }
    return TOOL_CATEGORY_DEFS
      .map((category) => ({
        ...category,
        rows: sortToolRowsForCategory(category, grouped.get(category.id) || []),
      }))
      .filter((category) => category.rows.length > 0);
  }, [allToolRules]);

  const toggleApprovalCategory = (categoryId: string) => {
    // [2026-06-05] Why: every approval category must be collapsed by default but
    // independently expandable. How: store a small boolean map keyed by category id.
    // Purpose: expanding one group does not disturb the rest of the settings page.
    setExpandedApprovalCategories((current) => ({ ...current, [categoryId]: !current[categoryId] }));
  };

  return (
    <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto max-w-3xl space-y-6">
        <header>
          <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">前端本地</p>
          <h1 className="mt-2 font-mono text-xl font-semibold tracking-[-0.04em]">客户端设置</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">
            这些偏好只保存在当前浏览器中，不会修改后端策略、共享会话状态或服务器配置。
          </p>
        </header>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">入口节点</h2>
          <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
            新对话消息默认由此节点处理。仅显示根入口节点，不包含子节点。
          </p>
          <select
            aria-label="入口节点"
            className="mt-3 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={(e) => setEntryNodeId(e.target.value)}
            value={entryNodeId}
          >
            {entryNodes.length === 0 && <option value={entryNodeId}>{entryNodeId || '加载中...'}</option>}
            {entryNodes.map((n) => (
              <option key={n.id} value={n.id}>{n.name || n.id}{n.description ? ` — ${n.description}` : ''}</option>
            ))}
          </select>
          <p className="mt-2 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">当前值：{entryNodeId || '（未设置）'}</p>
        </section>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <div className="mb-3">
            <h2 className="font-mono text-sm font-semibold">自动审批规则</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
              选择前端本地自动审批等级。后端的工作区、信任目录和其他安全策略仍然会继续生效。
            </p>
          </div>

          <div aria-label="审批等级" className="space-y-2" role="radiogroup">
            {APPROVAL_LEVEL_OPTIONS.map((option) => (
              <label
                className={`flex items-start gap-3 border p-3 ${approvalLevel === option.value ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`}
                key={option.value}
              >
                <input
                  checked={approvalLevel === option.value}
                  className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
                  name="approval-level"
                  onChange={() => {
                    // [2026-06-05] Why: the radio group is the primary approval
                    // control. How: write the selected level through clientPrefsStore.
                    // Purpose: all approval renderers and chatStore immediately use
                    // the same persisted safety level.
                    setApprovalLevel(option.value);
                  }}
                  type="radio"
                />
                <span>
                  <span className="block font-mono text-xs font-semibold text-[var(--duties-text)]">{option.label}</span>
                  <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">{option.description}</span>
                </span>
              </label>
            ))}
          </div>

          {approvalLevel === 'smart' && (
            <div className="mt-4 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
              <div className="mb-3">
                <h3 className="font-mono text-xs font-semibold text-[var(--duties-text)]">智能放行细化</h3>
                <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
                  Level 2 默认自动放行除重启外的工具。展开分类后，可以关闭某个工具的自动放行。
                </p>
              </div>
              <div className="space-y-2">
                {categorizedToolRules.map((category) => {
                  const expanded = Boolean(expandedApprovalCategories[category.id]);
                  const approvedCount = category.rows.filter((rule) => shouldAutoApproveTool(rule.toolName, autoApproveTools, 'smart')).length;
                  return (
                    <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)]" key={category.id}>
                      <button
                        aria-expanded={expanded}
                        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
                        onClick={() => toggleApprovalCategory(category.id)}
                        type="button"
                      >
                        <span className="min-w-0">
                          <span className="block font-mono text-xs font-semibold text-[var(--duties-text)]">{category.title}</span>
                          <span className="mt-0.5 block text-xs leading-5 text-[var(--duties-secondary)]">{category.description}</span>
                        </span>
                        <span className="flex flex-shrink-0 items-center gap-2 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
                          <span>{approvedCount}/{category.rows.length} 已放行</span>
                          <span>{expanded ? '收起' : '展开'}</span>
                        </span>
                      </button>
                      {expanded && (
                        <div className="space-y-2 border-t border-[var(--duties-border)] p-2">
                          {category.rows.map((rule) => {
                            const checked = shouldAutoApproveTool(rule.toolName, autoApproveTools, 'smart');
                            const disabled = rule.toolName === 'request_restart';
                            return (
                              <ToolRuleToggle
                                checked={checked}
                                disabled={disabled}
                                key={rule.toolName}
                                onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                                rule={rule}
                              />
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </section>


        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">对话标题</h2>
          <label className="mt-3 block text-xs font-semibold text-[var(--duties-secondary)]" htmlFor="client-title-generation">
            生成方式
          </label>
          <select
            aria-label="对话标题生成方式"
            className="mt-1 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            id="client-title-generation"
            onChange={(event) => setTitleGeneration(event.target.value as TitleGenerationMode)}
            value={titleGeneration}
          >
            {TITLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <p className="mt-2 text-xs leading-5 text-[var(--duties-secondary)]">
            {TITLE_OPTIONS.find((option) => option.value === titleGeneration)?.description}
          </p>
        </section>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">消息显示</h2>
          <div className="mt-3 space-y-3">
            <label className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
              <span>
                <span className="block font-mono text-xs font-semibold">默认折叠思考内容</span>
                <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">启用后，新的思考内容块会默认折叠。</span>
              </span>
              <input
                aria-label="默认折叠思考内容"
                checked={thinkingDefaultCollapsed}
                className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
                onChange={(event) => setThinkingDefaultCollapsed(event.target.checked)}
                type="checkbox"
              />
            </label>

            <label className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
              <span>
                <span className="block font-mono text-xs font-semibold">默认折叠工具结果</span>
                <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">启用后，工具参数和结果详情会默认折叠。</span>
              </span>
              <input
                aria-label="默认折叠工具结果"
                checked={toolResultsDefaultCollapsed}
                className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
                onChange={(event) => setToolResultsDefaultCollapsed(event.target.checked)}
                type="checkbox"
              />
            </label>
          </div>
        </section>
      </div>
    </section>
  );
};
