// [2026-09-12] Minimal offline checks for the web frontend.
// Why: this repo has no test runner (package.json only ships dev/build/preview)
// and no vitest dependency. How: plain Node assertions over the pure store
// helpers, bundled with the esbuild that already ships with Vite and run with
// `node`. Purpose: the fragile parts of the web features (child-session keys,
// dispatch routing, request-id attribution, response_info notices, approval
// levels, panel entry validation) get one runnable regression each without
// adding dependencies.
//
// Run: npx esbuild test/offline.test.ts --bundle --platform=node --format=esm \
//        --outfile=/tmp/clonoth-offline.mjs && node /tmp/clonoth-offline.mjs
import assert from 'node:assert/strict';

import { getChildNodeSessionId } from '../src/store/eventRouting';
import {
  CHILD_NODE_TERMINAL_VISIBLE_MS,
  createStoreBase,
  mergeChildNodesFromSessionChildren,
  resolveChildSessionHistoryView,
  shouldHideChildNode,
} from '../src/store/chatTypes';
import { createInitialChatState, reduceChatEvent } from '../src/store/eventReducer';
import { loadChildSessionHistoryIntoStore } from '../src/store/historyLoader';
import { mergeSource, normalizeResponseInfo } from '../src/store/eventReducerState';
import { hydrateStructuredHistory, normalizeHistoryThinkingSegments } from '../src/store/historyHydration';
import { compileApprovalPreset, identifyApprovalPreset } from '../src/store/sessionApprovalRules';
import { shouldAutoApproveTool } from '../src/store/clientPrefsStore';
import { resolvePanelEntry } from '../src/components/plugins/panelEntry';
import type { SupervisorEvent } from '../src/types/chat';
import type { ChatStoreState, StoreGetter, StoreSetter } from '../src/store/chatTypes';
import type { StructuredMessage } from '../src/api/supervisorClient';
import type { MessageSource } from '../src/types/message';

const checks: Array<[string, () => void | Promise<void>]> = [];
const check = (name: string, run: () => void | Promise<void>) => checks.push([name, run]);

// ── child session index ────────────────────────────────────────────────────

check('child nodes are indexed by session_id, not node_id', () => {
  const merged = mergeChildNodesFromSessionChildren({}, [
    { session_id: 's1', node_id: 'coder', status: 'running', started_at: 'T0' },
    { session_id: 's2', node_id: 'coder', status: 'completed', started_at: 'T1', completed_at: 'T2' },
  ], 'conv1');
  assert.deepEqual(Object.keys(merged).sort(), ['s1', 's2']);
  assert.equal(merged.s1.nodeId, 'coder');
  assert.equal(merged.s2.status, 'completed');
  assert.equal(merged.s2.completedAt, 'T2');
});

check('running child rows do not inherit a completion timestamp from updated_at', () => {
  const merged = mergeChildNodesFromSessionChildren({}, [
    { session_id: 's1', node_id: 'coder', status: 'running', started_at: 'T0', updated_at: 'T9' },
  ], 'conv1');
  assert.equal(merged.s1.completedAt, undefined);
});

// ── dispatch event routing ─────────────────────────────────────────────────

const event = (payload: Record<string, unknown>, sessionId = 'branch-session'): SupervisorEvent => ({
  seq: 1, event_id: 'e1', ts: 'T', session_id: sessionId, type: 'task_started', payload,
});

check('dispatch sub-tasks route to the dispatch session id', () => {
  const routed = getChildNodeSessionId(event({
    input: { _dispatch_origin: { kind: 'spawn_worker' }, parent_session_id: 'dispatch-session' },
  }), {
    input: { _dispatch_origin: { kind: 'spawn_worker' }, parent_session_id: 'dispatch-session' },
  } as Record<string, unknown>);
  assert.equal(routed, 'dispatch-session');
});

check('regular child events keep the child session id', () => {
  const payload = { child_session_id: 'child-1', input: { parent_session_id: 'other' } };
  assert.equal(getChildNodeSessionId(event(payload), payload), 'child-1');
});

// ── history empty state ───────────────────────────────────────────────────

check('store base starts with unknown (not empty) child history', () => {
  const base = createStoreBase();
  assert.deepEqual(base.childSessionEmpty, {});
  assert.deepEqual(base.childSessionMessages, {});
});

// ── request id usage attribution ──────────────────────────────────────────

check('response_info belongs to one llm request id', () => {
  const info = { provider: 'openai', model: 'gpt-x', notices: [{ code: 'fallback', text: 'fallback used' }] };
  const current: MessageSource = { llmRequestId: 'req-1', responseInfo: info };
  // Later stream patches for the same request keep the response info.
  assert.deepEqual(mergeSource(current, { llmRequestId: 'req-1' }).responseInfo, info);
  // A different request id means a new assistant turn: old notices must not leak.
  assert.equal(mergeSource(current, { llmRequestId: 'req-2' }).responseInfo, undefined);
  // The new request's own response info wins.
  const next = mergeSource(current, { llmRequestId: 'req-2', responseInfo: { model: 'gpt-y' } });
  assert.deepEqual(next.responseInfo, { model: 'gpt-y' });
});

