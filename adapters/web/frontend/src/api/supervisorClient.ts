// [2026-05-16] Real Supervisor API client — zero mock.
import type { NodeDef, SupervisorEvent } from '../types';

const API = '/v1';

// ── Helper ──

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(`${API}${path}`, init);
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp;
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/**
 * [2026-06-16] Read stored Admin Token for web-only protected write endpoints.
 * Why: several existing UI helpers do not accept a token parameter, but the backend
 * now requires Admin Token on high-risk web writes. How: read the same localStorage
 * key used by the settings login flow and return an empty string when storage is
 * unavailable. Purpose: authenticated browser sessions keep working without changing
 * every existing call site.
 */
function getStoredAdminToken(): string {
  try {
    return localStorage.getItem('clonoth_admin_token') || '';
  } catch {
    return '';
  }
}

// ── Attachment upload ──

export interface UploadedAttachment {
  path: string;
  name: string;
  size: number;
  mime_type: string;
  type: 'image' | 'file';
}

export async function uploadAttachment(
  file: File,
  conversationKey: string,
): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch(
    `${API}/attachments/upload?conversation_key=${encodeURIComponent(conversationKey)}`,
    { method: 'POST', body: form },
  );
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`Upload failed: ${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp.json();
}

// ── Inbound (send message) ──

export async function postInbound(params: {
  conversation_key: string;
  text: string;
  attachments?: any[];
  use_context?: boolean;
  entry_node_id?: string;
  provider_override?: Record<string, unknown> | null;
  // [AutoC 2026-06-18] Optional direct target session for the System session browser.
  // Why: arbitrary existing sessions cannot always be addressed by web conversation_key.
  // How: include session_id and Admin Token when the temporary session view sends a
  // message. Purpose: operators can talk in the selected session without adding it to
  // the sidebar or accidentally creating a new web session.
  session_id?: string;
}): Promise<{ session_id: string; inbound_seq: number; accepted: boolean }> {
  const token = params.session_id ? getStoredAdminToken() : '';
  const resp = await apiFetch('/inbound', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? authHeaders(token) : {}) },
    body: JSON.stringify({
      channel: 'web',
      conversation_key: params.conversation_key,
      text: params.text,
      attachments: params.attachments ?? [],
      use_context: params.use_context ?? true,
      use_branch: false,
      entry_node_id: params.entry_node_id,
      ...(params.provider_override ? { provider_override: params.provider_override } : {}),
      ...(params.session_id ? { session_id: params.session_id } : {}),
    }),
  });
  return resp.json();
}

// ── WebSocket events ──

// [2026-06-03] Global Supervisor WebSocket replaces per-session polling.
// Why: a browser tab can display several web sessions while more than one task is
// running, so closing or replacing the socket per active session loses events from
// the other sessions. How: keep one long-lived /v1/ws connection and let the store
// route each SupervisorEvent by event.session_id. Purpose: realtime delivery no
// longer depends on the fragile in-memory polling buffer or on the selected chat.
let _globalWs: WebSocket | null = null;
let _globalWsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _globalWsReconnectDelay = 1000;

export function connectGlobalWS(
  lastSeq: number,
  onEvent: (event: SupervisorEvent) => void,
  onOpen?: () => void,
  onDisconnect?: () => void,
): void {
  // [2026-06-03] Why: loadStartup and sendMessage can both request realtime setup.
  // How: do not open a second socket while one is CONNECTING or OPEN. Purpose:
  // global event delivery stays single-copy while the connection remains long-lived.
  const readyState = _globalWs?.readyState;
  if (readyState === 0 || readyState === 1) {
    return;
  }

  disconnectGlobalWS();

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/v1/ws`;
  const ws = new WebSocket(wsUrl);
  _globalWs = ws;

  ws.onopen = () => {
    // [2026-06-03] Why: /v1/ws uses a global sequence cursor, unlike the old
    // per-session endpoint. How: send the latest known EventLog seq across all
    // sessions once the socket opens. Purpose: reconnect catch-up covers every
    // session without session event polling.
    _globalWsReconnectDelay = 1000;
    ws.send(JSON.stringify({ last_seq: Math.max(0, lastSeq || 0) }));
    onOpen?.();
  };

  ws.onmessage = (msgEvent) => {
    try {
      const data = JSON.parse(msgEvent.data);
      if (data.type === 'ping') return;
      onEvent(data as SupervisorEvent);
    } catch {
      // [2026-06-03] Why: the global stream is append-only and should tolerate
      // malformed frames. How: ignore the bad frame only. Purpose: one malformed
      // payload cannot stop future session events.
    }
  };

  ws.onclose = () => {
    if (_globalWs === ws) _globalWs = null;
    onDisconnect?.();
    // [2026-06-03] Reconnect is handled by the store's onDisconnect callback
    // (startGlobalWebSocket). The transport layer only tracks backoff delay.
    _globalWsReconnectDelay = Math.min(_globalWsReconnectDelay * 2, 30000);
  };

  ws.onerror = () => {
    // [2026-06-03] Why: network errors should share the same cleanup and reconnect
    // callback as normal disconnects. How: close the socket and let onclose run.
    // Purpose: the store has one recovery path for long-lived global realtime.
    ws.close();
  };
}

