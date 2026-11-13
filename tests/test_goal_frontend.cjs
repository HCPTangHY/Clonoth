// Offline Goal detail-panel checks. Run: node --experimental-vm-modules tests/test_goal_frontend.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const sourcePath = path.resolve(__dirname, '../plugins/ide/web/goal_progress.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function element(tag = 'div') {
  let text = '';
  return {
    tag, children: [], parent: null, attrs: {}, events: new Map(), dataset: {}, style: {},
    hidden: false, disabled: false, focused: false, removed: false, value: '', scrollTop: 0,
    set textContent(value) { text = String(value); this.replaceChildren(); },
    get textContent() { return text + this.children.map(child => child.textContent).join(''); },
    set innerHTML(_) { throw new Error('Untrusted Goal text must never become HTML'); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(name, callback) { this.events.set(name, callback); },
    removeEventListener(name, callback) { if (this.events.get(name) === callback) this.events.delete(name); },
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } },
    replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); },
    focus() { this.focused = true; },
    remove() { this.removed = true; if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; },
  };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function drain() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
function record(sid = 'main', revision = 1, extra = {}) {
  return { schema_version: 1, session_id: sid, revision, goal: 'Complete the change', criteria: 'Tests pass',
    goal_version: 1, status: 'paused', rounds: 0, max_rounds: 8, stagnation: 0, reviews: [], pause_reason: '', is_child: false, ...extra };
}
async function harness(sid = 'main', data = {}, crypto) {
  const document = element('document');
  document.createElement = element;
  document.hidden = false;
  const requests = [], intervals = new Map(), timeouts = new Map(), subscribers = new Map();
  const controls = { closes: 0, accepted: true, edits: [], confirm: true };
  let nextId = 1;
  const window = {
    confirm() { return controls.confirm; },
    setInterval(callback, ms) { const id = nextId++; intervals.set(id, { callback, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, ms) { const id = nextId++; timeouts.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
  };
  const context = vm.createContext({ document, window, AbortController, ...(crypto ? { crypto } : {}) });
  const mod = new vm.SourceTextModule(source, { context, identifier: sourcePath });
  await mod.link(() => { throw new Error('Goal slot must be self-contained'); });
  await mod.evaluate();
  const plugin = Object.assign({}, mod.namespace.default);
  const ctx = {
    el: element(), state: {}, data: { sessionId: sid, ...data },
    events: { on(type, callback) { subscribers.set(type, callback); return () => subscribers.delete(type); } },
    close() { controls.closes++; },
    composer: { async setMode(...args) { controls.edits.push(args); return controls.accepted; } },
    api: { request(url, options) { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; } },
  };
  plugin.mount(ctx);
  return {
    plugin, ctx, requests, document, intervals, timeouts, subscribers, controls,
    async resolve(index, body) { requests[index].resolve(body); await drain(); },
    async reject(index, error) { requests[index].reject(error); await drain(); },
    update(sid, data = {}) { ctx.data = { sessionId: sid, ...data }; plugin.update(ctx); },
    input(field, value) { plugin[field].value = value; plugin[field].events.get('input')(); },
    click(button) { assert.equal(plugin[button].disabled, false, `${button} enabled`); return plugin[button].events.get('click')(); },
  };
}
let passed = 0;
async function check(name, test) { await test(); passed++; fs.writeSync(1, `PASS ${name}\n`); }

(async () => {
  await check('missing sessions do not load; actual children remain non-manageable after identity read', async () => {
    const missing = await harness('');
    try { assert.equal(missing.requests.length, 0); assert.equal(missing.plugin.root.hidden, true); }
    finally { missing.plugin.destroy(); }
    const h = await harness('child', { isChildSession: true });
    try {
      assert.equal(h.requests.length, 1);
      assert.equal(h.plugin.edit.disabled, true);
      await h.resolve(0, record('child', 1, { is_child: true }));
      assert.equal(h.plugin.body.hidden, true);
      assert.ok(h.plugin.ownerNotice.textContent.includes('真实子会话'));
      await h.plugin.mutate('resume');
      assert.equal(h.requests.length, 1);
    } finally { h.plugin.destroy(); }
  });
  await check('HTTP deployments without crypto can mount and save safely', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.input('criteria', 'Updated target');
      h.click('save');
      const body = JSON.parse(h.requests[1].options.body);
      assert.equal(body.action, 'set');
      assert.equal(body.expected_session_id, 'main');
      assert.equal(body.expected_revision, 1);
      assert.equal(body.goal, 'Complete the change');
      assert.equal(body.criteria, 'Updated target');
      assert.ok(body.request_id.startsWith('goal-'));
      assert.equal(h.plugin.save.disabled, true);
      await h.resolve(1, record('main', 2, { criteria: 'Updated target', goal_version: 2 }));
      assert.equal(h.plugin.dirty, false);
      assert.equal(h.plugin.resume.disabled, false);
      assert.equal(h.plugin.controls.children.includes(h.plugin.resume), true);
      assert.equal(h.plugin.data.status, 'paused');
      assert.equal(h.requests.length, 2, 'saving does not implicitly start');
    } finally { h.plugin.destroy(); }
  });
  await check('workspace changes retain panel settings and unsaved draft', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.plugin.settings.open = true;
      h.input('criteria', 'Unsaved');
      h.update('main', { workspace: { name: 'elsewhere', path: '/other' } });
      assert.equal(h.requests.length, 1);
      assert.equal(h.plugin.settings.open, true);
      assert.equal(h.plugin.body.hidden, false);
      assert.equal(h.plugin.criteria.value, 'Unsaved');
      assert.equal(h.plugin.data.session_id, 'main');
    } finally { h.plugin.destroy(); }
  });
  await check('late GET and POST responses cannot cross sessions', async () => {
    const h = await harness('one');
    try {
      h.update('two');
      assert.equal(h.requests[0].options.signal.aborted, true);
      await h.resolve(1, record('two'));
      await h.resolve(0, record('one', 99));
      assert.equal(h.plugin.data.session_id, 'two');
      h.input('criteria', 'Session two draft');
      h.click('save');
      h.update('three');
      await h.resolve(3, record('three'));
      await h.resolve(2, record('two', 2));
      assert.equal(h.plugin.data.session_id, 'three');
      assert.equal(h.plugin.criteria.value, 'Tests pass');
      assert.equal(h.plugin.mutating, false);
    } finally { h.plugin.destroy(); }
  });
  await check('failed reads show retry rather than an empty goal', async () => {
    const h = await harness();
    try {
      await h.reject(0, new Error('offline'));
      assert.equal(h.plugin.root.hidden, false);
      assert.equal(h.plugin.data, null);
      assert.ok(h.plugin.notice.textContent.includes('读取失败'));
      assert.equal(h.plugin.save.disabled, true);
      h.click('reload');
      await h.resolve(1, record());
      assert.equal(h.plugin.error, '');
      h.plugin.load();
      await h.reject(2, new Error('network'));
      assert.equal(h.plugin.data.goal, 'Complete the change');
      assert.equal(h.plugin.resume.disabled, true);
    } finally { h.plugin.destroy(); }
  });
  await check('malformed, mismatched and regressing responses are rejected', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 5));
      for (const body of [record('foreign', 6), record('main', 4), record('main', 6, { status: 'unknown' })]) {
        h.plugin.load();
        await h.resolve(h.requests.length - 1, body);
        assert.ok(h.plugin.error);
        assert.equal(h.plugin.data.revision, 5);
      }
    } finally { h.plugin.destroy(); }
  });
  await check('a conflict preserves draft, refreshes and never retries the mutation', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.input('criteria', 'My draft');
      h.click('save');
      await h.reject(1, new Error('409 Goal 已更新'));
      assert.equal(h.requests.length, 3);
      assert.equal(h.requests[2].options.method, undefined);
      await h.resolve(2, record('main', 2, { goal: 'Other writer', criteria: 'Remote criteria' }));
      assert.equal(h.plugin.criteria.value, 'My draft');
      assert.equal(h.plugin.data.goal, 'Other writer');
      assert.equal(h.plugin.conflict, true);
      assert.equal(h.plugin.save.disabled, true);
      await h.plugin.mutate('set');
      assert.equal(h.requests.length, 3);
      h.click('discard');
      assert.equal(h.plugin.criteria.value, 'Remote criteria');
      assert.equal(h.plugin.conflict, false);
    } finally { h.plugin.destroy(); }
  });
  await check('event updates never overwrite a dirty editor', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.input('criteria', 'Local conditions');
      const notify = h.subscribers.get('ide_goal_updated');
      notify({ session_id: 'other', revision: 2 });
      notify({ session_id: 'main', revision: 1 });
      assert.equal(h.requests.length, 1);
      notify({ session_id: 'main', revision: 2 });
      await h.resolve(1, record('main', 2, { criteria: 'Remote conditions' }));
      assert.equal(h.plugin.criteria.value, 'Local conditions');
      assert.equal(h.plugin.conflict, true);
    } finally { h.plugin.destroy(); }
  });
  await check('event arriving during a read triggers one follow-up read', async () => {
    const h = await harness();
    try {
      h.subscribers.get('ide_goal_updated')({ session_id: 'main', revision: 2 });
      assert.equal(h.requests.length, 1);
      await h.resolve(0, record());
      assert.equal(h.requests.length, 2);
      await h.resolve(1, record('main', 2));
      assert.equal(h.plugin.data.revision, 2);
    } finally { h.plugin.destroy(); }
  });
  await check('pause retains records and resume does not reset budgets', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 8, { status: 'reviewing', rounds: 5, reviews: [{ verdict: { passed: false, feedback: 'Need tests' } }] }));
      assert.equal(h.plugin.pause.hidden, false);
      h.click('pause');
      const paused = record('main', 9, { status: 'paused', rounds: 5, reviews: h.plugin.data.reviews, pause_reason: '工作区已切换' });
      await h.resolve(1, paused);
      assert.equal(h.plugin.data.reviews.length, 1);
      assert.equal(h.plugin.resume.hidden, false);
      h.click('resume');
      const body = JSON.parse(h.requests[2].options.body);
      assert.equal(body.action, 'resume');
      assert.equal(body.expected_revision, 9);
      assert.equal('rounds' in body, false);
      assert.equal('goal_version' in body, false);
    } finally { h.plugin.destroy(); }
  });
  await check('unsaved edits never prevent pause or discard the draft', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 1, { status: 'running' }));
      h.input('criteria', 'Draft while running');
      assert.equal(h.plugin.pause.disabled, false);
      h.click('pause');
      await h.resolve(1, record('main', 2, { status: 'paused' }));
      assert.equal(h.plugin.criteria.value, 'Draft while running');
      assert.equal(h.plugin.dirty, true);
      assert.equal(h.plugin.data.status, 'paused');
    } finally { h.plugin.destroy(); }
  });
  await check('clear is explicit, confirmed and separate from Todo', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.click('clear');
      assert.equal(h.requests.length, 1);
      assert.equal(h.plugin.confirm.hidden, false);
      assert.equal(h.plugin.confirmCancel.focused, true);
      h.click('confirmCancel');
      assert.equal(h.requests.length, 1);
      h.click('clear');
      h.click('confirmYes');
      assert.equal(JSON.parse(h.requests[1].options.body).action, 'clear');
      await h.resolve(1, record('main', 2, { goal: '', criteria: '', status: 'empty' }));
      assert.equal(h.plugin.clear.hidden, true);
      assert.equal(h.plugin.data.status, 'empty');
      assert.ok(h.requests.every(request => request.url.startsWith('/plugins/ide/goal?')));
    } finally { h.plugin.destroy(); }
  });
  await check('text is never HTML and keyboard controls retain accessible focus', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 2, { goal: '<script>not executable</script>', reviews: [{ verdict: { passed: false, feedback: '<img onerror=evil()>' } }] }));
      assert.equal(h.plugin.title.textContent, '当前目标');
      assert.equal(h.plugin.goalText.textContent, '<script>not executable</script>');
      assert.ok(h.plugin.reviews.textContent.includes('<img onerror=evil()>'));
      assert.equal(h.plugin.root.attrs['aria-label'], '当前会话 Goal');
      assert.equal(h.plugin.reviews.attrs['aria-label'], '评审记录');
      h.click('clear');
      let stopped = 0;
      const escape = { key: 'Escape', stopPropagation() { stopped++; } };
      h.plugin.root.events.get('keydown')(escape);
      assert.equal(stopped, 1);
      assert.equal(h.plugin.clear.focused, true);
      assert.equal(h.plugin.confirm.hidden, true);
      assert.equal(h.controls.closes, 0, 'Escape dismisses confirmation before panel');
      h.plugin.root.events.get('keydown')(escape);
      assert.equal(stopped, 2);
      assert.equal(h.controls.closes, 1);
    } finally { h.plugin.destroy(); }
  });
  await check('editing uses the composer without closing the panel', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 7));
      h.controls.accepted = false;
      await h.click('edit');
      assert.equal(h.controls.closes, 0);
      assert.equal(h.controls.edits[0][0], 'ide.goal');
      assert.equal(h.controls.edits[0][1], 'Complete the change');
      assert.equal(h.controls.edits[0][2].sessionId, 'main');
      assert.equal(h.controls.edits[0][2].revision, 7);
      h.controls.accepted = undefined;
      await h.click('edit');
      assert.equal(h.controls.closes, 0, 'an unconfirmed host result must not close the panel');
      h.controls.accepted = true;
      await h.click('edit');
      assert.equal(h.controls.closes, 0, 'editing keeps the panel open for Goal input');
      assert.equal(h.requests.length, 1, 'editing never submits or restarts the Goal');
    } finally { h.plugin.destroy(); }
  });
  await check('running settings require confirmation and the panel has no popover toggle', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 1, { status: 'running' }));
      assert.equal(h.plugin.toggle, undefined);
      assert.equal(h.plugin.body.hidden, false);
      assert.equal(h.plugin.settings.tag, 'details');
      h.input('criteria', 'New settings');
      h.controls.confirm = false;
      h.click('save');
      assert.equal(h.requests.length, 1);
      h.controls.confirm = true;
      h.click('save');
      assert.equal(JSON.parse(h.requests[1].options.body).action, 'set');
    } finally { h.plugin.destroy(); }
  });
  await check('empty and completed panels expose no automatic execution controls', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 1, { goal: '', criteria: '', status: 'empty' }));
      assert.equal(h.plugin.root.hidden, false);
      assert.ok(h.plugin.goalText.textContent.includes('尚未设置目标'));
      assert.equal(h.plugin.settings.hidden, true);
      assert.equal(h.plugin.resume.hidden, true);
      assert.equal(h.plugin.pause.hidden, true);
      assert.equal(h.plugin.edit.hidden, true);
      assert.equal(h.plugin.emptyReviews.hidden, false);
      h.plugin.load();
      await h.resolve(1, record('main', 2, { status: 'completed', reviews: [{ applied: true, verdict: { passed: true, feedback: 'Verified' } }] }));
      assert.equal(h.plugin.badge.textContent, '已完成');
      assert.equal(h.plugin.resume.hidden, true);
      assert.equal(h.plugin.pause.hidden, true);
      assert.equal(h.plugin.emptyReviews.hidden, true);
      assert.ok(h.plugin.reviews.textContent.includes('第 1 次 · 评审通过'));
    } finally { h.plugin.destroy(); }
  });
  await check('polling is bounded and paused in a hidden document', async () => {
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
    } finally { h.plugin.destroy(); }
  });
  await check('destroy clears listeners, pending reads, timers and late responses', async () => {
    const h = await harness();
    h.plugin.destroy();
    assert.equal(h.requests[0].options.signal.aborted, true);
    assert.equal(h.intervals.size, 0);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.subscribers.size, 0);
    assert.equal(h.document.events.size, 0);
    assert.equal(h.plugin.root.removed, true);
    await h.resolve(0, record());
    assert.equal(h.plugin.data, null);
    await h.plugin.load();
    assert.equal(h.requests.length, 1);
  });
  await check('mobile editor and submit use the host draft without an independent Goal write', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      let text = 'shared draft', submissions = 0;
      h.ctx.data.viewportWidth = () => 390;
      h.ctx.composer.getDraft = async () => ({ text, mode: 'ide.goal', busy: false, attachments: [] });
      h.ctx.composer.setDraft = async value => { text = value; return true; };
      h.ctx.composer.submit = async () => { submissions++; text = ''; return true; };
      await h.plugin.syncDraft();
      assert.equal(h.plugin.compose.hidden, false);
      assert.equal(h.plugin.draft.value, text);
      h.input('draft', 'phone draft');
      await h.plugin.draftWrite;
      assert.equal(text, 'phone draft');
      assert.equal(h.requests.length, 1);
      const submitted = h.plugin.submitGoal();
      await drain();
      assert.equal(submissions, 1);
      assert.equal(h.requests[1].options.method, undefined, 'post-submit refresh is a read');
      await h.resolve(1, record('main', 2, { goal: 'phone draft', status: 'running' }));
      await submitted;
      assert.equal(h.plugin.draft.value, '');
      h.ctx.data.viewportWidth = () => 1440;
      await h.plugin.syncDraft();
      assert.equal(h.plugin.compose.hidden, true);
    } finally { h.plugin.destroy(); }
  });
  await check('completed target offers independent creation with explicit action metadata', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record('main', 7, {status:'completed'}));
      assert.equal(h.plugin.edit.textContent, '以此创建新目标');
      assert.equal(h.plugin.submitDraft.textContent, '创建并开始');
      await h.click('edit');
      assert.equal(h.controls.edits.at(-1)[2].action, 'create');
      assert.equal(h.controls.edits.at(-1)[2].revision, 7);
    } finally { h.plugin.destroy(); }
  });
  await check('rejected host draft write prevents submitting stale text', async () => {
    const h = await harness();
    try {
      await h.resolve(0, record());
      h.ctx.data.viewportWidth = () => 390;
      h.ctx.composer.getDraft = async () => ({ text: 'old', mode: 'ide.goal', busy: false, attachments: [] });
      h.ctx.composer.setDraft = async () => false;
      h.ctx.composer.submit = async () => { throw new Error('must not submit'); };
      await h.plugin.syncDraft();
      h.input('draft', 'local text');
      await h.plugin.draftWrite;
      await h.plugin.submitGoal();
      assert.equal(h.requests.length, 1);
      assert.equal(h.plugin.draft.value, 'local text');
      assert.ok(h.plugin.draftError.includes('不可修改'));
    } finally { h.plugin.destroy(); }
  });
  await check('mobile active states hide input until explicit modification and retain unsent drafts', async () => {
    for (const status of ['running', 'waiting', 'reviewing']) {
      const h = await harness();
      try {
        await h.resolve(0, record('main', 1, {status}));
        h.ctx.data.viewportWidth = () => 390;
        h.ctx.composer.getDraft = async () => ({text:'unsent draft', mode:'ide.goal', busy:false, attachments:[]});
        await h.plugin.syncDraft();
        assert.equal(h.plugin.compose.hidden, true);
        h.controls.accepted = false;
        await h.click('edit');
        assert.equal(h.plugin.compose.hidden, true);
        h.controls.accepted = true;
        await h.click('edit');
        assert.equal(h.plugin.compose.hidden, false);
        assert.equal(h.plugin.draft.value, 'unsent draft');
        assert.equal(h.controls.edits.at(-1)[1], 'unsent draft', 'keep existing text while recording the explicit edit baseline');
        assert.equal(h.controls.edits.at(-1)[2].action, 'submit');
        assert.equal(h.controls.edits.at(-1)[2].revision, 1);
        assert.equal(h.plugin.submitDraft.textContent, '修改并重新开始');
        assert.equal(h.requests.length, 1, 'modify opens editor without a Goal mutation');
        h.plugin.editingGoal = false;
        h.plugin.renderDraft();
        assert.equal(h.plugin.compose.hidden, true);
        assert.equal(h.plugin.draft.value, 'unsent draft');
      } finally { h.plugin.destroy(); }
    }
  });
  await check('Web transport supplies stable request identity and authenticated user preempt', async () => {
    const apiPath = path.resolve(__dirname, '../adapters/web/frontend/src/api/supervisorClient.ts');
    const script = stripTypeScriptTypes(fs.readFileSync(apiPath, 'utf8'), { mode: 'strip' });
    const requests = [];
    const context = vm.createContext({
      localStorage: { getItem() { return 'test-token'; } },
      fetch: async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => ({ session_id: 'main', inbound_seq: 20, accepted: true, ok: true, handled: true }) };
      },
    });
    const mod = new vm.SourceTextModule(script, { context, identifier: apiPath });
    await mod.link(() => { throw new Error('API has only erased type imports'); });
    await mod.evaluate();
    const api = mod.namespace;
    await api.postInbound({ conversation_key: 'web:one', text: '/goal pause', message_id: 'request-1' });
    const inbound = JSON.parse(requests[0].options.body);
    assert.equal(inbound.message_id, 'request-1');
    assert.equal(inbound.request_id, 'request-1');
    assert.equal(inbound.user_input, true);
    assert.equal(inbound.text, '/goal pause');
    assert.equal(requests[0].options.headers.Authorization, 'Bearer test-token');
    const result = await api.preemptTask('task-1', '/goal pause', [], 'main', 'request-2');
    const preempt = JSON.parse(requests[1].options.body);
    assert.equal(preempt.user_input, true);
    assert.equal(preempt.request_id, 'request-2');
    assert.equal(preempt.session_id, 'main');
    assert.equal(preempt.message, '/goal pause');
    assert.equal(result.handled, true);
    assert.equal(requests.length, 2, 'no frontend parsing or additional Goal API calls');
    assert.notEqual(api.createClientRequestId(), api.createClientRequestId());
    // Parse both touched TypeScript modules with Node's bundled TS parser.
    stripTypeScriptTypes(fs.readFileSync(path.resolve(__dirname, '../adapters/web/frontend/src/store/chatStore.ts'), 'utf8'), { mode: 'strip' });
  });
  await check('temporary primary sessions can edit despite the legacy child-view flag', async () => {
    const h=await harness('main',{isChildSession:true});
    try {
      await h.resolve(0,record());assert.equal(h.plugin.body.hidden,false);
      await h.click('edit');assert.equal(h.controls.edits.at(-1)[2].sessionId,'main');
    } finally {h.plugin.destroy();}
  });
  await check('verified branch aliases label ownership, keep raw composer metadata and mutate the owner', async () => {
    const h=await harness('branch',{isChildSession:true});
    try {
      await h.resolve(0,record('owner',7,{requested_session_id:'branch'}));
      assert.equal(h.plugin.ownerSessionId,'owner');assert.equal(h.plugin.sessionId,'branch');
      assert.match(h.plugin.ownerNotice.textContent,/主会话：owner/);
      assert.equal(h.plugin.ownerNotice.hidden,false);
      await h.click('edit');
      assert.equal(h.controls.edits.at(-1)[2].sessionId,'branch');
      assert.equal(h.controls.edits.at(-1)[2].ownerSessionId,'owner');
      h.input('criteria','Branch edit');h.click('save');
      assert.ok(h.requests[1].url.endsWith('session_id=owner'));
      assert.equal(JSON.parse(h.requests[1].options.body).expected_session_id,'owner');
      await h.resolve(1,record('owner',8,{criteria:'Branch edit'}));
      assert.equal(h.plugin.error,'');assert.equal(h.plugin.data.revision,8);
      h.plugin.load();assert.ok(h.requests[2].url.endsWith('session_id=branch'));
      await h.resolve(2,record('owner',8,{requested_session_id:'branch'}));
    } finally {h.plugin.destroy();}
  });
  await check('alias proof is required and a different canonical owner is not silently adopted', async () => {
    const h=await harness('branch');
    try {
      await h.resolve(0,record('owner',1,{requested_session_id:'foreign'}));
      assert.equal(h.plugin.data,null);assert.ok(h.plugin.error);
      h.plugin.load();await h.resolve(1,record('owner',1,{requested_session_id:'branch'}));
      h.plugin.load();await h.resolve(2,record('other-owner',2,{requested_session_id:'branch'}));
      assert.equal(h.plugin.ownerSessionId,'owner');assert.match(h.plugin.error,/归属/);
      await h.plugin.mutate('resume');assert.equal(h.requests.length,3);
    } finally {h.plugin.destroy();}
  });
  await check('late draft reads and delayed edits cannot replace another viewed session draft', async () => {
    const h=await harness('one');
    try {
      await h.resolve(0,record('one',1,{status:'running'}));
      h.ctx.data.viewportWidth=()=>390;
      const delayed=deferred();h.ctx.composer.getDraft=()=>delayed.promise;
      h.plugin.mobile=true;h.plugin.compose.hidden=true;
      const edit=h.click('edit');h.update('two');
      await h.resolve(1,record('two'));
      delayed.resolve({text:'old session draft',mode:'ide.goal',busy:false,attachments:[]});await edit;await drain();
      assert.equal(h.controls.edits.length,0);assert.equal(h.plugin.editingGoal,false);
      assert.notEqual(h.plugin.draft.value,'old session draft');
    } finally {h.plugin.destroy();}
  });
  fs.writeSync(1, JSON.stringify({ tests: passed, result: 'passed' }) + '\n');
  process.exit(0);
})().catch(error => { fs.writeSync(2, String(error.stack || error) + '\n'); process.exit(1); });