// ── response_info notices stay independent ────────────────────────────────

check('raw response notices stay separate from provider metadata', () => {
  const info = normalizeResponseInfo({
    provider: 'anthropic',
    model: 'claude-x',
    usage: { input_tokens: 3 },
    notices: [
      { code: 'retry', label: '重试', text: '上游 429，已重试一次' },
      { code: 'ignored', text: 42 },
      'not-a-record',
    ],
    unexpected: { nested: true },
  });
  assert.deepEqual(info, {
    provider: 'anthropic',
    model: 'claude-x',
    usage: { input_tokens: 3 },
    notices: [{ code: 'retry', label: '重试', text: '上游 429，已重试一次' }],
  });
});

check('malformed response_info is dropped instead of crashing the card', () => {
  assert.equal(normalizeResponseInfo(undefined), undefined);
  assert.equal(normalizeResponseInfo('nope'), undefined);
  assert.equal(normalizeResponseInfo({ notices: 'nope' }), undefined);
});

// ── reasoning display contract ────────────────────────────────────────────

check('thinking_blocks win over the legacy thought_summary fallback', () => {
  const segments = normalizeHistoryThinkingSegments({
    thinking_blocks: [{ text: 'structured reasoning', started_at: 'T0' }],
    thought_summary: 'legacy summary',
  } as unknown as StructuredMessage);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, 'structured reasoning');
});

check('thought_summary is only a fallback for old rows', () => {
  const segments = normalizeHistoryThinkingSegments({
    thought_summary: 'legacy summary',
  } as unknown as StructuredMessage);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].text, 'legacy summary');
});

// ── approval level boundaries ─────────────────────────────────────────────

check('approval levels keep manual and smart guards, yolo bypasses them', () => {
  const rules = { execute_command: false };
  assert.equal(shouldAutoApproveTool('execute_command', rules, 'manual'), false);
  assert.equal(shouldAutoApproveTool('execute_command', rules, 'smart'), false);
  assert.equal(shouldAutoApproveTool('read_file', rules, 'smart'), true);
  assert.equal(shouldAutoApproveTool('execute_command', rules, 'yolo'), true);
  assert.equal(shouldAutoApproveTool('request_restart', {}, 'smart'), false);
  assert.equal(shouldAutoApproveTool('request_restart', {}, 'yolo'), true);
});

check('session presets serialize sparse rules and recognize normalized smart rules', () => {
  const rules = compileApprovalPreset('smart', { read_file: true, search_web: false, request_restart: false });
  assert.equal(rules.rules.length, 4);
  assert.deepEqual(rules.rules[3], { tool: 'search_web', action: 'ask' });
  assert.equal(identifyApprovalPreset(rules).level, 'smart');
  const reordered = { ...rules, rules: [...rules.rules].reverse() };
  assert.equal(identifyApprovalPreset(reordered).level, 'smart');
  assert.equal(identifyApprovalPreset(null).level, 'custom');
  assert.equal(identifyApprovalPreset(compileApprovalPreset('manual')).level, 'manual');
  assert.equal(identifyApprovalPreset(compileApprovalPreset('yolo')).level, 'yolo');
  assert.equal(identifyApprovalPreset({ ...rules, rules: [{ tool: 'mcp_*', action: 'ask' }] }).level, 'custom');
  assert.equal(identifyApprovalPreset({ ...rules, rules: [...rules.rules, { operation: 'write_file', action: 'ask' }] }).level, 'custom');
});

// ── plugin panel entry validation ─────────────────────────────────────────

const ORIGIN = 'https://clonoth.example';

check('panel entry accepts same-origin plugin statics only', () => {
  assert.deepEqual(
    resolvePanelEntry('/v1/plugins/demo/web/index.html?tab=1', ORIGIN),
    { path: '/v1/plugins/demo/web/index.html?tab=1' },
  );
  assert.ok(resolvePanelEntry('https://evil.example/v1/plugins/demo/web/index.html', ORIGIN).error);
  assert.ok(resolvePanelEntry('/v1/plugins/demo/web/index.html#frag', ORIGIN).error);
  assert.ok(resolvePanelEntry('/v1/admin/state', ORIGIN).error);
  assert.ok(resolvePanelEntry('/v1/plugins/demo/index.html', ORIGIN).error);
  assert.ok(resolvePanelEntry('https://user:pw@clonoth.example/v1/plugins/demo/web/x.html', ORIGIN).error);
});

// ── child node terminal visibility (sidebar + floating panel share one rule) ──

check('a re-opened child node drops the stale completion timestamp', () => {
  // 会话恢复或同一 session 开了新任务：终态 -> running，旧 completedAt 必须消失，
  // 否则侧边栏/浮动面板的 30 秒规则会把正在跑的行隐藏。
  const existing = {
    s1: {
      sessionId: 's1',
      nodeId: 'coder',
      parentConversationId: 'conv1',
      status: 'completed' as const,
      taskId: 'task-a',
      startedAt: 'T0',
      completedAt: 'T2',
    },
  };
  const merged = mergeChildNodesFromSessionChildren(existing, [
    { session_id: 's1', node_id: 'coder', status: 'running', task_id: 'task-b', started_at: 'T3' },
  ], 'conv1');
  assert.equal(merged.s1.status, 'running');
  assert.equal(merged.s1.taskId, 'task-b');
  assert.equal(merged.s1.completedAt, undefined);
  assert.equal(merged.s1.startedAt, 'T3');
});