export function disconnectGlobalWS(): void {
  // [2026-06-03] Why: resetState and tests still need an explicit cleanup point.
  // How: clear the pending reconnect marker and close only the global socket.
  // Purpose: ordinary task completion never calls this, but full teardown remains safe.
  if (_globalWsReconnectTimer) { clearTimeout(_globalWsReconnectTimer); _globalWsReconnectTimer = null; }
  if (_globalWs) {
    _globalWs.onclose = null;
    _globalWs.close();
    _globalWs = null;
  }
}

export function connectSessionWS(
  sessionId: string,
  lastSeq: number,
  onEvent: (event: SupervisorEvent) => void,
  onOpen?: () => void,
  onDisconnect?: () => void,
): void {
  // [2026-06-03] Compatibility wrapper. Why: the legacy chatStore still imports the
  // old per-session helper. How: route it through the global WebSocket and filter by
  // session id at the client boundary. Purpose: avoid keeping two WebSocket designs
  // while chatStore has moved to all-session realtime delivery.
  connectGlobalWS(
    lastSeq,
    (event) => { if (event.session_id === sessionId) onEvent(event); },
    onOpen,
    onDisconnect,
  );
}

export function disconnectSessionWS(): void {
  // [2026-06-03] Compatibility wrapper. Why: existing legacy call sites still call
  // disconnectSessionWS during teardown. How: delegate to the global cleanup helper.
  // Purpose: reset paths remain functional without reintroducing per-session sockets.
  disconnectGlobalWS();
}

// ── Health ──

export interface HealthState {
  status: string;
  run_id?: string;
  workspace_root?: string;
  started_at?: string;
  uptime_seconds?: number;
}

export async function checkHealth(): Promise<HealthState> {
  const resp = await apiFetch('/health');
  return resp.json();
}

export interface AdminApproval {
  approval_id: string;
  session_id?: string;
  operation: string;
  details?: Record<string, unknown>;
  status?: string;
  fingerprint?: string;
  requested_at?: string;
  decided_at?: string | null;
  decision?: 'allow' | 'deny' | null;
  comment?: string | null;
  tool_call_id?: string | null;
  node_id?: string | null;
  task_id?: string | null;
}

export interface AdminState {
  sessions: number;
  approvals: Record<string, number>;
  tasks: Record<string, number>;
  pending_approvals: AdminApproval[];
  engine_runtime: Record<string, unknown>;
}

export interface ActiveTask {
  task_id: string;
  session_id: string;
  node_id: string | null;
  status: 'running' | 'pending' | 'suspended';
  kind: 'node' | 'tool';
  created_at: string;
  updated_at: string;
  worker_id: string | null;
  caller_task_id: string | null;
  // [AutoC 2026-06-04] Why: the active-task modal should identify work without
  // downloading the full task input. How: mirror the backend's capped preview and
  // explicit cancellation flag. Purpose: UI rows can show context and disable
  // duplicate cancellation requests from typed data.
  input_summary: string;
  cancel_requested: boolean;
  current_phase: string;
  current_detail: string;
}

