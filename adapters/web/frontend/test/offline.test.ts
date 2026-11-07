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
import { createInitialChatState } from '../src/store/eventReducer';
import { loadChildSessionHistoryIntoStore } from '../src/store/historyLoader';
import { mergeSource, normalizeResponseInfo } from '../src/store/eventReducerState';
import { normalizeHistoryThinkingSegments } from '../src/store/historyHydration';
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