check('a non-terminal row never inherits completedAt even without a task change', () => {
  const existing = {
    s1: {
      sessionId: 's1',
      nodeId: 'coder',
      parentConversationId: 'conv1',
      status: 'completed' as const,
      taskId: 'task-a',
      startedAt: 'T0',
      completedAt: 'T2',
    },
  };
  const merged = mergeChildNodesFromSessionChildren(existing, [
    { session_id: 's1', node_id: 'coder', status: 'awaiting_approval', task_id: 'task-a' },
  ], 'conv1');
  assert.equal(merged.s1.status, 'awaiting_approval');
  assert.equal(merged.s1.completedAt, undefined);
  assert.equal(merged.s1.startedAt, 'T0');
});

check('terminal rows keep the backend completion timestamp', () => {
  const merged = mergeChildNodesFromSessionChildren({}, [
    { session_id: 's1', node_id: 'coder', status: 'failed', started_at: 'T0', completed_at: 'T2' },
  ], 'conv1');
  assert.equal(merged.s1.completedAt, 'T2');
  // 同一子节点再次进入终态但后端没给 completed_at：沿用旧值（仍是终态）。
  const again = mergeChildNodesFromSessionChildren(merged, [
    { session_id: 's1', node_id: 'coder', status: 'completed' },
  ], 'conv1');
  assert.equal(again.s1.completedAt, 'T2');
});

check('the 30s auto-hide applies only to real terminal states', () => {
  const now = Date.parse('2026-10-02T00:00:00Z');
  const stale = new Date(now - CHILD_NODE_TERMINAL_VISIBLE_MS - 1000).toISOString();
  const fresh = new Date(now - 1000).toISOString();
  assert.equal(shouldHideChildNode({ status: 'completed', completedAt: stale }, now), true);
  assert.equal(shouldHideChildNode({ status: 'failed', completedAt: stale }, now), true);
  assert.equal(shouldHideChildNode({ status: 'cancelled', completedAt: stale }, now), true);
  assert.equal(shouldHideChildNode({ status: 'completed', completedAt: fresh }, now), false);
  // 非终态即使残留旧的 completedAt 也继续显示（本次修复的核心断言）。
  assert.equal(shouldHideChildNode({ status: 'running', completedAt: stale }, now), false);
  assert.equal(shouldHideChildNode({ status: 'awaiting_approval', completedAt: stale }, now), false);
  assert.equal(shouldHideChildNode({ status: 'completed' }, now), false);
  assert.equal(shouldHideChildNode({ status: 'completed', completedAt: 'not-a-date' }, now), false);
});

// ── child session history view states ─────────────────────────────────────

check('child history view states are mutually exclusive', () => {
  assert.equal(resolveChildSessionHistoryView(2, false, false), 'content');
  assert.equal(resolveChildSessionHistoryView(0, false, false), 'loading');
  assert.equal(resolveChildSessionHistoryView(0, true, false), 'empty');
  assert.equal(resolveChildSessionHistoryView(0, false, true), 'failed');
  // 失败后事件流又补进了消息：以内容为准，不能被失败态挡住。
  assert.equal(resolveChildSessionHistoryView(1, false, true), 'content');
});

// ── child session history loading (offline fetch stubs) ───────────────────

type StubResponse = { ok: boolean; status: number; json: () => Promise<unknown> };

// 审批用例把 fetch 换成了永不 resolve 的桩；这里保存并在每个用例后还原。
const neverResolvingFetch = globalThis.fetch;

async function withFetch(
  handler: (url: string) => StubResponse | Promise<StubResponse>,
  run: () => Promise<void>,
): Promise<void> {
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const url = typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.href
        : String((input as Request).url);
    return Promise.resolve().then(() => handler(url)) as unknown as Promise<Response>;
  }) as typeof fetch;
  try {
    await run();
  } finally {
    globalThis.fetch = neverResolvingFetch;
  }
}

function createFakeChatStore() {
  let state = { ...createInitialChatState(), ...createStoreBase() } as unknown as ChatStoreState;
  const set: StoreSetter = (partial) => {
    const patch = typeof partial === 'function' ? partial(state) : partial;
    state = { ...state, ...patch } as ChatStoreState;
  };
  const get: StoreGetter = () => state;
  return { set, get, snapshot: () => state };
}

const okEmptyHistory = (): StubResponse => ({
  ok: true,
  status: 200,
  json: async () => ({ messages: [], total: 0, has_more: false }),
});