export interface AdminNode extends NodeDef {
  tool_access?: unknown;
  skills?: unknown;
  source?: 'system' | 'user';
}

export interface AdminTool {
  name: string;
  file?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  timeout_sec?: number;
  has_spec?: boolean;
}

export interface AdminSkill {
  name: string;
  description?: string;
  enabled?: boolean;
  strategy?: string;
  keywords?: string[];
  body_preview?: string;
  error?: string;
}

export interface McpClient {
  id: string;
  description?: string;
  enabled?: boolean;
  transport?: string;
  command?: string;
  args?: string[];
  env?: Record<string, unknown>;
  url?: string;
  headers?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AdminCreatePayload {
  id: string;
  content: string;
}

async function readRawConfig(path: string, token: string): Promise<string> {
  // [2026-06-02] Shared raw-config reader for the expanded Settings tabs.
  // Why: nodes, tools, skills, MCP clients, schedules, policy, and runtime all expose
  // the same {content:string} shape. How: centralize bearer auth and response
  // unwrapping in one helper. Purpose: page code edits text without duplicating API
  // response handling or accidentally returning the wrapper object.
  const resp = await apiFetch(path, { headers: authHeaders(token) });
  const json = await resp.json();
  return typeof json.content === 'string' ? json.content : '';
}

async function writeRawConfig(path: string, token: string, content: string): Promise<any> {
  // [2026-06-02] Shared raw-config writer for the expanded Settings tabs.
  // Why: every raw editor saves through the same {content:string} backend model. How:
  // send JSON with the admin bearer header in one helper. Purpose: the UI can add new
  // raw-backed pages without reimplementing method, headers, and payload shape.
  const resp = await apiFetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ content }),
  });
  return resp.json();
}

const pathPart = (value: string): string => encodeURIComponent(value);

export async function getAdminState(token: string): Promise<AdminState> {
  // [2026-06-01] Fetch the protected Supervisor snapshot for the chat dashboard.
  // Why: the new right rail needs session, approval, task, and engine worker counts.
  // How: call /v1/admin/state with the existing bearer token helper. Purpose: keep
  // dashboard polling inside the API client instead of scattering endpoint strings.
  const resp = await apiFetch('/admin/state', { headers: authHeaders(token) });
  return resp.json();
}

export async function fetchActiveTasks(token = ''): Promise<ActiveTask[]> {
  // [AutoC 2026-06-04] Why: the System dashboard task count now opens a detail
  // modal. How: call the new protected summary endpoint with the same bearer-token
  // helper used by other admin APIs. Purpose: task monitoring stays independent of
  // chatStore and does not duplicate request construction in UI components.
  const init = token ? { headers: authHeaders(token) } : undefined;
  const resp = await fetch(`${API}/admin/tasks/active`, init);
  if (!resp.ok) throw new Error(`Failed to fetch active tasks: ${resp.status}`);
  return resp.json();
}

export async function cancelTask(adminToken: string, taskId: string): Promise<void> {
  // [AutoC 2026-06-04] Why: ActiveTasksModal cancels one task at a time, while the
  // existing cancelActiveTasks helper targets an entire session. How: call the
  // public single-task endpoint and still include admin context headers for future
  // backend hardening. Purpose: row-level cancel buttons remain precise and safe.
  const headers = adminToken
    ? { ...authHeaders(adminToken), 'X-Admin-Token': adminToken }
    : {};
  const resp = await fetch(`${API}/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
    headers,
  });
  if (!resp.ok) throw new Error(`Cancel failed: ${resp.status}`);
}

// ── Admin auth ──

export interface AuthStatus {
  mode: 'setup' | 'jwt' | 'token';
  setup_completed: boolean;
  jwt_available: boolean;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  try {
    const resp = await fetch(`${API}/admin/auth/status`);
    if (!resp.ok) return { mode: 'token', setup_completed: false, jwt_available: false };
    return resp.json();
  } catch {
    return { mode: 'token', setup_completed: false, jwt_available: false };
  }
}

export async function checkAdminAuth(token: string): Promise<boolean> {
  try {
    const resp = await fetch(`${API}/admin/auth/check`, { headers: authHeaders(token) });
    return resp.ok;
  } catch {
    return false;
  }
}

export async function loginWithCredentials(
  username: string, password: string,
): Promise<{ ok: boolean; token?: string; error?: string }> {
  try {
    const resp = await fetch(`${API}/admin/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      return { ok: false, error: d.detail || '登录失败' };
    }
    const d = await resp.json();
    return { ok: true, token: d.token };
  } catch {
    return { ok: false, error: '连接失败' };
  }
}

