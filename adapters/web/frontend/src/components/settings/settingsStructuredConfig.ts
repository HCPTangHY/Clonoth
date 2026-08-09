// Structured config helpers for Settings UI — powered by js-yaml.
// Replaces the former hand-rolled regex/recursive-descent parser with
// a proper YAML library while preserving every exported type and function
// signature so that consuming components need zero changes.
import yaml from 'js-yaml';

// ==================== Type Definitions ====================

export type ScalarValue = string | number | boolean | null;
export type SimpleYamlValue = ScalarValue | SimpleYamlObject | SimpleYamlValue[];
export interface SimpleYamlObject { [key: string]: SimpleYamlValue; }

export interface ParsedProvidersResult {
  providers: Record<string, SimpleYamlObject>;
  rawProviders: string;
}

export interface ScheduleFormState {
  id: string;
  cron: string;
  type: 'message' | 'script';
  text: string;
  command: string;
  enabled: boolean;
  once: boolean;
  conversation_key: string;
  entry_node_id: string;
  workflow_id: string;
  timeout: string;
  silent: boolean;
}

export interface McpClientFormState {
  id: string;
  description: string;
  enabled: boolean;
  transport: 'stdio' | 'sse' | 'streamable_http';
  command: string;
  argsText: string;
  envText: string;
  url: string;
  headersText: string;
}

export interface SkillFormState {
  name: string;
  description: string;
  enabled: boolean;
  strategy: 'normal' | 'constant';
  keywordsText: string;
  order: string;
  priority: string;
  scan_depth: string;
  body: string;
}

export type NodeConfigType = 'ai' | 'tool' | 'router';
export type ToolAccessMode = 'all' | 'allow' | 'deny' | 'none';
export type RuntimeToolMode = 'json' | 'native' | 'fake-native';
export type OutputMode = 'hybrid' | 'tool_only';

export interface NodeConfigFormState {
  id: string;
  name: string;
  description: string;
  type: NodeConfigType;
  model: string;
  provider: string;
  memory_book: string;
  persistent: boolean;
  prompt: string;
  delegate_targetsText: string;
  tool_access_mode: ToolAccessMode;
  tool_access_allowText: string;
  tool_access_denyText: string;
}

export interface RuntimeConfigFormState {
  max_steps: string;
  streaming: boolean;
  retry_max_retries: string;
  compact_threshold_tokens: string;
  compact_keep_recent: string;
  tool_mode: RuntimeToolMode;
  output_mode: OutputMode;
}

// ==================== Core Helpers ====================

const DUMP_OPTS: yaml.DumpOptions = {
  indent: 2,
  lineWidth: -1,
  noRefs: true,
  sortKeys: false,
  quotingType: '"',
  forceQuotes: false,
  noCompatMode: true,
};

function safeLoad(raw: string): Record<string, any> {
  try {
    const result = yaml.load(raw);
    if (result && typeof result === 'object' && !Array.isArray(result)) return result as Record<string, any>;
  } catch { /* swallow parse errors — form stays at defaults */ }
  return {};
}

function safeDump(obj: any): string {
  return yaml.dump(obj, DUMP_OPTS);
}

function str(value: any, fallback = ''): string {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function normalizeToolAccessMode(value: string, fallback: ToolAccessMode = 'all'): ToolAccessMode {
  return value === 'allow' || value === 'deny' || value === 'none' || value === 'all' ? value : fallback;
}

function normalizeNodeConfigType(value: string): NodeConfigType {
  return value === 'tool' || value === 'router' || value === 'ai' ? value : 'ai';
}

function commaTextToItems(value: string): string[] {
  return value.split(',').map((s) => s.trim()).filter(Boolean);
}

/** Convert a plain object to a textarea-friendly `key: value` text. */
function serializeLooseKV(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([k, v]) => `${k}: ${String(v ?? '')}`)
    .join('\n');
}