check('a failed child history fetch lands in failed, not in endless loading', async () => {
  const store = createFakeChatStore();
  const urls: string[] = [];
  await withFetch((url) => {
    urls.push(url);
    return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) };
  }, async () => {
    await loadChildSessionHistoryIntoStore('child-fail', store.set);
  });
  const snapshot = store.snapshot();
  assert.match(urls[0], /\/v1\/sessions\/child-fail\/history/);
  assert.deepEqual(snapshot.childSessionMessages['child-fail'], []);
  assert.equal(snapshot.childSessionLoadFailed['child-fail'], true);
  assert.equal(snapshot.childSessionEmpty['child-fail'], false);
  assert.equal(
    resolveChildSessionHistoryView(
      snapshot.childSessionMessages['child-fail'].length,
      snapshot.childSessionEmpty['child-fail'],
      snapshot.childSessionLoadFailed['child-fail'],
    ),
    'failed',
  );
});

check('a network error is reported as failed instead of a silent no-op', async () => {
  const store = createFakeChatStore();
  await withFetch(() => {
    throw new Error('network down');
  }, async () => {
    await loadChildSessionHistoryIntoStore('child-net', store.set);
  });
  const snapshot = store.snapshot();
  assert.equal(snapshot.childSessionLoadFailed['child-net'], true);
  assert.equal(snapshot.childSessionEmpty['child-net'], false);
});

check('a 404 child session counts as cleaned/no records, not as a failure', async () => {
  const store = createFakeChatStore();
  await withFetch(() => ({ ok: false, status: 404, json: async () => ({ detail: 'not found' }) }), async () => {
    await loadChildSessionHistoryIntoStore('child-404', store.set);
  });
  const snapshot = store.snapshot();
  assert.equal(snapshot.childSessionEmpty['child-404'], true);
  assert.equal(snapshot.childSessionLoadFailed['child-404'], false);
});

check('a successful empty history settles on the empty state', async () => {
  const store = createFakeChatStore();
  await withFetch(okEmptyHistory, async () => {
    await loadChildSessionHistoryIntoStore('child-empty', store.set);
  });
  const snapshot = store.snapshot();
  assert.deepEqual(snapshot.childSessionMessages['child-empty'], []);
  assert.equal(snapshot.childSessionEmpty['child-empty'], true);
  assert.equal(snapshot.childSessionLoadFailed['child-empty'], false);
});

check('a stale child history response cannot overwrite a newer one', async () => {
  const store = createFakeChatStore();
  let releaseFirst: () => void = () => {};
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  let call = 0;
  await withFetch(async () => {
    call += 1;
    if (call === 1) {
      // 第一次请求（旧序号）被拖慢：如果晚到的响应能写回，就会把失败态覆盖成空态。
      await firstGate;
      return okEmptyHistory();
    }
    return { ok: false, status: 500, json: async () => ({ detail: 'boom' }) };
  }, async () => {
    const first = loadChildSessionHistoryIntoStore('child-race', store.set);
    const second = loadChildSessionHistoryIntoStore('child-race', store.set);
    await second;
    releaseFirst();
    await first;
  });
  const snapshot = store.snapshot();
  // 最新一次请求（失败）的结果必须保留。
  assert.equal(snapshot.childSessionLoadFailed['child-race'], true);
  assert.equal(snapshot.childSessionEmpty['child-race'], false);
});

check('a child history failure keeps previously loaded messages', async () => {
  const store = createFakeChatStore();
  // 先制造一次成功加载（事件流也会这样写缓存），再让重试失败。
  let failNext = false;
  await withFetch(async () => {
    if (failNext) return { ok: false, status: 503, json: async () => ({ detail: 'down' }) };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        messages: [{ id: 'm1', role: 'assistant', content: 'child output', created_at: '2026-10-02T00:00:00Z' }],
        total: 1,
        has_more: false,
      }),
    };
  }, async () => {
    await loadChildSessionHistoryIntoStore('child-keep', store.set);
    failNext = true;
    await loadChildSessionHistoryIntoStore('child-keep', store.set);
  });
  const snapshot = store.snapshot();
  assert.equal(snapshot.childSessionLoadFailed['child-keep'], true);
  assert.equal(snapshot.childSessionMessages['child-keep'].length, 1);
  assert.equal(snapshot.childSessionEmpty['child-keep'], false);
});

// ── reconnect history hydration preempt deduplication ─────────────────────