export async function setupAuth(
  username: string, password: string,
): Promise<{ ok: boolean; token?: string; error?: string }> {
  try {
    const resp = await fetch(`${API}/admin/auth/setup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      return { ok: false, error: d.detail || '设置失败' };
    }
    const d = await resp.json();
    return { ok: true, token: d.token };
  } catch {
    return { ok: false, error: '连接失败' };
  }
}

// ── Nodes ──

export async function getNodes(token: string): Promise<AdminNode[]> {
  const resp = await apiFetch('/admin/config/nodes', { headers: authHeaders(token) });
  return resp.json();
}

export function getConfigRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/config/raw', token);
}

export function updateConfigRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/config/raw', token, yaml);
}

// ── Multi-provider config ──

export interface ProviderConfigPublic {
  base_url: string;
  model: string;
  api_key_present: boolean;
  api_key_redacted: string;
}

export interface ProvidersResponse {
  active_provider: string;
  providers: Record<string, ProviderConfigPublic>;
  fallbacks: Array<Record<string, any>>;
  registered: string[];
}

export async function getProviders(token: string): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/providers', { headers: authHeaders(token) });
  return resp.json();
}

export async function upsertProvider(
  token: string,
  name: string,
  data: { base_url?: string; api_key?: string; model?: string },
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteProvider(token: string, name: string): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function setActiveProvider(token: string, provider: string): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/active-provider', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ provider }),
  });
  return resp.json();
}

export async function updateFallbacks(
  token: string,
  fallbacks: Array<Record<string, any>>,
): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/fallbacks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ fallbacks }),
  });
  return resp.json();
}

export function getRuntimeRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/runtime/raw', token);
}

export function updateRuntimeRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/runtime/raw', token, yaml);
}

export function getPolicyRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/policy/raw', token);
}

export function updatePolicyRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/policy/raw', token, yaml);
}

export function getSchedulesRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/schedules/raw', token);
}

export function updateSchedulesRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/schedules/raw', token, yaml);
}

export function getNodeRaw(token: string, nodeId: string): Promise<string> {
  return readRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token);
}

export function updateNodeRaw(token: string, nodeId: string, yaml: string): Promise<any> {
  return writeRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token, yaml);
}

export async function createNode(token: string, data: AdminCreatePayload): Promise<any> {
  // [2026-06-02] Create raw-backed node files through the existing admin endpoint.
  // Why: the backend currently accepts an id plus complete YAML content instead of a
  // higher-level template object. How: keep the wrapper close to that contract while
  // pages may build the content from a selected template. Purpose: node creation stays
  // compatible with Supervisor without adding another server schema.
  const resp = await apiFetch('/admin/config/nodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteNode(token: string, nodeId: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/nodes/${pathPart(nodeId)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getTools(token: string): Promise<AdminTool[]> {
  const resp = await apiFetch('/admin/config/tools', { headers: authHeaders(token) });
  return resp.json();
}

export function getToolRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token);
}

export function updateToolRaw(token: string, name: string, script: string): Promise<any> {
  return writeRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token, script);
}

export async function createTool(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/tools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteTool(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/tools/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getSkills(token: string): Promise<AdminSkill[]> {
  const resp = await apiFetch('/admin/config/skills', { headers: authHeaders(token) });
  return resp.json();
}

export function getSkillRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token);
}

export function updateSkillRaw(token: string, name: string, content: string): Promise<any> {
  return writeRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token, content);
}

export async function createSkill(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteSkill(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/skills/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getMcpClients(token: string): Promise<McpClient[]> {
  const resp = await apiFetch('/admin/config/mcp-clients', { headers: authHeaders(token) });
  return resp.json();
}

export function getMcpClientsRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/mcp-clients/raw', token);
}

export function updateMcpClientsRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/mcp-clients/raw', token, yaml);
}

export async function restartEngine(token: string): Promise<any> {
  // [2026-06-02] Expose restart as an explicit engine-only wrapper for Settings.
  // Why: the UI must not guess the RestartIn payload each time. How: send the
  // backend-required target and a Chinese reason string with admin auth. Purpose: the
  // dangerous action remains behind a confirm dialog while the API contract is fixed.
  const resp = await apiFetch('/admin/restart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ target: 'engine', reason: '用户从设置页面请求重启引擎' }),
  });
  return resp.json();
}

export async function reloadConfig(token: string): Promise<any> {
  const resp = await apiFetch('/config/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function reloadTools(token: string): Promise<any> {
  const resp = await apiFetch('/tools/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getAllToolNames(token: string): Promise<string[]> {
  // [2026-06-01] Why: approval preferences must list the backend's complete tool
  // set instead of a stale frontend constant. How: call the protected Supervisor
  // all-tool-names endpoint with the existing bearer header helper. Purpose: the
  // client settings page can show recommended tools plus every other tool returned
  // by the running server, while failed auth still falls back in the UI.
  const resp = await apiFetch('/admin/config/all-tool-names', { headers: authHeaders(token) });
  return resp.json();
}

// ── Model config ──

export async function getModelConfig(token: string): Promise<{
  model: string;
  base_url: string;
  api_key_present: boolean;
  api_key?: string;
}> {
  const resp = await apiFetch('/config/openai/secret', { headers: authHeaders(token) });
  return resp.json();
}

export async function updateModelConfig(
  token: string,
  params: { model?: string; base_url?: string; api_key?: string },
): Promise<any> {
  const resp = await apiFetch('/config/openai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export type SessionProviderOverride = Record<string, unknown>;

export async function getSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Fetch session-scoped provider overrides.
  // Why: the right panel must show the effective session model/base_url instead of
  // only the global OpenAI defaults. How: call Supervisor's admin-protected
  // provider_override endpoint with the same bearer token used by config APIs.
  // Purpose: model edits can affect only the selected session.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, { headers: authHeaders(token) });
  return resp.json();
}

export async function updateSessionProviderOverride(
  sessionId: string,
  token: string,
  params: SessionProviderOverride,
): Promise<SessionProviderOverride> {
  // [2026-06-01] Save session-scoped provider overrides.
  // Why: global model updates are too broad for the requested session panel. How:
  // PUT the complete override object, preserving fields the compact editor does not
  // touch. Purpose: allow per-session model, provider, api_key, and base_url edits.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export async function clearSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Clear session-scoped provider overrides.
  // Why: the session panel needs a direct way back to node/global defaults. How:
  // call DELETE on the same provider_override resource. Purpose: avoid saving empty
  // model fields that would be hard to distinguish from inherited defaults.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

// ── Approvals ──

export async function decideApproval(
  approvalId: string,
  decision: 'allow' | 'deny',
  comment = '',
): Promise<any> {
  // [2026-06-16] Attach stored Admin Token to approval decisions.
  // Why: the backend now protects this high-risk write endpoint. How: read the
  // saved settings token once and spread auth headers only when it exists. Purpose:
  // avoid sending an empty Bearer header while preserving unauthenticated failure.
  const _token = getStoredAdminToken();
  const resp = await apiFetch(`/approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(_token ? authHeaders(_token) : {}) },
    body: JSON.stringify({ decision, comment: comment || `${decision} via web` }),
  });
  return resp.json();
}

