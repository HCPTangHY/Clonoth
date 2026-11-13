// Offline compact Goal header checks. Run: node --experimental-vm-modules tests/test_goal_header.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sourcePath = path.resolve(__dirname, '../plugins/ide/web/goal_header.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function element(tag = 'div') {
  return {
    tag, children: [], attrs: {}, events: new Map(), dataset: {}, hidden: false, removed: false,
    textContent: '',
    set innerHTML(_) { throw new Error('Header must never insert Goal text as HTML'); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(name, callback) { this.events.set(name, callback); },
    removeEventListener(name, callback) { if (this.events.get(name) === callback) this.events.delete(name); },
    append(...children) { this.children.push(...children); },
    remove() { this.removed = true; },
  };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function drain() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
function record(sid = 'main', extra = {}) {
  return { schema_version: 1, session_id: sid, revision: 1, goal: 'A goal', status: 'running', is_child: false, ...extra };
}
async function harness(sid = 'main', data = {}) {
  const document = element('document');
  document.createElement = element;
  document.hidden = false;
  const requests = [], calls = [], intervals = new Map(), timeouts = new Map(), subscribers = new Map();
  let sequence = 0;
  const window = {
    innerWidth: 390,
    setInterval(callback, ms) { const id = ++sequence; intervals.set(id, { callback, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, ms) { const id = ++sequence; timeouts.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
  };
  const context = vm.createContext({ document, window, AbortController });
  const mod = new vm.SourceTextModule(source, { context, identifier: sourcePath });
  await mod.link(() => { throw new Error('Header module must be self-contained'); });
  await mod.evaluate();
  const plugin = Object.assign({}, mod.namespace.default);
  const ctx = {
    el: element(), data: { sessionId: sid, ...data },
    events: { on(type, callback) { subscribers.set(type, callback); return () => subscribers.delete(type); } },
    api: {
      request(url, options) { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; },
      async call(...args) { calls.push(args); },
    },
  };
  plugin.mount(ctx);
  return {
    plugin, ctx, requests, calls, intervals, timeouts, subscribers, document, window,
    async resolve(index, body) { requests[index].resolve(body); await drain(); },
    async reject(index, error) { requests[index].reject(error); await drain(); },
    update(sessionId, extra = {}) { ctx.data = { sessionId, ...extra }; plugin.update(ctx); },
    click() { plugin.button.events.get('click')(); },
  };
}
let passed = 0;
async function check(name, test) { await test(); ++passed; fs.writeSync(1, `PASS ${name}\n`); }

(async () => {
  await check('missing sessions do not load; actual child identity comes from backend', async () => {
    for (const [sid, extra] of [['', {}], ['no-session', {}]]) {
      const h = await harness(sid, extra);
      try { assert.equal(h.requests.length, 0); assert.equal(h.plugin.button.hidden, true); }
      finally { h.plugin.destroy(); }
    }
    const child = await harness('child', { isChildSession: true });
    try {
      assert.equal(child.requests.length, 1);
      assert.equal(child.plugin.button.disabled, true);
      await child.resolve(0, record('child', { is_child: true }));
      assert.equal(child.plugin.button.hidden, true);
    } finally { child.plugin.destroy(); }
    const h = await harness();
    try {
      await h.resolve(0, record('main', { goal: '', status: 'empty' }));
      assert.equal(h.plugin.button.hidden, true);
    } finally { h.plugin.destroy(); }
  });
  await check('compact entry shows status rather than the full objective', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', { goal: '<img onerror=evil()>'.repeat(100) }));
      assert.equal(h.plugin.button.hidden, false);
      assert.equal(h.plugin.button.textContent, 'Goal · 执行中');
      assert.equal(h.plugin.button.dataset.status, 'running');
      assert.equal(h.plugin.button.attrs['aria-label'], '打开 Goal 详情');
      assert.equal(h.plugin.button.textContent.includes('<img'), false);
      assert.equal(h.ctx.el.children.length, 2, 'only style and entry are mounted');
    } finally { h.plugin.destroy(); }
  });
  await check('entry opens the existing right panel without a Goal mutation', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.click();
      await drain();
      assert.equal(h.calls.length, 1);
      assert.equal(h.calls[0][0], 'openPanel');
      assert.equal(h.calls[0][1], 'plugin:ide:goal');
      assert.equal(h.requests.length, 1);
      assert.equal(h.requests[0].options.method, undefined);
    } finally { h.plugin.destroy(); }
  });
  await check('read failures retain an entry for recovery', async () => {
    const h = await harness();
    try {
      await h.reject(0, new Error('offline'));
      assert.equal(h.plugin.button.hidden, false);
      assert.equal(h.plugin.button.textContent, 'Goal · 读取失败');
      h.click();
      assert.equal(h.calls[0][1], 'plugin:ide:goal');
      h.plugin.load();
      await h.resolve(1, record('main', { status: 'completed' }));
      assert.equal(h.plugin.button.textContent, 'Goal · 已完成');
    } finally { h.plugin.destroy(); }
  });
  await check('events refresh only the current session and coalesce during a request', async () => {
    const h = await harness();
    try {
      const notify = h.subscribers.get('ide_goal_updated');
      notify({ session_id: 'another' });
      assert.equal(h.requests.length, 1);
      notify({ session_id: 'main' });
      notify({ session_id: 'main' });
      assert.equal(h.requests.length, 1);
      await h.resolve(0, record());
      assert.equal(h.requests.length, 2);
      await h.resolve(1, record('main', { status: 'reviewing' }));
      assert.equal(h.plugin.button.textContent, 'Goal · 评审中');
      notify({ session_id: 'main' });
      assert.equal(h.requests.length, 3);
      await h.resolve(2, record('main', { status: 'paused' }));
      assert.equal(h.plugin.button.textContent, 'Goal · 已暂停');
    } finally { h.plugin.destroy(); }
  });
  await check('late requests cannot change a different session or child view', async () => {
    const h = await harness('one');
    try {
      h.update('two');
      assert.equal(h.requests[0].options.signal.aborted, true);
      await h.resolve(1, record('two', { status: 'completed' }));
      await h.resolve(0, record('one', { status: 'blocked' }));
      assert.equal(h.plugin.button.textContent, 'Goal · 已完成');
      h.plugin.load();
      h.update('child', { isChildSession: true });
      assert.equal(h.requests[2].options.signal.aborted, true);
      await h.resolve(2, record('two'));
      assert.equal(h.requests.length, 4);
      await h.resolve(3, record('child', { is_child: true }));
      assert.equal(h.plugin.button.hidden, true);
    } finally { h.plugin.destroy(); }
  });
  await check('mismatched or unknown response states are not presented as valid', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('foreign'));
      assert.equal(h.plugin.button.textContent, 'Goal · 读取失败');
      h.plugin.load();
      await h.resolve(1, record('main', { status: 'not-a-state' }));
      assert.equal(h.plugin.button.dataset.status, 'blocked');
      assert.equal(h.plugin.button.textContent, 'Goal · 读取失败');
      h.plugin.load();
      await h.resolve(2, record('main', { is_child: true }));
      assert.equal(h.plugin.button.hidden, true);
    } finally { h.plugin.destroy(); }
  });
  await check('bounded polling pauses while the document is hidden', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      const timer = [...h.intervals.values()][0];
      assert.equal(timer.ms, 4000);
      h.document.hidden = true;
      timer.callback();
      assert.equal(h.requests.length, 1);
      h.document.hidden = false;
      h.document.events.get('visibilitychange')();
      timer.callback();
      assert.equal(h.requests.length, 2);
      await h.resolve(1, record());
      assert.equal(h.timeouts.size, 0);
    } finally { h.plugin.destroy(); }
  });
  await check('timeout aborts the read without creating a write', async () => {
    const h = await harness();
    try {
      const timer = [...h.timeouts.values()][0];
      assert.equal(timer.ms, 8000);
      timer.callback();
      assert.equal(h.requests[0].options.signal.aborted, true);
      await h.reject(0, new Error('aborted'));
      assert.equal(h.plugin.button.textContent, 'Goal · 读取失败');
      assert.equal(h.timeouts.size, 0);
      assert.equal(h.requests.every(r => !r.options.method || r.options.method === 'GET'), true);
    } finally { h.plugin.destroy(); }
  });
  await check('destroy aborts pending work and detaches subscriptions', async () => {
    const h = await harness();
    h.plugin.destroy();
    assert.equal(h.requests[0].options.signal.aborted, true);
    assert.equal(h.plugin.button.removed, true);
    assert.equal(h.plugin.style.removed, true);
    assert.equal(h.subscribers.size, 0);
    assert.equal(h.document.events.size, 0);
    assert.equal(h.intervals.size, 0);
    await h.resolve(0, record());
    assert.equal(h.plugin.button.removed, true);
    assert.equal(h.timeouts.size, 0);
    await h.plugin.load();
    assert.equal(h.requests.length, 1);
  });
  await check('Goal no longer changes host rail layout or shared panel close behavior', async () => {
    const read = name => fs.readFileSync(path.resolve(__dirname, '../adapters/web/frontend/src', name), 'utf8');
    assert.equal(read('components/layout/AppLayout.tsx').includes('rightHeader'), false);
    assert.equal(read('views/viewRegistry.tsx').includes('right_panel_actions'), false);
    const panel = read('components/plugins/PluginPanel.tsx');
    assert.equal(panel.includes('setRightPanelOpen(false)'), false);
    assert.equal(panel.includes('onClick={onClose}'), true);
    const input = read('components/chat/ChatInput.tsx');
    assert.equal(input.match(/slot="input_status"/g).length, 1);
    assert.ok(input.indexOf('slot="input_status"') < input.indexOf('<AttachmentList'));
    assert.equal(source.includes('ide-goal-nav'), false);
  });
  await check('temporary primary sessions and verified aliases retain the raw view identity', async () => {
    const h=await harness('branch',{isChildSession:true});
    try {
      assert.equal(h.plugin.button.disabled,true);
      await h.resolve(0,record('owner',{requested_session_id:'branch'}));
      assert.equal(h.plugin.button.hidden,false);assert.equal(h.plugin.button.disabled,false);
      assert.equal(h.plugin.sessionId,'branch');assert.equal(h.plugin.ownerSessionId,'owner');
      assert.match(h.plugin.button.title,/owner/);h.click();
      const notify=h.subscribers.get('ide_goal_updated');notify({session_id:'owner'});
      assert.equal(h.requests.length,2);assert.ok(h.requests[1].url.endsWith('session_id=branch'));
      await h.resolve(1,record('owner',{requested_session_id:'branch',status:'completed'}));
    } finally {h.plugin.destroy();}
  });
  await check('an owner alias without a matching requested-session echo is not accepted', async () => {
    const h=await harness('branch');
    try {
      await h.resolve(0,record('owner',{requested_session_id:'another'}));
      assert.equal(h.plugin.ownerSessionId,'');assert.equal(h.plugin.button.textContent,'Goal · 读取失败');
    } finally {h.plugin.destroy();}
  });
  fs.writeSync(1, JSON.stringify({ tests: passed, result: 'passed' }) + '\n');
  process.exit(0);
})().catch(error => { fs.writeSync(2, String(error.stack || error) + '\n'); process.exit(1); });