check('preserve-mode hydration deduplicates live/prior preempts and scopes offline preempts to their own task', () => {
  const sessionId = 'sess-preempt';
  const conversationId = 'conv-preempt';
  let live = createInitialChatState();
  const emit = (seq: number, type: SupervisorEvent['type'], payload: Record<string, unknown>, ts: string) => {
    live = reduceChatEvent(live, {
      seq,
      event_id: `ev-${seq}`,
      ts,
      session_id: sessionId,
      type,
      payload: { conversation_key: `web:${conversationId}`, ...payload },
    });
  };

  // Turn 1: live user message + assistant + user preempt + async tool preempt
  emit(1, 'inbound_message', { text: 'first turn' }, '2026-11-13T07:00:00Z');
  emit(2, 'stream_delta', { task_id: 'task-1', source_inbound_seq: 1, type: 'text', content: 'working 1' }, '2026-11-13T07:00:01Z');
  emit(3, 'preempt_injected', { task_id: 'task-1', source_inbound_seq: 1, message: 'old user preempt' }, '2026-11-13T07:00:02Z');
  emit(4, 'preempt_injected', {
    task_id: 'task-1',
    source_inbound_seq: 1,
    message: '<dispatch_result kind="async_tool">\ndone\n</dispatch_result>',
  }, '2026-11-13T07:00:03Z');
  emit(5, 'outbound_message', { task_id: 'task-1', source_inbound_seq: 1, text: 'turn 1 finished', action_type: 'finish' }, '2026-11-13T07:00:04Z');

  // Turn 2: currently generating; one live preempt seen, one offline preempt missed while disconnected
  emit(6, 'inbound_message', { text: 'second turn' }, '2026-11-13T07:01:00Z');
  emit(7, 'stream_delta', { task_id: 'task-2', source_inbound_seq: 6, type: 'text', content: 'working 2' }, '2026-11-13T07:01:01Z');
  emit(8, 'preempt_injected', { task_id: 'task-2', source_inbound_seq: 6, message: 'live preempt 2' }, '2026-11-13T07:01:02Z');

  const history: StructuredMessage[] = [
    { id: 'u1', role: 'user', content: 'first turn', source_task_id: 'task-1', created_at: '2026-11-13T07:00:00Z' },
    { id: 'a1', role: 'assistant', content: 'turn 1 finished', source_task_id: 'task-1', created_at: '2026-11-13T07:00:01Z' },
    { id: 'p1', role: 'user', content: 'old user preempt', is_preempt: true, source_task_id: 'task-1', created_at: '2026-11-13T07:00:02Z' },
    { id: 'p2', role: 'user', content: '<dispatch_result kind="async_tool">\ndone\n</dispatch_result>', is_preempt: true, source_task_id: 'task-1', created_at: '2026-11-13T07:00:03Z' },
    { id: 'p-orphan', role: 'user', content: 'ancient unrendered preempt', is_preempt: true, source_task_id: 'task-0', created_at: '2026-11-13T06:50:00Z' },
    { id: 'u2', role: 'user', content: 'second turn', source_task_id: 'task-2', created_at: '2026-11-13T07:01:00Z' },
    { id: 'a2', role: 'assistant', content: 'working 2', source_task_id: 'task-2', created_at: '2026-11-13T07:01:01Z' },
    { id: 'p3', role: 'user', content: 'live preempt 2', is_preempt: true, source_task_id: 'task-2', created_at: '2026-11-13T07:01:02Z' },
    { id: 'p4', role: 'user', content: 'missed while offline', is_preempt: true, source_task_id: 'task-2', created_at: '2026-11-13T07:01:03Z' },
  ];

  // Reconnect #1 while generating (preserveExistingMessages = true)
  const reconnectedOnce = hydrateStructuredHistory(live, sessionId, conversationId, history, true);
  // Reconnect #2 while still generating must remain idempotent
  const reconnectedTwice = hydrateStructuredHistory(reconnectedOnce, sessionId, conversationId, history, true);

  const order = reconnectedTwice.messageOrderByConversation[conversationId] || [];
  const lastMsg = reconnectedTwice.messagesById[order[order.length - 1]];
  assert.equal(lastMsg.role, 'assistant');
  assert.equal(lastMsg.source.taskId, 'task-2');

  const lastPreemptTexts = lastMsg.blocks
    .filter((b) => b.kind === 'notice' && b.level === 'preempt')
    .map((b) => (b.kind === 'notice' ? b.text : ''));

  // Only task-2's live preempt (1 copy) + missed offline preempt (1 copy); never task-1's or task-0's!
  assert.deepEqual(lastPreemptTexts, ['live preempt 2', 'missed while offline']);

  // Turn 1's assistant card keeps only its original 2 preempts without duplication
  const turn1Assistant = order
    .map((id) => reconnectedTwice.messagesById[id])
    .find((m) => m?.role === 'assistant' && m.source.taskId === 'task-1');
  assert.ok(turn1Assistant);
  const turn1Preempts = turn1Assistant.blocks.filter((b) => b.kind === 'notice' && b.level === 'preempt');
  assert.equal(turn1Preempts.length, 2);

  // Unmatched ancient preempt is not appended as a standalone user card at the bottom
  assert.equal(
    order.some((id) => reconnectedTwice.messagesById[id]?.blocks.some((b) => b.kind === 'text' && b.text === 'ancient unrendered preempt')),
    false,
  );
});

// ── approval card matching under repeated provider tool_call_id ("call_1") ──