/** Parse textarea `key: value` lines back into a plain object. */
function parseLooseKV(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(':');
    if (idx < 0) continue;
    const key = trimmed.slice(0, idx).trim();
    const val = trimmed.slice(idx + 1).trim();
    if (key) result[key] = val;
  }
  return result;
}

// ==================== Node Config ====================

export function parseNodeConfig(raw: string, fallbackId = ''): NodeConfigFormState {
  const doc = safeLoad(raw);
  const ta = (doc.tool_access && typeof doc.tool_access === 'object' && !Array.isArray(doc.tool_access))
    ? doc.tool_access as Record<string, any> : {};
  return {
    id: str(doc.id, fallbackId),
    name: str(doc.name),
    description: str(doc.description),
    type: normalizeNodeConfigType(str(doc.type, 'ai')),
    model: str(doc.model),
    provider: str(doc.provider),
    memory_book: str(doc.memory_book),
    persistent: doc.persistent === true,
    prompt: str(doc.prompt),
    delegate_targetsText: Array.isArray(doc.delegate_targets) ? doc.delegate_targets.map(String).join(', ') : '',
    tool_access_mode: normalizeToolAccessMode(str(ta.mode, 'all')),
    tool_access_allowText: Array.isArray(ta.allow) ? ta.allow.map(String).join(', ') : '',
    tool_access_denyText: Array.isArray(ta.deny) ? ta.deny.map(String).join(', ') : '',
  };
}

export function serializeNodeConfig(raw: string, form: NodeConfigFormState): string {
  const doc = safeLoad(raw);
  doc.id = form.id.trim();
  doc.name = form.name.trim();
  doc.description = form.description.trim();
  doc.type = form.type;
  doc.model = form.model.trim();
  if (form.provider.trim()) doc.provider = form.provider.trim(); else delete doc.provider;
  if (form.memory_book.trim()) doc.memory_book = form.memory_book.trim(); else delete doc.memory_book;
  doc.persistent = form.persistent;
  if (form.prompt.trim()) doc.prompt = form.prompt; else delete doc.prompt;
  doc.delegate_targets = commaTextToItems(form.delegate_targetsText);
  const mode = normalizeToolAccessMode(form.tool_access_mode);
  const ta: Record<string, any> = { mode };
  if (mode === 'allow') ta.allow = commaTextToItems(form.tool_access_allowText);
  if (mode === 'deny') ta.deny = commaTextToItems(form.tool_access_denyText);
  doc.tool_access = ta;
  return safeDump(doc);
}

// ==================== Runtime Config ====================

function normalizeRuntimeToolMode(value: string, fallback: RuntimeToolMode = 'json'): RuntimeToolMode {
  return value === 'native' || value === 'fake-native' || value === 'json' ? value : fallback;
}

function normalizeOutputMode(value: string): OutputMode {
  return value === 'tool_only' ? 'tool_only' : 'hybrid';
}

export function parseRuntimeConfig(raw: string): RuntimeConfigFormState {
  const doc = safeLoad(raw);
  const engine = doc.engine && typeof doc.engine === 'object' && !Array.isArray(doc.engine) ? doc.engine as Record<string, any> : {};
  const retry = engine.retry && typeof engine.retry === 'object' && !Array.isArray(engine.retry) ? engine.retry as Record<string, any> : {};
  const compact = engine.compact && typeof engine.compact === 'object' && !Array.isArray(engine.compact) ? engine.compact as Record<string, any> : {};
  return {
    max_steps: engine.max_steps === undefined ? '' : String(engine.max_steps),
    streaming: engine.streaming !== false,
    retry_max_retries: retry.max_retries === undefined ? '' : String(retry.max_retries),
    compact_threshold_tokens: compact.threshold_tokens === undefined ? '' : String(compact.threshold_tokens),
    compact_keep_recent: compact.keep_recent === undefined ? '' : String(compact.keep_recent),
    tool_mode: normalizeRuntimeToolMode(str(engine.tool_mode, 'json')),
    output_mode: normalizeOutputMode(str(engine.output_mode, 'hybrid')),
  };
}

