// [2026-05-31] ToolCallCard renders normalized tool executions for MessageCard v2.
// Why: the old ToolCallRow only understood finished legacy tool calls and could not
// show argument streaming, queued tools, async task starts, or hidden control tools.
// How: map every ToolStatus to compact styling, show one-line summaries, and expose
// structured arguments/results in a collapsible details panel. Purpose: keep tool
// progress visible inside the same message timeline that owns the text output.
import { useMemo, useState, type ReactNode } from 'react';

import { decideApproval } from '../../api/supervisorClient';
import { autoApprovedApprovalIds } from '../../store/approvalManager';
import { useClientPrefsStore } from '../../store/clientPrefsStore';
import type { ToolExecution, ToolStatus } from '../../types/message';
import { Icon } from '../common';
import { INLINE_BLOCK_BODY_TEXT_CLASS, INLINE_BLOCK_HEADER_TEXT_CLASS } from './renderingConstants';

interface ToolCallCardProps {
  tool: ToolExecution;
}

interface StatusStyle {
  // Why: the compact tool header renders status as a single Material Symbol icon.
  // How: keep only the icon name and icon color classes that ToolHeaderButton uses.
  // Purpose: STATUS_STYLES no longer carries old badge labels or wrapper styles that
  // the current architecture does not render.
  icon: string;
  iconClassName: string;
  spin?: boolean;
}

interface DisplayText {
  text: string;
  sizeLabel: string;
}

interface ReadFileSection {
  path: string;
  content: string;
}

interface ListDirTree {
  directories: Record<string, unknown>[];
  totalFiles?: number;
  totalDirs?: number;
}

interface CommandResult {
  returnCode: number;
  output: string;
}

const DETAIL_PREVIEW_CHAR_LIMIT = 10000;
const PREVIEW_PRE_CLASS = 'max-h-56 overflow-auto whitespace-pre-wrap break-words bg-black/5 p-2';
// Why: approval rows and expanded tool details share the same compact monospace size.
// How: keep the Tailwind value as a local constant because it is specific to ToolCallCard.
// Purpose: the card avoids repeated magic values while preserving the exact current layout.
const TOOL_DETAIL_TEXT_CLASS = 'text-[0.66rem]';

const STATUS_STYLES: Record<ToolStatus, StatusStyle> = {
  args_streaming: {
    icon: 'progress_activity',
    iconClassName: 'text-blue-600',
    spin: true,
  },
  queued: {
    icon: 'pending',
    iconClassName: 'text-[var(--duties-tertiary)]',
  },
  running: {
    icon: 'progress_activity',
    iconClassName: 'text-blue-600',
    spin: true,
  },
  awaiting_approval: {
    icon: 'verified_user',
    iconClassName: 'text-orange-600',
  },
  async_started: {
    icon: 'open_in_new',
    iconClassName: 'text-indigo-600',
  },
  success: {
    icon: 'check_circle',
    iconClassName: 'text-green-600',
  },
  error: {
    icon: 'error',
    iconClassName: 'text-red-600',
  },
  cancelled: {
    icon: 'cancel',
    iconClassName: 'text-gray-500',
  },
};

function getByteLength(value: string): number {
  // Why: expanded tool details now preview large payloads instead of hard-truncating
  // them. How: calculate UTF-8 bytes from the original full string. Purpose: users can
  // see the true payload size even when the visible preview is capped at 10000 chars.
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).length;
  }
  return value.length;
}

function formatByteSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function getPreviewText(text: string, showFull: boolean): string {
  const chars = Array.from(text);
  if (showFull || chars.length <= DETAIL_PREVIEW_CHAR_LIMIT) return text;

  // Why: rendering arbitrarily large details can freeze the chat timeline. How: show
  // a 10000-character preview and let the user opt in to the full payload. Purpose:
  // remove the old irreversible 2000-character truncation without losing performance.
  return chars.slice(0, DETAIL_PREVIEW_CHAR_LIMIT).join('');
}

function isPreviewLimited(text: string): boolean {
  return Array.from(text).length > DETAIL_PREVIEW_CHAR_LIMIT;
}