// ── Cancel ──

export async function cancelActiveTasks(sessionId: string): Promise<any> {
  const resp = await apiFetch(`/sessions/${sessionId}/cancel_active_tasks`, { method: 'POST' });
  return resp.json();
}

// ── Retry ──

export async function retryInbound(sessionId: string, sourceInboundSeq: number, newText?: string): Promise<{
  ok: boolean;
  new_inbound_seq?: number;
  source_inbound_seq?: number;
  truncated_messages?: number;
  error?: string;
}> {
  const payload: Record<string, unknown> = { source_inbound_seq: sourceInboundSeq };
  if (newText !== undefined) payload.new_text = newText;
  const resp = await apiFetch(`/sessions/${sessionId}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return resp.json();
}

// ── Pending approvals ──

export async function getSessionPendingApprovals(sessionId: string): Promise<any[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/pending_approvals`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Preempt ──

export async function preemptTask(taskId: string, message?: string, attachments?: any[]): Promise<any> {
  const body: Record<string, unknown> = {};
  if (message) body.message = message;
  if (attachments && attachments.length > 0) body.attachments = attachments;
  const resp = await apiFetch(`/tasks/${encodeURIComponent(taskId)}/preempt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return resp.json();
}

// ── Reset conversation ──

export async function resetConversation(conversationKey: string): Promise<any> {
  // [2026-06-16] Attach stored Admin Token to conversation reset.
  // Why: reset changes the web session mapping and is now admin-protected. How: use
  // getStoredAdminToken and omit Authorization when no token is saved. Purpose:
  // keep the existing one-argument API helper compatible with protected backend use.
  const _token = getStoredAdminToken();
  const resp = await apiFetch('/conversations/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(_token ? authHeaders(_token) : {}) },
    body: JSON.stringify({ conversation_key: conversationKey }),
  });
  return resp.json();
}

// ── Active node ──

export interface ActiveNodeResponse {
  node_id: string;
  is_override: boolean;
  default_node_id: string;
  session_id?: string;
  effective_provider?: string;
  effective_model?: string;
  effective_base_url?: string;
  node_provider?: string;
  node_model?: string;
}

export async function getActiveNode(sessionId: string): Promise<ActiveNodeResponse> {
  const resp = await apiFetch(`/sessions/${sessionId}/active_node`);
  return resp.json();
}

export async function switchNode(sessionId: string, targetNodeId: string): Promise<ActiveNodeResponse & { ok: boolean; target_node_id: string; error?: string }> {
  const resp = await apiFetch(`/sessions/${sessionId}/switch_node`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_node_id: targetNodeId }),
  });
  return resp.json();
}

// ── App config (public, no auth needed) ──

export interface AppConfigPublic {
  version?: number;
  provider: string;
  openai: { model: string; base_url: string; api_key_present?: boolean; api_key?: string };
  entry_node_id?: string;
  default_entry_node_id?: string;
  shell?: { entry_node_id?: string };
  // [2026-06-07] Provider name → default model mapping for all configured providers.
  provider_models?: Record<string, string>;
}

export async function getConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Expose the public config endpoint under its route name as well as
  // getAppConfig. Why: the Client settings page needs to read the currently
  // configured entry_node_id when the backend includes it, while older callers still
  // use getAppConfig for model display. How: return the same /v1/config response with
  // optional entry-node fields in the TypeScript shape. Purpose: selection state can
  // prefer real Supervisor configuration without breaking existing config consumers.
  const resp = await apiFetch('/config');
  return resp.json();
}

export function getAppConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Keep the historical helper as an alias to getConfig.
  // Why: Header and session panels already call getAppConfig. How: delegate to the
  // route-named wrapper instead of duplicating fetch logic. Purpose: both old and new
  // settings code share one public config contract.
  return getConfig();
}

// ── List sessions ──

export interface SessionListItem {
  session_id: string;
  conversation_key: string;
  channel: string;
  parent_session_id?: string;
  created_at: string;
  updated_at: string;
}

export async function listSessions(channel = 'web', limit = 50): Promise<SessionListItem[]> {
  try {
    const params = new URLSearchParams({ limit: String(limit) });
    if (channel) params.set('channel', channel);
    const resp = await fetch(`${API}/sessions?${params.toString()}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

export interface ContextWindowResponse {
  session_id: string;
  prompt_tokens: number;
  effective_tokens: number;
  compact_threshold: number;
  utilization: number;
  source: string;
  message_count: number;
}

export async function getContextWindow(sessionId: string): Promise<ContextWindowResponse> {
  // [2026-06-05] Why: the chat composer needs an initial context-token snapshot
  // before the next LLM call produces a WebSocket update. How: call the dedicated
  // session endpoint with an encoded path segment and return the backend JSON shape.
  // Purpose: chatStore has one typed API wrapper for context-window hydration.
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/context_window`);
  return resp.json();
}

export interface CompactSessionResponse {
  status: string;
  session_id?: string;
  keep_recent?: number;
  inbound_seq?: number;
}

export async function compactSession(sessionId: string, keepRecent = 6): Promise<CompactSessionResponse> {
  // [2026-06-06] Why: manual context compression should be invoked through one
  // typed client helper instead of inline fetch calls in UI components. How: POST
  // keep_recent as JSON to the session compact endpoint with the session id encoded
  // as one path segment. Purpose: the ContextPopover can stay focused on UI state.
  // [2026-06-16] Why: the backend now requires Admin Token for this web-only write.
  // How: read the stored token and include auth headers only when a token exists.
  // Purpose: preserve existing callers while protecting manual compaction.
  const _token = getStoredAdminToken();
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/compact`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(_token ? authHeaders(_token) : {}) },
    body: JSON.stringify({ keep_recent: keepRecent }),
  });
  return resp.json();
}

// ── Delete session ──

export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  try {
    // [2026-06-16] Attach stored Admin Token to session deletion.
    // Why: deleting a session removes server and conversation-store state. How: build
    // the DELETE request with Authorization only when the settings token is present.
    // Purpose: keep unauthenticated deletion blocked without emitting an empty token.
    const _token = getStoredAdminToken();
    const resp = await fetch(`${API}/sessions/${sessionId}`, {
      method: 'DELETE',
      ...(_token ? { headers: authHeaders(_token) } : {}),
    });
    if (!resp.ok) return { ok: false };
    return resp.json();
  } catch {
    return { ok: false };
  }
}

