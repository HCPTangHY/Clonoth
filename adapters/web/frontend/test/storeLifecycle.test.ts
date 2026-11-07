// [2026-10-02] 真实 store 的登出/重登生命周期检查（Web 登出并发修复的回归用例）。
// Why: 前一版只有纯函数级的离线断言，没有覆盖「resetState 正在被调用时还有请求在飞」
// 这一条真实链路——它是登出后旧会话重新出现在侧边栏、事件泵重启、子会话历史回填的根因。
// How: 装一套假的浏览器环境（WebSocket / localStorage / window / fetch / setInterval），
// 然后 await import 真实的 zustand store（chatStore），用假后端驱动 loadStartup /
// viewChildSession / resetState 的真实流程。Purpose: 不联网、不用真浏览器、不碰真实 auth，
// 也能验证「登出一次 = 零 socket 零轮询」「重登一次 = 一条 socket + 一个轮询」以及
// reset 之前发出的 startup/parent/child 响应被世代守卫丢弃。
//
// Run（需要 node_modules；复用 Vite 自带的 esbuild，不引入任何依赖）：
//   node_modules/.bin/esbuild test/storeLifecycle.test.ts --bundle --platform=node \
//     --format=esm --outfile=/tmp/clonoth-store-lifecycle.mjs \
//   && node /tmp/clonoth-store-lifecycle.mjs
import assert from 'node:assert/strict';

// ── 假浏览器环境（必须在导入 store 之前装好） ───────────────────────────────

function defineGlobal(name: string, value: unknown): void {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  /** 仍处于 CONNECTING/OPEN 的 socket，等价于真实浏览器里“还活着”的连接。 */
  static get live(): FakeWebSocket[] {
    return FakeWebSocket.instances.filter((socket) => socket.readyState !== 3);
  }

  url: string;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
  }
}

const storage = new Map<string, string>();
const fakeLocalStorage = {
  getItem: (key: string) => (storage.has(key) ? storage.get(key)! : null),
  setItem: (key: string, value: string) => { storage.set(key, String(value)); },
  removeItem: (key: string) => { storage.delete(key); },
  clear: () => { storage.clear(); },
  key: (index: number) => [...storage.keys()][index] ?? null,
  get length() { return storage.size; },
};

const fakeWindow: Record<string, any> = {
  location: { protocol: 'http:', host: 'localhost:5173', origin: 'http://localhost:5173', hash: '' },
  history: {
    replaceState(_state: unknown, _title: string, url: string) {
      fakeWindow.location.hash = url.startsWith('#') ? url : '';
    },
  },
  addEventListener: () => {},
  removeEventListener: () => {},
  setTimeout: (handler: () => void, delay?: number) => setTimeout(handler, delay),
  clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
};

// 假定时器：真 setInterval 会让脚本挂 30 秒，这里只登记句柄，用来断言“恰有一个轮询”。
const intervals = new Map<number, () => void>();
let nextIntervalId = 1;
defineGlobal('setInterval', (handler: () => void) => {
  const id = nextIntervalId++;
  intervals.set(id, handler);
  return id;
});
defineGlobal('clearInterval', (id: number) => { intervals.delete(id); });

type StubResponse = {
  ok: boolean;
  status: number;
  headers: { get: (name: string) => string | null };
  json: () => Promise<unknown>;
  text: () => Promise<string>;
  blob: () => Promise<unknown>;
};

function json(body: unknown, status = 200): StubResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => body,
  };
}

type FetchHandler = (url: string, init?: RequestInit) => StubResponse | Promise<StubResponse>;

const sessionRow = (sessionId: string) => ({
  session_id: sessionId,
  conversation_key: `web:${sessionId}`,
  channel: 'web',
  created_at: '2026-10-02T00:00:00Z',
  updated_at: '2026-10-02T00:00:00Z',
});

// 默认后端：所有读接口都返回“空但合法”的响应，单个用例只覆盖自己关心的那一条。
const defaultHandler: FetchHandler = (url) => {
  if (url.startsWith('/v1/sessions?')) return json([]);
  if (url.includes('/history')) return json({ messages: [], total: 0, has_more: false });
  if (url.includes('/context_window')) {
    return json({
      session_id: 's', prompt_tokens: 0, effective_tokens: 1, compact_threshold: 100,
      utilization: 0.01, source: 'test', message_count: 0,
    });
  }
  if (url.includes('/children')) return json([]);
  if (url.includes('/pending_approvals')) return json([]);
  if (url.includes('/running_tasks')) return json({ tasks: [] });
  return json({});
};

const requests: string[] = [];
let handler: FetchHandler = defaultHandler;

defineGlobal('fetch', (input: unknown, init?: RequestInit) => {
  const url = typeof input === 'string'
    ? input
    : input instanceof URL
      ? input.href
      : String((input as { url?: string })?.url ?? '');
  requests.push(url);
  return Promise.resolve().then(() => handler(url, init));
});