function applyNumberField(target: Record<string, any>, key: string, value: string): void {
  const trimmed = value.trim();
  if (!trimmed) { delete target[key]; return; }
  const parsed = Number(trimmed);
  target[key] = Number.isFinite(parsed) ? parsed : trimmed;
}

export function serializeRuntimeConfig(raw: string, form: RuntimeConfigFormState): string {
  const doc = safeLoad(raw);
  const engine = doc.engine && typeof doc.engine === 'object' && !Array.isArray(doc.engine) ? doc.engine as Record<string, any> : {};
  const retry = engine.retry && typeof engine.retry === 'object' && !Array.isArray(engine.retry) ? engine.retry as Record<string, any> : {};
  const compact = engine.compact && typeof engine.compact === 'object' && !Array.isArray(engine.compact) ? engine.compact as Record<string, any> : {};
  applyNumberField(engine, 'max_steps', form.max_steps);
  engine.streaming = form.streaming;
  applyNumberField(retry, 'max_retries', form.retry_max_retries);
  applyNumberField(compact, 'threshold_tokens', form.compact_threshold_tokens);
  applyNumberField(compact, 'keep_recent', form.compact_keep_recent);
  engine.retry = retry;
  engine.compact = compact;
  engine.tool_mode = normalizeRuntimeToolMode(form.tool_mode);
  engine.output_mode = normalizeOutputMode(form.output_mode);
  doc.engine = engine;
  delete doc.entry_node_id;
  delete doc.tool_mode;
  delete doc.max_concurrent_tasks;
  return safeDump(doc);
}

// ==================== Providers ====================

export function parseProvidersFromRuntime(raw: string): ParsedProvidersResult {
  const doc = safeLoad(raw);
  const src = doc.providers;
  const providers: Record<string, SimpleYamlObject> = {};
  if (src && typeof src === 'object' && !Array.isArray(src)) {
    for (const [k, v] of Object.entries(src)) {
      if (v && typeof v === 'object' && !Array.isArray(v)) providers[k] = v as SimpleYamlObject;
    }
  }
  const rawBlock = Object.keys(providers).length > 0
    ? yaml.dump({ providers }, DUMP_OPTS).trimEnd()
    : 'providers: {}';
  return { providers, rawProviders: rawBlock };
}

export function serializeProvidersBlock(providers: Record<string, SimpleYamlObject>): string {
  const filtered = Object.fromEntries(Object.entries(providers).filter(([k]) => k.trim()));
  if (Object.keys(filtered).length === 0) return 'providers: {}';
  return yaml.dump({ providers: filtered }, DUMP_OPTS).trimEnd();
}

export function replaceProvidersInRuntime(raw: string, providers: Record<string, SimpleYamlObject>): string {
  const doc = safeLoad(raw);
  doc.providers = providers;
  return safeDump(doc);
}

// ==================== Schedules ====================

export function parseSchedules(raw: string): ScheduleFormState[] {
  const doc = safeLoad(raw);
  const list = Array.isArray(doc.schedules) ? doc.schedules : [];
  return list
    .filter((item: any): item is Record<string, any> => Boolean(item && typeof item === 'object' && !Array.isArray(item)))
    .map((item: Record<string, any>) => ({
      id: str(item.id),
      cron: str(item.cron),
      type: (item.type === 'script' ? 'script' : 'message') as 'message' | 'script',
      text: str(item.text),
      command: str(item.command),
      enabled: item.enabled !== false,
      once: item.once === true,
      conversation_key: str(item.conversation_key),
      entry_node_id: str(item.entry_node_id),
      workflow_id: str(item.workflow_id),
      timeout: item.timeout === undefined ? '' : String(item.timeout),
      silent: item.silent === true,
    }));
}