check('live stream and reconnect replay attach approvals to the active call_1 card instead of historical call_1 cards', () => {
  const sessionId = 'sess-approval';
  const conversationId = 'conv-approval';
  const history: StructuredMessage[] = [
    { id: 'u1', role: 'user', content: 'turn 1', source_task_id: 'task-old', created_at: '2026-11-13T10:00:00Z' },
    {
      id: 'a1',
      role: 'assistant',
      content: '',
      source_task_id: 'task-old',
      created_at: '2026-11-13T10:00:01Z',
      tool_calls: [{ id: 'call_1', name: 'execute_command', arguments: { command: 'echo old' } }],
    },
    { id: 't1', role: 'tool', tool_call_id: 'call_1', name: 'execute_command', content: 'old ok', created_at: '2026-11-13T10:00:02Z' },
    { id: 'a1-done', role: 'assistant', content: 'done 1', source_task_id: 'task-old', created_at: '2026-11-13T10:00:03Z' },
  ];

  // 1. Live stream path: history has an old completed call_1, current task runs two call_1 steps
  let live = hydrateStructuredHistory(createInitialChatState(), sessionId, conversationId, history, false);
  const oldStableId = live.toolExecutionOrder[0];
  assert.equal(live.toolExecutionsById[oldStableId].id, 'call_1');
  assert.equal(live.toolExecutionsById[oldStableId].status, 'success');

  const emitLive = (seq: number, type: SupervisorEvent['type'], payload: Record<string, unknown>, ts: string) => {
    live = reduceChatEvent(live, {
      seq,
      event_id: `live-ap-${seq}`,
      ts,
      session_id: sessionId,
      type,
      payload: { conversation_key: `web:${conversationId}`, ...payload },
    });
  };

  emitLive(10, 'inbound_message', { text: 'deploy now' }, '2026-11-13T10:01:00Z');
  emitLive(11, 'tool_call_start', {
    task_id: 'task-new', source_inbound_seq: 10, llm_request_id: 'req-step-1',
    tool_call_id: 'call_1', tool_name: 'execute_command', arguments: { command: 'echo check' },
  }, '2026-11-13T10:01:01Z');
  emitLive(12, 'tool_call_end', {
    task_id: 'task-new', source_inbound_seq: 10, llm_request_id: 'req-step-1',
    tool_call_id: 'call_1', tool_name: 'execute_command', status: 'success', raw_inline: 'check',
  }, '2026-11-13T10:01:02Z');
  emitLive(13, 'stream_end', { task_id: 'task-new', source_inbound_seq: 10, llm_request_id: 'req-step-1' }, '2026-11-13T10:01:02Z');

  // Step 2 reuses tool_call_id="call_1" for request_restart and pauses for approval
  emitLive(14, 'tool_call_start', {
    task_id: 'task-new', source_inbound_seq: 10, llm_request_id: 'req-step-2',
    tool_call_id: 'call_1', tool_name: 'request_restart', arguments: { target: 'all', reason: 'deploy' },
  }, '2026-11-13T10:01:03Z');
  emitLive(15, 'approval_requested', {
    approval_id: 'ap-live-1', operation: 'restart', status: 'pending',
    tool_call_id: 'call_1', task_id: 'task-new', node_id: 'bootstrap.coder',
    details: { target: 'all', reason: 'deploy' },
  }, '2026-11-13T10:01:04Z');

  // Historical call_1 and step-1 call_1 stay 'success'; only step-2 request_restart becomes 'awaiting_approval'!
  assert.equal(live.toolExecutionsById[oldStableId].status, 'success');
  assert.equal(live.toolExecutionsById[oldStableId].approvalId, undefined);
  const newestStableId = live.toolExecutionOrder[live.toolExecutionOrder.length - 1];
  assert.equal(live.toolExecutionsById[newestStableId].name, 'request_restart');
  assert.equal(live.toolExecutionsById[newestStableId].status, 'awaiting_approval');
  assert.equal(live.toolExecutionsById[newestStableId].approvalId, 'ap-live-1');

  emitLive(16, 'approval_decided', {
    approval_id: 'ap-live-1', decision: 'allow', tool_call_id: 'call_1', task_id: 'task-new',
  }, '2026-11-13T10:01:05Z');
  assert.equal(live.toolExecutionsById[newestStableId].status, 'running');
  assert.equal(live.toolExecutionsById[newestStableId].approvalStatus, 'allowed');
  assert.equal(live.toolExecutionsById[oldStableId].status, 'success');

  // 2. Reconnect / page-refresh path: /history only has completed call_1 rows; pending approval must synthesize a new bottom card!
  let reconnected = hydrateStructuredHistory(createInitialChatState(), sessionId, conversationId, history, false);
  const reconnectedOldId = reconnected.toolExecutionOrder[0];
  reconnected = reduceChatEvent(reconnected, {
    seq: 99,
    event_id: 'synthetic-approval-ap-reconnect-1',
    ts: '2026-11-13T10:02:00Z',
    session_id: sessionId,
    type: 'approval_requested',
    payload: {
      approval_id: 'ap-reconnect-1',
      operation: 'restart',
      status: 'pending',
      tool_call_id: 'call_1',
      task_id: 'task-new',
      node_id: 'bootstrap.coder',
      details: { target: 'all', reason: 'deploy' },
      conversation_key: `web:${conversationId}`,
    },
  });
  assert.equal(reconnected.toolExecutionsById[reconnectedOldId].status, 'success');
  assert.equal(reconnected.toolExecutionsById[reconnectedOldId].approvalId, undefined);
  const synthStableId = reconnected.toolExecutionOrder[reconnected.toolExecutionOrder.length - 1];
  assert.notEqual(synthStableId, reconnectedOldId);
  assert.equal(reconnected.toolExecutionsById[synthStableId].name, 'request_restart');
  assert.equal(reconnected.toolExecutionsById[synthStableId].status, 'awaiting_approval');
  assert.equal(reconnected.toolExecutionsById[synthStableId].approvalId, 'ap-reconnect-1');
});