function createDisplayText(text: string): DisplayText | null {
  if (!text) return null;
  return {
    text,
    sizeLabel: `[${formatByteSize(getByteLength(text))}]`,
  };
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function getArgumentsText(tool: ToolExecution): string {
  if (tool.argumentsText) return tool.argumentsText;
  return stringifyValue(tool.arguments);
}

function getDataField(tool: ToolExecution): Record<string, unknown> | null {
  // Why: backend tool responses now use a unified { ok, data, error } envelope. How:
  // safely unwrap only object-shaped result.data values. Purpose: every renderer can
  // prefer current structured fields while preserving legacy raw_inline fallbacks.
  const result = tool.result;
  if (isRecord(result) && isRecord(result.data)) {
    return result.data;
  }
  return null;
}

function getLegacyResultRecord(tool: ToolExecution): Record<string, unknown> | null {
  // Why: older stored conversations may still have structure at result.* instead of
  // result.data.*. How: expose the top-level result object only when it is not the new
  // envelope. Purpose: keep historical tool cards readable after the backend change.
  if (isRecord(tool.result) && !isRecord(tool.result.data)) return tool.result;
  return null;
}

function getReadableDataResult(tool: ToolExecution): string {
  // Why: data.result is the backend-provided human-readable result string. How: return
  // it only when it is already text. Purpose: unknown tools show useful text instead
  // of dumping the full unified envelope as JSON.
  const data = getDataField(tool);
  const dataResult = data?.result;
  return typeof dataResult === 'string' ? dataResult : '';
}

function getRawResultText(tool: ToolExecution): string {
  return getReadableDataResult(tool) || stringifyValue(tool.rawInline || tool.result);
}

function getResultText(tool: ToolExecution): string {
  // Why: unified tool responses moved readable output from top-level result/raw_inline
  // into result.data.result. How: error remains first, then data.result, then legacy
  // raw_inline/result. Purpose: collapsed size labels and expanded fallback previews
  // match the text users should read for both new and old tool events.
  if (tool.error) return tool.error;
  const readableResult = getReadableDataResult(tool);
  if (readableResult) return readableResult;
  if (tool.rawInline) return tool.rawInline;
  return stringifyValue(tool.result);
}

function getCompactPreview(text: string): string {
  const compact = text.replace(/\s+/g, ' ').trim();
  if (compact.length <= 120) return compact;
  return `${compact.slice(0, 120)}…`;
}

function getStatusStyle(tool: ToolExecution): StatusStyle {
  if (tool.control && (tool.rejected || tool.status === 'error')) {
    return STATUS_STYLES.error;
  }

  return STATUS_STYLES[tool.status];
}

// Why: each known tool type needs a stable icon, display name, and compact
// description source. How: keep static metadata in TOOL_META_MAP and resolve
// description templates from the current ToolExecution arguments at render time.
// Purpose: the collapsed tool row stays readable without spreading tool-specific
// formatting logic through the component body.
interface ToolMeta {
  icon: string;
  label: string;
  descTemplate: string;
}

const TOOL_META_MAP: Record<string, ToolMeta> = {
  read_file: { icon: 'description', label: '阅读', descTemplate: '' },
  write_file: { icon: 'edit_document', label: '写入', descTemplate: '' },
  apply_diff: { icon: 'difference', label: '修改', descTemplate: '' },
  execute_command: { icon: 'terminal', label: '执行', descTemplate: '' },
  grep: { icon: 'search', label: '搜索文件', descTemplate: '' },
  search_in_files: { icon: 'search', label: '搜索文件', descTemplate: '' },
  list_dir: { icon: 'folder_open', label: '列出', descTemplate: '' },
  reply: { icon: 'chat_bubble', label: '中间回复', descTemplate: '' },
  finish: { icon: 'check_circle', label: '完成', descTemplate: '' },
  ask: { icon: 'help', label: '提问', descTemplate: '' },
  save_memory: { icon: 'bookmark', label: '写入记忆', descTemplate: '' },
  list_memories: { icon: 'bookmarks', label: '罗列记忆', descTemplate: '' },
  delete_memory: { icon: 'bookmark_remove', label: '删除记忆', descTemplate: '' },
  create_or_update_skill: { icon: 'build', label: '技能管理', descTemplate: '' },
  list_skills: { icon: 'bookmarks', label: '列出技能', descTemplate: '' },
  delete_skill: { icon: 'playlist_remove', label: '删除技能', descTemplate: '' },
  create_schedule: { icon: 'schedule', label: '创建定时', descTemplate: '' },
  list_schedules: { icon: 'calendar_month', label: '列出定时', descTemplate: '' },
  delete_schedule: { icon: 'event_busy', label: '删除定时', descTemplate: '' },
  create_or_update_tool: { icon: 'build', label: '工具管理', descTemplate: '' },
  reload_tools: { icon: 'refresh', label: '重载工具', descTemplate: '' },
  request_restart: { icon: 'restart_alt', label: '请求重启', descTemplate: '' },
  switch_node: { icon: 'swap_horiz', label: '切换节点', descTemplate: '' },
  compact_context: { icon: 'compress', label: '压缩上下文', descTemplate: '' },
  clear_context: { icon: 'delete_sweep', label: '清空上下文', descTemplate: '' },
  cancel_active_tasks: { icon: 'cancel', label: '取消任务', descTemplate: '' },
  preempt_task: { icon: 'priority_high', label: '追加指令', descTemplate: '' },
  get_context_window: { icon: 'data_usage', label: '上下文窗口', descTemplate: '' },
  read_video: { icon: 'movie', label: '读取视频', descTemplate: '' },

  discord_manage: { icon: 'smart_toy', label: 'Discord', descTemplate: '' },
  clonoth_debug: { icon: 'bug_report', label: '调试', descTemplate: '' },
  create_agent: { icon: 'person_add', label: '创建节点', descTemplate: '' },

};

const DEFAULT_TOOL_META: ToolMeta = { icon: 'handyman', label: '', descTemplate: '' };
const TOOL_DESC_PLACEHOLDER_PATTERN = /\{(\w+)\}/g;
const TOOL_DESC_VALUE_CHAR_LIMIT = 60;

// ---------------------------------------------------------------------------
//  Tool-specific inline summaries — replaces generic descTemplate resolution
// ---------------------------------------------------------------------------

function basename(p: string): string {
  const s = p.replace(/\\/g, '/');
  return s.split('/').pop() || s;
}

function shortPath(p: string, maxLen = 40): string {
  if (!p) return '';
  if (p.length <= maxLen) return p;
  const name = basename(p);
  if (name.length >= maxLen - 4) return `…${name.slice(-(maxLen - 1))}`;
  const prefix = p.slice(0, maxLen - name.length - 2);
  return `${prefix}…/${name}`;
}

function extractCoreCommand(cmd: string): string {
  if (!cmd) return '';
  // Strip leading 'cd xxx &&' chains
  let s = cmd.replace(/^(?:cd\s+\S+\s*&&\s*)+/gi, '').trim();
  // Strip trailing pipes
  s = s.replace(/\s*\|\s*(?:head|tail|grep|wc|sort|uniq|tee)\b.*$/i, '').trim();
  // Strip trailing '&& echo xxx'
  s = s.replace(/\s*&&\s*echo\b.*$/i, '').trim();
  // Strip trailing '2>&1'
  s = s.replace(/\s*2>&1\s*$/, '').trim();
  if (s.length > 50) s = `${s.slice(0, 47)}…`;
  return s;
}

function getToolInlineSummary(tool: ToolExecution): string {
  const args = getToolTemplateArguments(tool);
  const name = tool.name;

  switch (name) {
    case 'read_file': {
      const files = Array.isArray(args.files) ? args.files.filter(isRecord) : [];
      if (files.length > 1) return `阅读 ${files.length} 文件`;
      const p = getStringValue(args.path || (files[0] as Record<string, unknown>)?.path);
      if (!p) return '';
      const sl = getNumberValue(args.start_line || args.startLine || (files[0] as Record<string, unknown>)?.startLine);
      const el = getNumberValue(args.end_line || args.endLine || (files[0] as Record<string, unknown>)?.endLine);
      const fn = basename(p);
      if (sl !== undefined && el !== undefined) return `阅读 ${fn} L${sl}-${el}`;
      if (sl !== undefined) return `阅读 ${fn} L${sl}+`;
      return `阅读 ${fn}`;
    }
    case 'write_file': {
      const p = getStringValue(args.path);
      return p ? `写入 ${basename(p)}` : '';
    }
    case 'apply_diff': {
      const p = getStringValue(args.path);
      const diffs = Array.isArray(args.diffs) ? args.diffs : [];
      const fn = p ? basename(p) : '';
      if (diffs.length > 0) return `修改 ${fn} · ${diffs.length} 处`;
      return fn ? `修改 ${fn}` : '';
    }
    case 'execute_command': {
      const cmd = getStringValue(args.command);
      return extractCoreCommand(cmd);
    }
    case 'grep':
    case 'search_in_files': {
      const q = getStringValue(args.query);
      const p = getStringValue(args.path);
      const pat = getStringValue(args.pattern);
      const mode = getStringValue(args.mode);
      const qStr = q ? `"${q.length > 25 ? q.slice(0, 22) + '…' : q}"` : '';
      const scopeParts: string[] = [];
      if (pat && pat !== '**/*') scopeParts.push(pat);
      if (p && p !== '.') scopeParts.push(`${basename(p)}/`);
      const scope = scopeParts.length > 0 ? scopeParts.join(' ') : '';
      if (mode === 'replace') {
        return scope ? `在 ${scope} 中替换 ${qStr}` : `替换 ${qStr}`;
      }
      return scope ? `在 ${scope} 中搜索 ${qStr}` : `搜索 ${qStr}`;
    }
    case 'list_dir': {
      const p = getStringValue(args.path);
      const paths = Array.isArray(args.paths) ? args.paths.map(String) : [];
      if (paths.length > 1) return `列出 ${paths.length} 目录`;
      const target = p || (paths[0] ?? '');
      if (!target) return '';
      const parts = target.replace(/\\/g, '/').replace(/\/$/, '').split('/');
      return `列出 ${parts[parts.length - 1]}/`;
    }
    case 'save_memory': {
      const id = getStringValue(args.id);
      return id ? `写入记忆 ${id}` : '';
    }
    case 'list_memories': {
      const book = getStringValue(args.book);
      return book ? `罗列记忆 ${book}` : '罗列记忆';
    }
    case 'delete_memory': {
      const id = getStringValue(args.id);
      return id ? `删除记忆 ${id}` : '';
    }
    case 'create_or_update_skill': {
      const sn = getStringValue(args.name);
      return sn ? `技能 ${sn}` : '';
    }
    case 'delete_skill': {
      const sn = getStringValue(args.name);
      return sn ? `删除技能 ${sn}` : '';
    }
    case 'create_schedule': {
      const sid = getStringValue(args.id);
      return sid ? `创建定时 ${sid}` : '';
    }
    case 'delete_schedule': {
      const sid = getStringValue(args.id);
      return sid ? `删除定时 ${sid}` : '';
    }
    case 'request_restart': {
      const t = getStringValue(args.target) || 'all';
      return `重启 ${t}`;
    }
    case 'switch_node': {
      const t = getStringValue(args.target);
      return t ? `切换到 ${t}` : '';
    }
    case 'clear_context': {
      return getStringValue(args.channel_id);
    }
    case 'preempt_task': {
      const tid = getStringValue(args.task_id);
      return tid ? `追加指令 ${tid.slice(0, 8)}` : '';
    }
    case 'clonoth_debug': {
      return getStringValue(args.action);
    }
    case 'create_agent': {
      const an = getStringValue(args.name);
      return an ? `创建节点 ${an}` : '';
    }
    case 'create_or_update_tool': {
      const tn = getStringValue(args.name);
      return tn ? `工具 ${tn}` : '';
    }
    default: break;
  }

  // dispatch_to_* with instruction
  if (name.startsWith('dispatch_to_')) {
    const inst = getStringValue(args.instruction || args.text);
    return inst.length > 30 ? `${inst.slice(0, 27)}…` : inst;
  }

  // MCP tools: try to show first meaningful arg
  if (name.startsWith('mcp_')) {
    const keys = Object.keys(args).filter(k => k !== 'params');
    const firstVal = keys.length > 0 ? getStringValue(args[keys[0]]) : '';
    if (!firstVal && isRecord(args.params)) {
      const pk = Object.keys(args.params);
      const pv = pk.length > 0 ? getStringValue((args.params as Record<string, unknown>)[pk[0]]) : '';
      return pv.length > 40 ? `${pv.slice(0, 37)}…` : pv;
    }
    return firstVal.length > 40 ? `${firstVal.slice(0, 37)}…` : firstVal;
  }

  return '';
}

// ---------------------------------------------------------------------------
//  Tool result suffix — appended after completion
// ---------------------------------------------------------------------------

function getToolResultSuffix(tool: ToolExecution): string {
  if (tool.status !== 'success' && tool.status !== 'error') return '';
  const name = tool.name;
  const data = getDataField(tool) || getLegacyResultRecord(tool);

  switch (name) {
    case 'read_file': {
      if (!data) return '';
      const total = getNumberValue(data.totalCount);
      const success = getNumberValue(data.successCount);
      const fail = getNumberValue(data.failCount);
      const results = Array.isArray(data.results) ? data.results.filter(isRecord) : [];
      // Compute total bytes
      let totalBytes = 0;
      for (const r of results) {
        if (r.success) {
          const sz = getNumberValue(r.size);
          if (sz !== undefined) totalBytes += sz;
          else {
            const content = getStringValue(r.content);
            if (content) totalBytes += getByteLength(content);
          }
        }
      }
      const sizeStr = totalBytes > 0 ? ` · ${formatByteSize(totalBytes)}` : '';
      if (fail && fail > 0 && total && total > 1) return `${success}/${total} 成功 · ${fail} 失败`;
      if (total && total > 1) return `${total} 文件${sizeStr}`;
      return sizeStr ? sizeStr.slice(3) : ''; // strip leading " · "
    }
    case 'write_file': {
      if (!data) return '';
      const bytes = getNumberValue(data.bytes);
      // Compute +lines from content in arguments
      const wfArgs = getToolTemplateArguments(tool);
      const content = getStringValue(wfArgs.content);
      const lineCount = content ? content.split('\n').length : 0;
      const linePart = lineCount > 0 ? ` +${lineCount}` : '';
      const sizePart = bytes !== undefined ? formatByteSize(bytes) : '';
      return [sizePart, linePart].filter(Boolean).join('');
    }
    case 'apply_diff': {
      if (!data) return '';
      const applied = getNumberValue(data.appliedCount);
      const failed = getNumberValue(data.failedCount);
      const total = getNumberValue(data.diffCount);
      if (applied === undefined) return '';
      // Compute +lines/-lines from diffs in arguments
      const args = getToolTemplateArguments(tool);
      const diffs = Array.isArray(args.diffs) ? args.diffs.filter(isRecord) : [];
      let addedLines = 0;
      let removedLines = 0;
      for (const d of diffs) {
        const search = getStringValue(d.search);
        const replace = getStringValue(d.replace);
        if (search || replace) {
          const oldLines = search ? search.split('\n').length : 0;
          const newLines = replace ? replace.split('\n').length : 0;
          addedLines += Math.max(0, newLines - oldLines);
          removedLines += Math.max(0, oldLines - newLines);
        }
      }
      const diffStat = (addedLines > 0 || removedLines > 0) ? ` +${addedLines} -${removedLines}` : '';
      if (failed && failed > 0) return `${applied}/${total} 通过 · ${failed} 失败${diffStat}`;
      return `${applied}/${total} 通过${diffStat}`;
    }
    case 'execute_command': {
      const cmd = parseCommandResult(tool);
      if (!cmd) return '';
      return cmd.returnCode === 0 ? '✓ 0' : `✗ ${cmd.returnCode}`;
    }
    case 'grep':
    case 'search_in_files': {
      const sr = parseSearchResults(tool);
      if (!sr) return '';
      const mode = getStringValue(getToolTemplateArguments(tool).mode);
      if (mode === 'replace') {
        return `${sr.count} 处`;
      }
      const fileSet = new Set(sr.rows.map(r => getStringValue(r.file)).filter(Boolean));
      const filePart = fileSet.size > 0 ? ` · ${fileSet.size} 文件` : '';
      const truncPart = sr.truncated ? ' (截断)' : '';
      return `${sr.count} 结果${filePart}${truncPart}`;
    }
    case 'list_dir': {
      const tree = parseListDirTree(tool);
      if (!tree) return '';
      const parts: string[] = [];
      if (tree.totalFiles !== undefined) parts.push(`${tree.totalFiles} 文件`);
      if (tree.totalDirs !== undefined) parts.push(`${tree.totalDirs} 目录`);
      return parts.join(' · ');
    }
    default:
      return '';
  }
}

function getToolMeta(toolName: string): ToolMeta {
  if (TOOL_META_MAP[toolName]) return TOOL_META_MAP[toolName];
  if (toolName.startsWith('dispatch_to_')) {
    const target = toolName.replace('dispatch_to_', '').replace(/_/g, ' ');
    return { icon: 'send', label: `委派 ${target}`, descTemplate: '{instruction}' };
  }
  if (toolName.startsWith('mcp_')) {
    const provider = toolName.split('_')[1] || 'MCP';
    return { icon: 'extension', label: `MCP: ${provider}`, descTemplate: '' };
  }
  return DEFAULT_TOOL_META;
}

function getToolTemplateArguments(tool: ToolExecution): Record<string, unknown> {
  const directArgs = isRecord(tool.arguments) ? { ...tool.arguments } : {};
  if (Object.keys(directArgs).length > 0 || !tool.argumentsText) {
    return withDerivedTemplateArguments(directArgs);
  }

  try {
    const parsedArgs = JSON.parse(tool.argumentsText);
    return withDerivedTemplateArguments(isRecord(parsedArgs) ? { ...parsedArgs } : {});
  } catch {
    return directArgs;
  }
}

function withDerivedTemplateArguments(args: Record<string, unknown>): Record<string, unknown> {
  if (args.path) return args;

  const filePathSummary = getFilesPathSummary(args.files);
  return filePathSummary ? { ...args, path: filePathSummary } : args;
}

function getFilesPathSummary(files: unknown): string {
  if (!Array.isArray(files)) return '';

  const paths = files
    .filter(isRecord)
    .map((file) => getStringValue(file.path))
    .filter(Boolean);

  if (paths.length === 1) return paths[0];
  if (paths.length > 1) return `${paths[0]} +${paths.length - 1}`;
  return '';
}

function formatDescTemplateValue(value: unknown): string {
  if (value === undefined || value === null) return '';

  const text = String(value);
  if (text.length <= TOOL_DESC_VALUE_CHAR_LIMIT) return text;
  return `${text.slice(0, TOOL_DESC_VALUE_CHAR_LIMIT - 3)}…`;
}

function resolveDescTemplate(template: string, tool: ToolExecution): string {
  // Why: compact descriptions use templates such as {path} and {command}. How:
  // resolve placeholders from structured arguments, deriving path from files[].path
  // when the tool uses the batch file shape. Purpose: the header can summarize common
  // tools without rendering their full JSON arguments.
  if (!template) return '';

  const resolvedArgs = getToolTemplateArguments(tool);
  return template
    .replace(TOOL_DESC_PLACEHOLDER_PATTERN, (_match, key) => formatDescTemplateValue(resolvedArgs[key]))
    .replace(/\s+/g, ' ')
    .trim();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function getStringValue(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

function getNumberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function getBooleanValue(value: unknown): boolean {
  return value === true;
}

function getApprovalDetailsRecord(tool: ToolExecution): Record<string, unknown> {
  // Why: approval metadata is stored on ToolExecution as an untyped record so it can
  // preserve backend payloads across versions. How: unwrap approvalDetails.details
  // only when it is object-shaped. Purpose: the inline approval UI can display path
  // and reason without duplicating ApprovalBlock.
  const nested = isRecord(tool.approvalDetails?.details) ? tool.approvalDetails.details : undefined;
  return nested || {};
}

function getApprovalOperation(tool: ToolExecution): string {
  // Why: approvalDetails may come from old or new event shapes. How: prefer the
  // explicit operation field and fall back to the tool name. Purpose: pending approval
  // panels always show a useful operation label.
  const details = getApprovalDetailsRecord(tool);
  const detailToolName = getStringValue(details.tool_name) || getStringValue(details.tool) || getStringValue(details.name);
  const operation = isRecord(tool.approvalDetails) ? getStringValue(tool.approvalDetails.operation) : '';
  return detailToolName || operation || tool.name;
}

function getResultRecord(tool: ToolExecution): Record<string, unknown> | undefined {
  if (isRecord(tool.result)) return tool.result;
  if (!tool.rawInline) return undefined;

  try {
    const parsed = JSON.parse(tool.rawInline);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function parseReadFileData(tool: ToolExecution): ReadFileSection[] {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  const rows = Array.isArray(data?.results) ? data.results.filter(isRecord) : [];

  // Why: read_file now returns file entries as result.data.results instead of only a
  // printable transcript. How: convert object entries with path/content fields into
  // the same section shape used by the legacy parser. Purpose: structured new results
  // render as separate files and old raw_inline transcripts still work below.
  return rows
    .map((row, index) => ({
      path: getStringValue(row.path) || `文件-${index + 1}`,
      content: getStringValue(row.content),
    }))
    .filter((section) => section.path || section.content);
}

function parseReadFileSections(rawText: string): ReadFileSection[] {
  const matches = Array.from(rawText.matchAll(/^── (.*?) ──(?:[ \t]*(.*))?$/gm));
  if (matches.length === 0) return [];

  // Why: read_file returns a compact text transcript containing one or more file
  // headers. How: split on the backend header line and keep each header path beside
  // its own content. Purpose: expanded details read like files instead of one dump.
  return matches.map((match, index) => {
    const headerEnd = (match.index || 0) + match[0].length;
    const nextStart = matches[index + 1]?.index ?? rawText.length;
    const suffix = (match[2] || '').trim();
    const body = rawText.slice(headerEnd, nextStart).replace(/^\r?\n/, '').replace(/\r?\n$/, '');
    return {
      path: match[1] || '文件',
      content: suffix ? `${suffix}${body ? `\n${body}` : ''}` : body,
    };
  });
}

function parseCommandResult(tool: ToolExecution): CommandResult | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  const returnCode = getNumberValue(data?.returncode);
  if (returnCode !== undefined) {
    // Why: execute_command exposes status and transcript as
    // result.data.returncode/result.data.output. How: read those fields first, with
    // legacy top-level result support through getLegacyResultRecord. Purpose: users
    // see accurate status even when raw_inline is absent or stale.
    return {
      returnCode,
      output: getStringValue(data?.output),
    };
  }

  const rawText = stringifyValue(tool.rawInline || tool.result);
  const match = rawText.match(/^returncode=(-?\d+)\r?\n?([\s\S]*)$/);
  if (!match) return null;

  // Why: execute_command legacy events include machine-readable status in raw text.
  // How: extract returncode before rendering output. Purpose: historical transcripts
  // keep their compact status badge after the unified result migration.
  return {
    returnCode: Number(match[1]),
    output: match[2] || '',
  };
}

function parseSearchResults(tool: ToolExecution): { rows: Record<string, unknown>[]; count: number; truncated: boolean } | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  if (!data) return null;

  // Why: search_in_files moved rows and counters under result.data. How: normalize
  // data.results, data.count, and data.truncated with legacy top-level fallback.
  // Purpose: the search renderer shows a concise hit list instead of an envelope dump.
  const rows = Array.isArray(data.results) ? data.results.filter(isRecord) : [];
  return {
    rows,
    count: getNumberValue(data.count) ?? rows.length,
    truncated: getBooleanValue(data.truncated),
  };
}

function parseListDirTree(tool: ToolExecution): ListDirTree | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  if (!data) return null;

  // Why: list_dir now returns directory listings and totals under result.data. How:
  // normalize the directory array and optional totals from data first, then support old
  // top-level result fields. Purpose: directory trees remain structured across formats.
  return {
    directories: Array.isArray(data.results) ? data.results.filter(isRecord) : [],
    totalFiles: getNumberValue(data.totalFiles),
    totalDirs: getNumberValue(data.totalDirs),
  };
}

function renderJsonWithHighlightedKeys(text: string): ReactNode {
  const nodes: ReactNode[] = [];
  const pattern = /("(?:\\.|[^"\\])*")(\s*:)?/g;
  let lastIndex = 0;
  let tokenIndex = 0;
  let match: RegExpExecArray | null;

  // Why: unknown JSON tools still need a readable fallback. How: wrap only object keys
  // with a blue span while preserving the original whitespace in a pre block. Purpose:
  // provide lightweight syntax coloring without adding a dependency.
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    if (match[2]) {
      nodes.push(
        <span key={`json-key-${tokenIndex}`} className="text-blue-600">
          {match[1]}
        </span>,
      );
      nodes.push(match[2]);
    } else {
      nodes.push(match[0]);
    }

    lastIndex = match.index + match[0].length;
    tokenIndex += 1;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

interface LayeredPreviewProps {
  text: string;
  emptyLabel?: string;
  renderText?: (visibleText: string) => ReactNode;
}

function LayeredPreview({ text, emptyLabel = '', renderText }: LayeredPreviewProps) {
  const [showFull, setShowFull] = useState(false);
  const limited = isPreviewLimited(text);
  const visibleText = getPreviewText(text, showFull);
  const renderedText = visibleText || emptyLabel;

  return (
    <div className="space-y-1">
      {renderText ? renderText(renderedText) : <pre className={PREVIEW_PRE_CLASS}>{renderedText}</pre>}
      {limited && !showFull && (
        <button
          type="button"
          className="text-[0.64rem] font-semibold text-blue-600 hover:underline"
          onClick={() => setShowFull(true)}
        >
          查看完整内容
        </button>
      )}
    </div>
  );
}

function renderReadFileResult(tool: ToolExecution): ReactNode {
  const dataSections = parseReadFileData(tool);
  const rawText = stringifyValue(tool.rawInline || tool.result);
  const sections = dataSections.length > 0 ? dataSections : parseReadFileSections(rawText);

  if (sections.length === 0) {
    return <LayeredPreview text={getRawResultText(tool)} />;
  }

  return (
    <div className="space-y-2">
      {sections.map((section, index) => (
        <div key={`${section.path}-${index}`} className={index > 0 ? 'border-t border-current/10 pt-2' : ''}>
          <div className="mb-1 font-semibold text-[var(--duties-text)]">{section.path}</div>
          <LayeredPreview text={section.content} emptyLabel="（空文件）" />
        </div>
      ))}
    </div>
  );
}

function renderExecuteCommandResult(tool: ToolExecution): ReactNode {
  const commandResult = parseCommandResult(tool);

  if (!commandResult) {
    return <LayeredPreview text={getRawResultText(tool)} />;
  }

  const badgeClassName = commandResult.returnCode === 0
    ? 'border-green-200 bg-green-100 text-green-700'
    : 'border-red-200 bg-red-100 text-red-700';

  return (
    <div className="space-y-2">
      <span className={`inline-flex rounded border px-1.5 py-0.5 font-semibold ${badgeClassName}`}>
        返回码={commandResult.returnCode}
      </span>
      <LayeredPreview text={commandResult.output} emptyLabel="（无输出）" />
    </div>
  );
}

function renderSearchInFilesResult(tool: ToolExecution): ReactNode {
  const parsed = parseSearchResults(tool);

  if (!parsed) {
    return renderFallbackResult(tool);
  }

  const { rows, count, truncated } = parsed;

  return (
    <div className="space-y-1">
      <div className="font-semibold text-[var(--duties-text)]">
        找到 {count} 个结果 {truncated && <span className="font-normal text-[var(--duties-tertiary)]">（结果已截断）</span>}
      </div>
      <div className="space-y-1">
        {rows.map((row, index) => {
          const file = getStringValue(row.file) || '（未知文件）';
          const line = (getNumberValue(row.line) ?? getStringValue(row.line)) || '?';
          const match = getStringValue(row.match);
          const context = getStringValue(row.context);
          return (
            <div key={`${file}-${line}-${index}`} className="break-words rounded bg-black/5 px-2 py-1">
              <span className="font-semibold text-[var(--duties-text)]">{file}:{line}</span>
              <span className="text-[var(--duties-tertiary)]"> | </span>
              <span>{match}</span>
              {context && (
                <>
                  <span className="text-[var(--duties-tertiary)]"> | </span>
                  <span className="text-[var(--duties-tertiary)]">{context}</span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function renderListDirResult(tool: ToolExecution): ReactNode {
  const parsed = parseListDirTree(tool);

  if (!parsed) {
    return renderFallbackResult(tool);
  }

  const { directories, totalFiles, totalDirs } = parsed;

  return (
    <div className="space-y-2">
      {(totalFiles !== undefined || totalDirs !== undefined) && (
        <div className="font-semibold text-[var(--duties-text)]">
          {totalDirs ?? 0} 个目录，{totalFiles ?? 0} 个文件
        </div>
      )}
      {directories.map((directory, directoryIndex) => {
        const path = getStringValue(directory.path) || '.';
        const entries = Array.isArray(directory.entries) ? directory.entries.filter(isRecord) : [];
        return (
          <div key={`${path}-${directoryIndex}`} className={directoryIndex > 0 ? 'border-t border-current/10 pt-2' : ''}>
            <div className="inline-flex items-center gap-1 font-semibold text-[var(--duties-text)]">
              {/* [2026-06-01] Why: directory result headers used folder emoji.
                  How: render folder through Material Symbols. Purpose: structured
                  list_dir previews match the rest of the tool icon migration. */}
              <Icon name="folder" size={14} />
              <span>{path}</span>
            </div>
            <div className="mt-1 space-y-0.5 pl-3">
              {entries.map((entry, entryIndex) => {
                const type = getStringValue(entry.type);
                const iconName = type === 'directory' ? 'folder' : 'draft';
                return (
                  <div key={`${path}-${entryIndex}`} className="inline-flex items-center gap-1">
                    {/* [2026-06-01] Why: list entries used folder and document emoji.
                        How: select a Material Symbol name from the entry type. Purpose:
                        generated directory previews no longer emit emoji. */}
                    <Icon name={iconName} size={13} />
                    <span>{getStringValue(entry.name)}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function renderJsonResult(tool: ToolExecution): ReactNode {
  const readableResult = getReadableDataResult(tool);
  if (readableResult) {
    // Why: unknown JSON tools now often include a human-readable result.data.result.
    // How: render that string before syntax-highlighting the full envelope. Purpose:
    // users get the intended text fallback instead of a less readable JSON dump.
    return <LayeredPreview text={readableResult} />;
  }

  const jsonSource = tool.result !== undefined ? tool.result : getResultRecord(tool) || tool.rawInline;
  const jsonText = stringifyValue(jsonSource);

  return (
    <LayeredPreview
      text={jsonText}
      renderText={(visibleText) => (
        <pre className={PREVIEW_PRE_CLASS}>{renderJsonWithHighlightedKeys(visibleText)}</pre>
      )}
    />
  );
}

function renderFallbackResult(tool: ToolExecution): ReactNode {
  // Why: generic tools can now provide readable text in result.data.result even when
  // their format is json. How: getResultText checks data.result before legacy raw text
  // and JSON serialization. Purpose: unknown unified tools render the clearest text.
  return <LayeredPreview text={getResultText(tool)} />;
}

function renderResult(tool: ToolExecution): ReactNode {
  if (tool.status === 'error' || tool.rejected) {
    return <LayeredPreview text={getResultText(tool)} />;
  }

  // Why: unified tool envelopes make structured fields available by tool name even
  // when format metadata is missing or no longer matches the old raw_inline style.
  // How: dispatch known tools by name first, then fall back to plain data.result text
  // or highlighted JSON. Purpose: current and historical tool cards both stay readable.
  if (tool.name === 'read_file') {
    return renderReadFileResult(tool);
  }
  if (tool.name === 'execute_command') {
    return renderExecuteCommandResult(tool);
  }
  if (tool.name === 'grep' || tool.name === 'search_in_files') {
    return renderSearchInFilesResult(tool);
  }
  if (tool.name === 'list_dir') {
    return renderListDirResult(tool);
  }
  if (tool.format === 'json') {
    return renderJsonResult(tool);
  }

  return renderFallbackResult(tool);
}

type ApprovalDecision = 'allow' | 'deny';

interface ToolHeaderButtonProps {
  canExpand: boolean;
  expanded: boolean;
  toolMeta: ToolMeta;
  statusStyle: StatusStyle;
  displayName: string;
  inlineDetail: string;
  isFailed: boolean;
  collapsedSummary: string;
  elapsedMs?: number;
  onToggle: () => void;
}

function ToolHeaderButton({
  canExpand,
  expanded,
  toolMeta,
  statusStyle,
  displayName,
  inlineDetail,
  isFailed,
  collapsedSummary,
  elapsedMs,
  onToggle,
}: ToolHeaderButtonProps) {
  // Why: the first row is the only clickable disclosure control for the tool card.
  // How: keep the icon, chevron, status, title, inline detail, and elapsed time in one
  // focused component. Purpose: the parent ToolCallCard can read as header, approval,
  // approval result, then expanded details without interleaving those concerns.
  return (
    <button
      type="button"
      className={`flex w-full items-center gap-1.5 text-left ${INLINE_BLOCK_HEADER_TEXT_CLASS} text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)] ${canExpand ? 'cursor-pointer' : 'cursor-default'}`}
      aria-expanded={canExpand ? expanded : undefined}
      onClick={() => canExpand && onToggle()}
    >
      <span className="flex-shrink-0 text-[var(--duties-tertiary)]">
        <Icon name={toolMeta.icon} size={14} />
      </span>
      {canExpand && (
        <span className={`flex-shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}>
          <Icon name="chevron_right" size={13} />
        </span>
      )}
      <span className={`flex-shrink-0 ${statusStyle.iconClassName} ${statusStyle.spin ? 'inline-block animate-spin' : ''}`}>
        <Icon name={statusStyle.icon} size={13} />
      </span>
      {inlineDetail
        ? <span className={`truncate ${isFailed ? 'text-red-500' : 'text-[var(--duties-text)]'}`}>{inlineDetail}</span>
        : <span className="flex-shrink-0 font-semibold text-[var(--duties-text)]">{displayName}</span>
      }
      {!inlineDetail && collapsedSummary && <span className="truncate text-[var(--duties-secondary)]">{collapsedSummary}</span>}
      {elapsedMs !== undefined && (
        <span className="flex-shrink-0 text-[var(--duties-tertiary)]">{Math.round(elapsedMs)}ms</span>
      )}
    </button>
  );
}

interface ToolApprovalSectionProps {
  tool: ToolExecution;
  approvalDetails: Record<string, unknown>;
  approvalOperation: string;
  isAutoApprovedPending: boolean;
  approvalLoading: boolean;
  approvalError: string;
  onApproval: (approvalId: string, decision: ApprovalDecision) => void;
}

function ToolApprovalSection({
  tool,
  approvalDetails,
  approvalOperation,
  isAutoApprovedPending,
  approvalLoading,
  approvalError,
  onApproval,
}: ToolApprovalSectionProps) {
  // Why: pending approval details and action buttons are a distinct section between
  // the tool header and expanded payloads. How: render the section only for the
  // awaiting_approval lifecycle state that has an approval id. Purpose: approval UI
  // stays local to the tool execution without obscuring normal result details.
  if (tool.status !== 'awaiting_approval' || !tool.approvalId) return null;

  const approvalId = tool.approvalId;

  return (
    <div className={`mt-1 space-y-2 pl-1 font-mono ${TOOL_DETAIL_TEXT_CLASS}`}>
      <div className="space-y-1 text-[var(--duties-secondary)]">
        <div><span className="text-[var(--duties-tertiary)]">操作：</span> <code className="text-[var(--duties-text)]">{approvalOperation}</code></div>
        {approvalDetails.path !== undefined && (
          <div><span className="text-[var(--duties-tertiary)]">路径：</span> <code className="text-[var(--duties-text)]">{getStringValue(approvalDetails.path)}</code></div>
        )}
        {approvalDetails.reason !== undefined && (
          <div><span className="text-[var(--duties-tertiary)]">原因：</span> {getStringValue(approvalDetails.reason)}</div>
        )}
      </div>
      {isAutoApprovedPending ? (
        <div className="inline-flex items-center gap-1 rounded-sm bg-gray-100 px-2 py-1 text-xs font-semibold text-gray-600">
          {/* Why: auto-approved tools should not present manual buttons while the
              local client decision is being submitted. How: show a muted badge
              instead. Purpose: users can distinguish local auto-approval from a
              pending manual decision. */}
          <Icon name="check_circle" size={14} />
          <span>已自动放行</span>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-60"
            onClick={() => onApproval(approvalId, 'allow')}
            disabled={approvalLoading}
          >
            {/* Why: approval buttons used emoji status marks. How: render Material
                Symbols before each label. Purpose: the inline approval flow uses the
                same icon system as status rows. */}
            <Icon name="check_circle" size={14} />
            <span>允许</span>
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm bg-red-50 px-2 py-1 text-xs font-medium text-red-600 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-60"
            onClick={() => onApproval(approvalId, 'deny')}
            disabled={approvalLoading}
          >
            <Icon name="cancel" size={14} />
            <span>拒绝</span>
          </button>
          {approvalLoading && <span className="text-[var(--duties-tertiary)]">提交中…</span>}
        </div>
      )}
      {approvalError && <div className="text-xs font-semibold text-red-600">{approvalError}</div>}
    </div>
  );
}

function ToolApprovalOutcome({ tool }: { tool: ToolExecution }) {
  // Why: once a tool moves past awaiting_approval into running/completed/error,
  // the approval decision is redundant — the tool status itself conveys the outcome.
  // How: only show the outcome badge while the tool is still in awaiting_approval
  // state (briefly, between decision and status transition). Once running/done,
  // hide it to avoid clutter in the streaming view.
  // Purpose: approval badges don't permanently occupy space after tools complete.
  if (tool.status !== 'awaiting_approval') return null;

  if (tool.approvalStatus === 'allowed') {
    return (
      <div className="mt-1 inline-flex items-center gap-1 pl-1 font-mono text-xs font-semibold text-green-600">
        <Icon name="check_circle" size={14} />
        <span>已批准</span>
      </div>
    );
  }

  if (tool.approvalStatus === 'denied') {
    return (
      <div className="mt-1 inline-flex items-center gap-1 pl-1 font-mono text-xs font-semibold text-red-500">
        <Icon name="cancel" size={14} />
        <span>已拒绝</span>
      </div>
    );
  }

  return null;
}

interface ToolDetailsSectionProps {
  tool: ToolExecution;
  expanded: boolean;
  canExpand: boolean;
  isFailed: boolean;
  argumentDisplay: DisplayText | null;
  resultDisplay: DisplayText | null;
}

function ToolDetailsSection({ tool, expanded, canExpand, isFailed, argumentDisplay, resultDisplay }: ToolDetailsSectionProps) {
  // Why: arguments, results, and execution ids are the expanded payload layer, not
  // part of the compact header or approval action area. How: render this section only
  // after the disclosure is open and reuse the existing structured result renderers.
  // Purpose: the card hierarchy stays header button, approval section, then details.
  if (!expanded || !canExpand) return null;

  return (
    <div className={`mt-1.5 space-y-2 font-mono ${TOOL_DETAIL_TEXT_CLASS} text-[var(--duties-secondary)]`}>
      {(tool.taskId || tool.nodeId || tool.nodeName) && (
        <div className="space-y-0.5">
          <div className="font-semibold text-[var(--duties-text)]">执行信息</div>
          {tool.taskId && <div>任务：<code>{tool.taskId}</code></div>}
          {tool.nodeName && <div>节点：<code>{tool.nodeName}</code></div>}
          {tool.nodeId && <div>节点 ID：<code>{tool.nodeId}</code></div>}
        </div>
      )}
      {argumentDisplay && (
        <div>
          <div className="mb-1 font-semibold text-[var(--duties-text)]">
            参数 <span className="font-normal text-[var(--duties-tertiary)]">{argumentDisplay.sizeLabel}</span>
          </div>
          <LayeredPreview text={argumentDisplay.text} />
        </div>
      )}
      {resultDisplay && (
        <div>
          <div className={`mb-1 font-semibold ${isFailed ? 'text-red-600' : 'text-[var(--duties-text)]'}`}>
            {isFailed ? '错误' : '结果'} <span className="font-normal text-[var(--duties-tertiary)]">{resultDisplay.sizeLabel}</span>
          </div>
          {renderResult(tool)}
        </div>
      )}
    </div>
  );
}

export const ToolCallCard = ({ tool }: ToolCallCardProps) => {
  const toolResultsDefaultCollapsed = useClientPrefsStore(state => state.toolResultsDefaultCollapsed);
  const autoApproveTools = useClientPrefsStore(state => state.autoApproveTools);
  const approvalLevel = useClientPrefsStore(state => state.approvalLevel);
  // [2026-06-01] Tool detail expansion now follows clientPrefsStore.
  // Why: result disclosure was hard-coded as collapsed. How: initialize the local
  // disclosure state from the browser preference. Purpose: each frontend can choose
  // whether tool details start open without changing backend output.
  const [expanded, setExpanded] = useState(() => !toolResultsDefaultCollapsed);
  // Why: approval decisions are submitted from inside the tool card. How: keep a
  // local loading flag only for the clicked network request and let WebSocket replay
  // update approvalStatus. Purpose: the UI avoids duplicate clicks without inventing
  // a second source of truth for approval state.
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const statusStyle = getStatusStyle(tool);
  const argumentsText = useMemo(() => getArgumentsText(tool), [tool]);
  const resultText = useMemo(() => getResultText(tool), [tool]);
  const argumentDisplay = useMemo(() => createDisplayText(argumentsText), [argumentsText]);
  const resultDisplay = useMemo(() => createDisplayText(resultText), [resultText]);
  const collapsedSummary = tool.summary
    ? getCompactPreview(tool.summary)
    : tool.status === 'args_streaming'
      ? getCompactPreview(argumentDisplay?.text || '')
      : '';
  const approvalDetails = getApprovalDetailsRecord(tool);
  const approvalOperation = getApprovalOperation(tool);
  const canExpand = Boolean(argumentDisplay || resultDisplay || tool.taskId || tool.nodeId || tool.nodeName || tool.approvalDetails);
  const isAutoApprovedPending = tool.status === 'awaiting_approval'
    && tool.approvalStatus === 'pending'
    && Boolean(tool.approvalId && autoApprovedApprovalIds.has(tool.approvalId));

  const handleApproval = async (approvalId: string, decision: ApprovalDecision) => {
    // Why: the same tool card owns the approval action. How: call the existing
    // supervisor client and rely on approval_decided events to update the reducer.
    // Purpose: the button path stays compatible with the old ApprovalCard API while
    // rendering from ToolExecution.
    setApprovalLoading(true);
    setApprovalError('');
    try {
      await decideApproval(approvalId, decision, `${decision} via tool card`);
    } catch (error) {
      setApprovalError(error instanceof Error ? error.message : '提交审批决定失败。');
    } finally {
      setApprovalLoading(false);
    }
  };

  if (tool.hidden) {
    return null;
  }

  // Why: tool cards should be lightweight inline annotations with recognizable tool
  // identity. How: resolve metadata once, then pass the icon, label, status, summary,
  // and detail text into the header button. Purpose: the render order stays clear and
  // each row is identifiable without reading the raw function name first.
  const toolMeta = getToolMeta(tool.name);
  const displayName = toolMeta.label || tool.name;
  // Why: rejected is optional on ToolExecution, but child components need a strict
  // boolean. How: coerce the optional value while preserving the existing error-or-
  // rejected failure rule. Purpose: the refactored section props stay type-safe.
  const isFailed = tool.status === 'error' || Boolean(tool.rejected);
  const inlineSummary = useMemo(() => getToolInlineSummary(tool), [tool]);
  const resultSuffix = useMemo(() => getToolResultSuffix(tool), [tool]);
  const failureReason = useMemo(() => {
    if (!isFailed) return '';
    const raw = tool.error || getResultText(tool);
    return raw ? getCompactPreview(raw) : '';
  }, [isFailed, tool]);
  const inlineDetail = isFailed
    ? failureReason
    : resultSuffix
      ? (inlineSummary ? `${inlineSummary} → ${resultSuffix}` : resultSuffix)
      : inlineSummary;

  return (
    <div className={`font-mono ${INLINE_BLOCK_BODY_TEXT_CLASS} text-[var(--duties-secondary)]`}>
      <ToolHeaderButton
        canExpand={canExpand}
        expanded={expanded}
        toolMeta={toolMeta}
        statusStyle={statusStyle}
        displayName={displayName}
        inlineDetail={inlineDetail}
        isFailed={isFailed}
        collapsedSummary={collapsedSummary}
        elapsedMs={tool.elapsedMs}
        onToggle={() => setExpanded((value) => !value)}
      />
      <ToolApprovalSection
        tool={tool}
        approvalDetails={approvalDetails}
        approvalOperation={approvalOperation}
        isAutoApprovedPending={isAutoApprovedPending}
        approvalLoading={approvalLoading}
        approvalError={approvalError}
        onApproval={handleApproval}
      />
      <ToolApprovalOutcome tool={tool} />
      <ToolDetailsSection
        tool={tool}
        expanded={expanded}
        canExpand={canExpand}
        isFailed={isFailed}
        argumentDisplay={argumentDisplay}
        resultDisplay={resultDisplay}
      />
    </div>
  );
};

export type { ToolCallCardProps };