export function serializeSchedules(schedules: ScheduleFormState[]): string {
  const items = schedules.map((s) => {
    const obj: Record<string, any> = {
      id: s.id, cron: s.cron, type: s.type, text: s.text,
      enabled: s.enabled, once: s.once,
    };
    if (s.conversation_key) obj.conversation_key = s.conversation_key;
    if (s.entry_node_id) obj.entry_node_id = s.entry_node_id;
    if (s.workflow_id) obj.workflow_id = s.workflow_id;
    if (s.type === 'script') {
      obj.command = s.command;
      if (s.timeout) obj.timeout = Number(s.timeout) || s.timeout;
      obj.silent = s.silent;
    }
    return obj;
  });
  if (items.length === 0) return 'schedules: []\n';
  return yaml.dump({ schedules: items }, DUMP_OPTS);
}

// ==================== MCP Clients ====================

export function parseMcpClients(raw: string): McpClientFormState[] {
  const doc = safeLoad(raw);
  const clients = doc.clients;
  if (!clients || typeof clients !== 'object' || Array.isArray(clients)) return [];
  return Object.entries(clients)
    .filter(([, v]) => v && typeof v === 'object' && !Array.isArray(v))
    .map(([id, v]) => {
      const c = v as Record<string, any>;
      return {
        id,
        description: str(c.description),
        enabled: c.enabled !== false,
        transport: (c.transport === 'sse' ? 'sse' : c.transport === 'stdio' ? 'stdio' : 'streamable_http') as 'stdio' | 'sse' | 'streamable_http',
        command: str(c.command),
        argsText: Array.isArray(c.args) ? c.args.map(String).join('\n') : '',
        envText: serializeLooseKV(c.env),
        url: str(c.url),
        headersText: serializeLooseKV(c.headers),
      };
    });
}

export function serializeMcpClients(clients: McpClientFormState[]): string {
  if (clients.length === 0) return 'version: 1\nclients: {}\n';
  const obj: Record<string, any> = {};
  for (const c of clients) {
    const entry: Record<string, any> = {
      transport: c.transport,
      enabled: c.enabled,
      description: c.description,
    };
    if (c.transport === 'stdio') {
      entry.command = c.command;
      entry.args = c.argsText.split('\n').map((s) => s.trim()).filter(Boolean);
      entry.env = parseLooseKV(c.envText);
    } else {
      entry.url = c.url;
      entry.headers = parseLooseKV(c.headersText);
    }
    obj[c.id] = entry;
  }
  return yaml.dump({ version: 1, clients: obj }, DUMP_OPTS);
}

// ==================== Skills ====================

export function parseSkillMarkdown(raw: string, fallbackName: string): SkillFormState {
  let meta: Record<string, any> = {};
  let body = raw;
  if (raw.startsWith('---\n')) {
    const end = raw.indexOf('\n---\n', 4);
    if (end >= 0) {
      try {
        const parsed = yaml.load(raw.slice(4, end));
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) meta = parsed as Record<string, any>;
      } catch { /* ignore frontmatter parse errors */ }
      body = raw.slice(end + 5);
    }
  }
  const keywords = Array.isArray(meta.keywords) ? meta.keywords.map(String) : [];
  return {
    name: str(meta.name, fallbackName),
    description: str(meta.description),
    enabled: meta.enabled !== false,
    strategy: meta.strategy === 'constant' ? 'constant' : 'normal',
    keywordsText: keywords.join('\n'),
    order: meta.order === undefined ? '0' : String(meta.order),
    priority: meta.priority === undefined ? '0' : String(meta.priority),
    scan_depth: meta.scan_depth === undefined ? '0' : String(meta.scan_depth),
    body,
  };
}

export function serializeSkillMarkdown(form: SkillFormState): string {
  const keywords = form.keywordsText.split('\n').map((s) => s.trim()).filter(Boolean);
  const meta: Record<string, any> = {
    name: form.name,
    description: form.description,
    enabled: form.enabled,
    strategy: form.strategy,
    keywords,
    order: Number(form.order) || 0,
    priority: Number(form.priority) || 0,
    scan_depth: Number(form.scan_depth) || 0,
  };
  const frontmatter = yaml.dump(meta, DUMP_OPTS);
  return `---\n${frontmatter}---\n\n${form.body.replace(/^\n+/, '')}`;
}