defineGlobal('window', fakeWindow);
defineGlobal('localStorage', fakeLocalStorage);
defineGlobal('WebSocket', FakeWebSocket);

// ── 真实 store（在假环境装好之后才求值） ────────────────────────────────────

// pluginsStore 与 pluginRuntime 互相引用（pluginsStore 在模块顶层订阅 plugin 事件）。
// esbuild 的单文件 bundle 里循环依赖的初始化顺序由“第一个被访问的模块”决定：从
// chatStore 进入会让 pluginsStore 在 pluginRuntime 的事件总线建立之前订阅而抛错。
// 浏览器里也是 pluginsStore 先完成（它持有订阅），这里显式复现同样的顺序，
// 只影响测试的加载顺序，不改变任何生产代码语义。
await import('../src/store/pluginsStore');
const { useChatStore } = await import('../src/store/chatStore');

// ── helpers ────────────────────────────────────────────────────────────────

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, resolve: release };
}

/** 让出若干轮事件循环：真实的 loadStartup 会串起多个 await 的 HTTP 往返。 */
async function flush(rounds = 16): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
}

const sessionScans = () => requests.filter((url) => url.startsWith('/v1/sessions?')).length;
const liveIntervals = () => intervals.size;
const resetEnvironment = (): void => {
  useChatStore.getState().resetState();
  FakeWebSocket.instances.length = 0;
  intervals.clear();
  requests.length = 0;
  storage.clear();
  handler = defaultHandler;
  fakeWindow.location.hash = '';
};

const checks: Array<[string, () => void | Promise<void>]> = [];
const check = (name: string, run: () => void | Promise<void>) => checks.push([name, run]);

// ── cases ──────────────────────────────────────────────────────────────────

check('logout then relogin leaves zero sockets, then exactly one socket and one poll timer', async () => {
  resetEnvironment();
  handler = (url) => (url.startsWith('/v1/sessions?') ? json([sessionRow('sess-1')]) : defaultHandler(url));

  // 首次登录（MainApp 挂载 → loadStartup）
  useChatStore.getState().loadStartup();
  await flush();
  let state = useChatStore.getState();
  assert.deepEqual(state.conversations.map((conversation) => conversation.id), ['sess-1']);
  assert.equal(state.activeConversationId, 'sess-1');
  assert.equal(FakeWebSocket.live.length, 1);
  assert.equal(liveIntervals(), 1);

  // 登出（GeneralSettingsPage → resetState）
  useChatStore.getState().resetState();
  state = useChatStore.getState();
  assert.deepEqual(state.conversations, []);
  assert.deepEqual(state.childSessionMessages, {});
  assert.equal(state.activeConversationId, null);
  assert.equal(FakeWebSocket.live.length, 0);
  assert.equal(liveIntervals(), 0);

  // 重新登录：重新拉列表，并只建立一条连接 + 一个轮询
  requests.length = 0;
  useChatStore.getState().loadStartup();
  await flush();
  state = useChatStore.getState();
  assert.deepEqual(state.conversations.map((conversation) => conversation.id), ['sess-1']);
  assert.equal(sessionScans(), 1);
  assert.equal(FakeWebSocket.live.length, 1);
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.equal(liveIntervals(), 1);
});

check('repeated loadStartup is idempotent (one scan, one socket, one poll timer)', async () => {
  resetEnvironment();
  handler = (url) => (url.startsWith('/v1/sessions?') ? json([sessionRow('sess-1')]) : defaultHandler(url));

  useChatStore.getState().loadStartup();
  useChatStore.getState().loadStartup();
  useChatStore.getState().loadStartup();
  await flush();

  assert.equal(sessionScans(), 1);
  assert.equal(FakeWebSocket.instances.length, 1);
  assert.equal(liveIntervals(), 1);
  assert.deepEqual(useChatStore.getState().conversations.map((conversation) => conversation.id), ['sess-1']);
});

check('a startup response that lands after logout cannot write sessions or restart the pump', async () => {
  resetEnvironment();
  const gate = deferred();
  handler = async (url) => {
    if (url.startsWith('/v1/sessions?')) {
      await gate.promise;
      return json([sessionRow('stale-session')]);
    }
    return defaultHandler(url);
  };

  useChatStore.getState().loadStartup();
  await flush();
  assert.equal(sessionScans(), 1);
  // 列表还没回来：事件泵尚未启动。
  assert.equal(FakeWebSocket.instances.length, 0);

  useChatStore.getState().resetState();
  assert.equal(FakeWebSocket.live.length, 0);
  assert.equal(liveIntervals(), 0);

  gate.resolve();
  await flush();

  const state = useChatStore.getState();
  assert.deepEqual(state.conversations, []);
  assert.equal(state.activeConversationId, null);
  assert.equal(FakeWebSocket.instances.length, 0);
  assert.equal(liveIntervals(), 0);
});