// ── phantom attachment prevention & completion-card attachment placement ──

check('history hydration scopes repeated call_2 results, ignores read_file/json-array/worker-arg phantom attachments, and moves generated images to the visible completion card', () => {
  const sessionId = 'sess-att';
  const conversationId = 'conv-att';
  const history: StructuredMessage[] = [
    // Turn 1: call_2 is a plain list_dir returning JSON array of dicts + spawn_worker with attachment_paths
    { id: 'u1', role: 'user', content: 'check files', source_task_id: 't1', created_at: '2026-11-13T11:00:00Z' },
    {
      id: 'a1-work',
      role: 'assistant',
      content: '',
      source_task_id: 't1',
      created_at: '2026-11-13T11:00:01Z',
      tool_calls: [
        { id: 'call_1', name: 'spawn_worker', arguments: { instruction: 'inspect', attachment_paths: ['docs/spec.md'] } },
        { id: 'call_2', name: 'list_dir', arguments: { paths: ['src'] } },
      ],
    },
    { id: 't1-1', role: 'tool', tool_call_id: 'call_1', tool_name: 'spawn_worker', content: 'spawned', created_at: '2026-11-13T11:00:02Z' },
    {
      id: 't1-2',
      role: 'tool',
      tool_call_id: 'call_2',
      tool_name: 'list_dir',
      content: '{"ok":true,"data":{"result":[{"name":"index.ts","type":"file"},{"path":"src/app.tsx","type":"file"}]}}',
      tool_result_structured: {
        ok: true,
        data: { result: [{ name: 'index.ts', type: 'file' }, { path: 'src/app.tsx', type: 'file' }] },
      },
      created_at: '2026-11-13T11:00:02Z',
    } as unknown as StructuredMessage,
    { id: 'a1-done', role: 'assistant', content: 'checked', source_task_id: 't1', created_at: '2026-11-13T11:00:03Z' },

    // Turn 2: call_2 is read_file reading an image + tool_result_attachment bridge, then call_2 in next step is generate_image!
    { id: 'u2', role: 'user', content: 'inspect and draw', source_task_id: 't2', created_at: '2026-11-13T11:01:00Z' },
    {
      id: 'a2-read',
      role: 'assistant',
      content: '',
      source_task_id: 't2',
      created_at: '2026-11-13T11:01:01Z',
      tool_calls: [{ id: 'call_2', name: 'read_file', arguments: { path: 'ui.png' } }],
    },
    {
      id: 't2-read',
      role: 'tool',
      tool_call_id: 'call_2',
      tool_name: 'read_file',
      content: '[Image]',
      tool_attachments: [{ type: 'image', path: 'data/attachments/read_file/abc123.png', name: 'ui.png' }],
      created_at: '2026-11-13T11:01:02Z',
    } as unknown as StructuredMessage,
    {
      id: 'u2-bridge',
      role: 'user',
      message_type: 'tool_result_attachment',
      content: '',
      attachments: [{ type: 'image', path: 'data/attachments/read_file/abc123.png', name: 'ui.png' }],
      created_at: '2026-11-13T11:01:02Z',
    },
    {
      id: 'a2-gen',
      role: 'assistant',
      content: '',
      source_task_id: 't2',
      created_at: '2026-11-13T11:01:03Z',
      tool_calls: [{ id: 'call_2', name: 'generate_image', arguments: { prompt: 'cat' } }],
    },
    {
      id: 't2-gen',
      role: 'tool',
      tool_call_id: 'call_2',
      tool_name: 'generate_image',
      content: '{"attachments":["data/attachments/cat.png"]}',
      tool_attachments: [{ type: 'image', path: 'data/attachments/cat.png', name: 'cat.png' }],
      created_at: '2026-11-13T11:01:04Z',
    } as unknown as StructuredMessage,
    { id: 'a2-final', role: 'assistant', content: 'here is your cat', source_task_id: 't2', created_at: '2026-11-13T11:01:05Z' },
  ];

  const hydrated = hydrateStructuredHistory(createInitialChatState(), sessionId, conversationId, history, false);
  const cards = (hydrated.messageOrderByConversation[conversationId] || []).map((id) => hydrated.messagesById[id]);

  // Turn 1 cards have ZERO phantom attachments (no call_2 leak from Turn 2, no list_dir dicts, no spawn_worker arg leak)
  const turn1Cards = cards.filter((c) => c.source.taskId === 't1');
  for (const card of turn1Cards) {
    assert.deepEqual(card.attachments || [], []);
  }

  // Turn 2 intermediate work cards (a2-read and a2-gen) have ZERO top-of-turn attachments;
  // read_file's internal png is dropped completely, and cat.png lands ONLY on a2-final!
  const turn2AssistantCards = cards.filter((c) => c.role === 'assistant' && c.source.taskId === 't2');
  assert.equal(turn2AssistantCards.length, 3);
  assert.deepEqual(turn2AssistantCards[0].attachments || [], []);
  assert.deepEqual(turn2AssistantCards[1].attachments || [], []);
  assert.equal(turn2AssistantCards[2].completionType, 'finish');
  assert.deepEqual((turn2AssistantCards[2].attachments || []).map((a) => a.path), ['data/attachments/cat.png']);
});