// ── Session messages (legacy, flat text) ──

export async function getSessionMessages(sessionId: string, limit = 200): Promise<{ role: string; content: string }[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/messages?limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Structured history (ConversationStore) ──

export interface StructuredThinkingBlock {
  text: string;
  started_at?: string;
  ended_at?: string;
  // [AutoC 2026-06-04] Why: mixed backend versions may serialize structured
  // thinking timestamps in camelCase. How: type both naming conventions. Purpose:
  // history hydration keeps elapsed-time headers instead of falling back to length.
  startedAt?: string;
  endedAt?: string;
}

export interface StructuredMessage {
  id: string;
  role: string;
  content: string | Array<Record<string, unknown>>;
  attachments?: Array<{ name?: string; size?: number; url?: string; type?: string; path?: string; mime_type?: string }>;
  message_type?: string;
  created_at?: string;
  source_node_id?: string;
  // [AutoC 2026-06-03] Why: dispatch-result history rows need the same structured
  // child navigation metadata as realtime WebSocket payloads. How: expose the
  // selected backend metadata fields returned by /history. Purpose: the UI can show
  // a child-session jump button after page refresh without parsing callback text.
  source_task_id?: string;
  // [AutoC 2026-06-04] Why: history rows may be correlated with live request cards
  // after request-scoped streaming. How: type the optional backend request id.
  // Purpose: future hydration can reconcile one persisted assistant row with one
  // request card without falling back to task-level matching.
  llm_request_id?: string;
  child_session_id?: string;
  // [AutoC 2026-06-04] Why: dispatch_result history now uses explicit child/caller
  // metadata and summary. How: expose the new fields while keeping legacy dispatch_*
  // fields typed for old rows. Purpose: TypeScript callers can render refreshed
  // callback cards from structure instead of localized content text.
  child_task_id?: string;
  child_node_id?: string;
  caller_node_id?: string;
  summary?: string;
  dispatch_task_id?: string;
  dispatch_node_id?: string;
  // [AutoC 2026-06-04] Why: historical reasoning needs the same timing metadata as
  // live ThinkingBlock cards. How: accept the new backend thinking_blocks array while
  // keeping flat string/object thinking for old rows. Purpose: refreshed history can
  // render elapsed thinking time instead of falling back to character counts.
  thinking?: string | StructuredThinkingBlock;
  thinking_text?: string;
  thinking_blocks?: StructuredThinkingBlock[];
  // Clonoth format: {id, name, arguments(object)}
  tool_calls?: Array<{ id?: string; name: string; arguments?: Record<string, unknown> }>;
  tool_call_id?: string;
  tool_name?: string;
  name?: string;
  tool_rejected?: boolean;
  tool_rejection_code?: string;
  tool_result_visibility?: string;
  tool_result_structured?: Record<string, unknown>;
  raw_inline?: string;
  tool_result_raw_inline?: string;
  format?: string;
  tool_result_format?: string;
  elapsed_ms?: number;
  tool_result_elapsed_ms?: number;
  tool_summary?: string;
  tool_result_summary?: string;
  tool_attachments?: Array<{ name?: string; size?: number; url?: string; type?: string; path?: string; mime_type?: string }>;
  provider?: string;
  tool_mode?: string;
  provider_metadata?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  tool_call_ids?: string[];
  // [thinking-time 2026-06-01] Precise reasoning timing from backend meta.
  reasoning_started_at?: string;
  reasoning_ended_at?: string;
  // [2026-06-06] Preempt message flag from meta.preempt.
  is_preempt?: boolean;
}

export interface ChildSessionInfo {
  // [2026-06-03] Why: child session rows come from sessions.json, not chat history.
  // How: mirror the backend's snake_case response so chatStore can normalize it into
  // ChildNodeState. Purpose: refresh can restore child-node status and navigation.
  session_id: string;
  parent_session_id?: string;
  route_parent_session_id?: string;
  node_id?: string;
  context_key?: string;
  context_mode?: string;
  status?: string;
  task_id?: string;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface HistoryPageResponse {
  messages: StructuredMessage[];
  total: number;
  has_more: boolean;
}

export async function getSessionHistory(sessionId: string, limit = 0, taskId?: string, offset = 0): Promise<StructuredMessage[]> {
  const page = await getSessionHistoryPage(sessionId, limit, offset, taskId);
  return page.messages;
}

export async function getSessionHistoryPage(
  sessionId: string, limit = 0, offset = 0, taskId?: string,
): Promise<HistoryPageResponse> {
  try {
    let url = `${API}/sessions/${sessionId}/history?limit=${limit}&offset=${offset}`;
    if (taskId) url += `&task_id=${encodeURIComponent(taskId)}`;
    const resp = await fetch(url);
    if (!resp.ok) return { messages: [], total: 0, has_more: false };
    const data = await resp.json();
    // Support both old (array) and new (object with messages) response formats
    if (Array.isArray(data)) return { messages: data, total: data.length, has_more: false };
    return data;
  } catch {
    return { messages: [], total: 0, has_more: false };
  }
}

export async function getSessionChildren(sessionId: string): Promise<ChildSessionInfo[]> {
  try {
    // [2026-06-03] Why: child-session metadata is stored in the supervisor registry,
    // while /history only returns messages. How: call the dedicated read-only endpoint
    // and tolerate missing older backends by returning an empty array. Purpose: the web
    // app can be deployed independently while still restoring childNodes when possible.
    const resp = await fetch(`${API}/sessions/${sessionId}/children`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Running tasks (for reconnect state recovery) ──

export interface RunningTaskInfo {
  task_id: string;
  session_id: string;
  node_id?: string;
  status?: string;
  is_user_entry?: boolean;
  source_inbound_seq?: number | null;
}

export async function getSessionRunningTasks(sessionId: string): Promise<RunningTaskInfo[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/running_tasks`);
    if (!resp.ok) return [];
    const data = await resp.json();
    return data.tasks || [];
  } catch {
    return [];
  }
}

// ── Legacy compat exports ──

export const sendInbound = postInbound;