check('parent history that lands after logout cannot refill the cleared store', async () => {
  resetEnvironment();
  const gate = deferred();
  let historyRequests = 0;
  handler = async (url) => {
    if (url.startsWith('/v1/sessions?')) return json([sessionRow('sess-1')]);
    if (url.includes('/sessions/sess-1/history')) {
      historyRequests += 1;
      await gate.promise;
      return json({
        messages: [{
          id: 'm1', role: 'assistant', content: '上一个账号的消息', created_at: '2026-10-02T00:00:00Z',
        }],
        total: 1,
        has_more: false,
      });
    }
    return defaultHandler(url);
  };

  useChatStore.getState().loadStartup();
  await flush();
  // 列表已落库、事件泵已建立，历史第一页还在飞。
  assert.equal(FakeWebSocket.live.length, 1);
  assert.equal(historyRequests, 1);

  useChatStore.getState().resetState();
  gate.resolve();
  await flush();

  const state = useChatStore.getState();
  assert.deepEqual(state.conversations, []);
  assert.deepEqual(state.messagesById, {});
  assert.deepEqual(state.messageOrderByConversation, {});
  assert.equal(state.activeConversationId, null);
});

check('context usage and running tasks arriving after logout cannot repopulate state', async () => {
  resetEnvironment();
  const { loadContextUsageIntoStore, restoreGeneratingStateFromBackend } = await import('../src/store/sessionRecovery');
  const gate = deferred();
  handler = async (url) => {
    if (url.includes('/context_window')) {
      await gate.promise;
      return defaultHandler(url);
    }
    if (url.includes('/running_tasks')) {
      await gate.promise;
      return json({ tasks: [{ task_id: 'old-task', is_user_entry: true }] });
    }
    return defaultHandler(url);
  };
  const contextLoad = loadContextUsageIntoStore('old-session', useChatStore.setState, useChatStore.getState);
  const taskLoad = restoreGeneratingStateFromBackend('old-conv', 'old-session', useChatStore.setState, useChatStore.getState);
  await flush();
  useChatStore.getState().resetState();
  gate.resolve();
  await Promise.all([contextLoad, taskLoad]);
  assert.deepEqual(useChatStore.getState().contextUsageBySession, {});
  assert.deepEqual(useChatStore.getState().generatingBySession, {});
  assert.deepEqual(useChatStore.getState().activeTaskBySession, {});
});

check('child history that lands after logout cannot refill childSessionMessages', async () => {
  resetEnvironment();
  const gate = deferred();
  let childHistoryRequests = 0;
  handler = async (url) => {
    if (url.includes('/sessions/child-stale/history')) {
      childHistoryRequests += 1;
      await gate.promise;
      return json({
        messages: [{
          id: 'c1', role: 'assistant', content: '子会话旧输出', created_at: '2026-10-02T00:00:00Z',
        }],
        total: 1,
        has_more: false,
      });
    }
    return defaultHandler(url);
  };

  useChatStore.getState().viewChildSession('child-stale');
  await flush();
  assert.equal(childHistoryRequests, 1);
  // 进入子会话时先落空数组（加载中），此时历史还在飞。
  assert.deepEqual(useChatStore.getState().childSessionMessages['child-stale'], []);

  useChatStore.getState().resetState();
  gate.resolve();
  await flush();

  const state = useChatStore.getState();
  assert.deepEqual(state.childSessionMessages, {});
  assert.deepEqual(state.childSessionEmpty, {});
  assert.deepEqual(state.childSessionLoadFailed, {});
  assert.equal(state.viewingChildSessionId, null);
});

check('a startup that reached no sessions still lets the next login establish one pump', async () => {
  resetEnvironment();
  // 后端不可达：listSessions 把失败吞成空页，loadStartup 仍要建立事件流。
  handler = () => { throw new Error('network down'); };
  useChatStore.getState().loadStartup();
  await flush();
  assert.deepEqual(useChatStore.getState().conversations, []);
  assert.equal(FakeWebSocket.live.length, 1);
  assert.equal(liveIntervals(), 1);

  useChatStore.getState().resetState();
  assert.equal(FakeWebSocket.live.length, 0);
  assert.equal(liveIntervals(), 0);

  // 重试：后端恢复后重新登录，列表与连接都回到正常状态。
  handler = (url) => (url.startsWith('/v1/sessions?') ? json([sessionRow('sess-2')]) : defaultHandler(url));
  useChatStore.getState().loadStartup();
  await flush();
  assert.deepEqual(useChatStore.getState().conversations.map((conversation) => conversation.id), ['sess-2']);
  assert.equal(FakeWebSocket.live.length, 1);
  assert.equal(liveIntervals(), 1);
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