check('refreshing while a tail tool call is awaiting approval keeps the hydrated card active and attaches approval in place without duplicate cards', () => {
  const sessionId = 'sess-tail-approval';
  const conversationId = 'conv-tail-approval';
  const historyWithInFlightTail: StructuredMessage[] = [
    { id: 'u1', role: 'user', content: 'deploy', source_task_id: 'task-48ad', source_inbound_seq: 100, created_at: '2026-11-13T16:28:00Z' },
    {
      id: 'a-reply',
      role: 'assistant',
      content: '',
      source_task_id: 'task-48ad',
      source_inbound_seq: 100,
      llm_request_id: 'req-1',
      created_at: '2026-11-13T16:39:32Z',
      tool_calls: [{ id: 'call_1', name: 'intermediate_reply', arguments: { text: 'preparing restart' } }],
    },
    { id: 't-reply', role: 'tool', tool_call_id: 'call_1', tool_name: 'intermediate_reply', content: 'ok', created_at: '2026-11-13T16:39:32Z' },
    {
      id: 'a-restart',
      role: 'assistant',
      content: '',
      source_task_id: 'task-48ad',
      source_inbound_seq: 100,
      llm_request_id: 'req-2',
      source_node_id: 'bootstrap.coder',
      created_at: '2026-11-13T16:42:30Z',
      tool_calls: [{ id: 'call_1', name: 'request_restart', arguments: { target: 'all', reason: 'deploy' } }],
    },
    // No role='tool' result row for a-restart yet (blocked on user approval)
  ];

  let state = hydrateStructuredHistory(createInitialChatState(), sessionId, conversationId, historyWithInFlightTail, false);
  const orderBefore = state.messageOrderByConversation[conversationId] || [];
  assert.equal(orderBefore.length, 3);
  const tailMsgBefore = state.messagesById[orderBefore[2]];
  assert.equal(tailMsgBefore.status, 'running_tools');
  const tailToolId = state.toolExecutionOrder[state.toolExecutionOrder.length - 1];
  assert.equal(state.toolExecutionsById[tailToolId].name, 'request_restart');
  assert.equal(state.toolExecutionsById[tailToolId].status, 'running');
  assert.equal(state.toolExecutionsById[tailToolId].taskId, 'task-48ad');

  // Pending approval replay attaches directly to the hydrated a-restart card (no duplicate card!)
  state = reduceChatEvent(state, {
    seq: 500,
    event_id: 'synthetic-approval-tail-1',
    session_id: sessionId,
    type: 'approval_requested',
    ts: '2026-11-13T16:42:30Z',
    payload: {
      approval_id: 'ap-tail-1',
      operation: 'restart',
      status: 'pending',
      tool_call_id: 'call_1',
      task_id: 'task-48ad',
      node_id: 'bootstrap.coder',
      details: { target: 'all', reason: 'deploy' },
      conversation_key: `web:${conversationId}`,
    },
  });

  const orderAfterReplay = state.messageOrderByConversation[conversationId] || [];
  assert.equal(orderAfterReplay.length, 3);
  assert.equal(state.messagesById[orderAfterReplay[2]].status, 'awaiting_approval');
  assert.equal(state.toolExecutionsById[tailToolId].status, 'awaiting_approval');
  assert.equal(state.toolExecutionsById[tailToolId].approvalId, 'ap-tail-1');

  // Subsequent live approval_decided + tool_call_end also update the same card in place
  state = reduceChatEvent(state, {
    seq: 501,
    event_id: 'live-ap-decided-1',
    session_id: sessionId,
    type: 'approval_decided',
    ts: '2026-11-13T16:42:35Z',
    payload: {
      approval_id: 'ap-tail-1',
      decision: 'deny',
      tool_call_id: 'call_1',
      task_id: 'task-48ad',
      conversation_key: `web:${conversationId}`,
    },
  });
  state = reduceChatEvent(state, {
    seq: 502,
    event_id: 'live-tool-end-1',
    session_id: sessionId,
    type: 'tool_call_end',
    ts: '2026-11-13T16:42:36Z',
    payload: {
      task_id: 'task-48ad',
      source_inbound_seq: 100,
      llm_request_id: 'req-2',
      tool_call_id: 'call_1',
      tool_name: 'request_restart',
      status: 'error',
      raw_inline: 'ERROR: user denied approval',
      conversation_key: `web:${conversationId}`,
    },
  });

  assert.equal((state.messageOrderByConversation[conversationId] || []).length, 3);
  assert.equal(state.toolExecutionsById[tailToolId].status, 'error');
  assert.equal(state.toolExecutionsById[tailToolId].approvalStatus, 'denied');
});

// ── runner ────────────────────────────────────────────────────────────────

let failed = 0;
for (const [name, run] of checks) {
  try {
    await run();
    console.log(`ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${error instanceof Error ? error.message : String(error)}`);
  }
}
console.log(failed === 0 ? `ALL ${checks.length} CHECKS PASSED` : `${failed} of ${checks.length} CHECKS FAILED`);
if (failed > 0) process.exitCode = 1;
