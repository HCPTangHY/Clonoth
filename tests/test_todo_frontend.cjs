// Session-owned Todo UI checks; no browser, network or dependencies.
// Run: node --experimental-vm-modules tests/test_todo_frontend.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sourcePath = path.resolve(__dirname, '../plugins/ide/web/todo_progress.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function element(tag = 'div') {
  let text = '';
  return {
    tag, attrs: {}, events: new Map(), dataset: {}, children: [], parent: null,
    hidden: false, scrollTop: 0, focused: false, removed: false,
    set textContent(value) { text = String(value); this.replaceChildren(); },
    get textContent() { return text + this.children.map(child => child.textContent).join(''); },
    set innerHTML(_) { throw new Error('Todo text must not be interpreted as HTML'); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(name, callback) { this.events.set(name, callback); },
    removeEventListener(name, callback) {
      if (this.events.get(name) === callback) this.events.delete(name);
    },
    append(...children) {
      for (const child of children) { child.parent = this; this.children.push(child); }
    },
    replaceChildren(...children) {
      for (const child of this.children) child.parent = null;
      this.children = [];
      this.append(...children);
    },
    contains(target) { return this === target || this.children.some(child => child.contains(target)); },
    focus() { this.focused = true; },
    remove() {
      this.removed = true;
      if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
      this.parent = null;
    },
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function drain() {
  // Flush the fake API continuation and its finally block without using timers.
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

function list(sessionId, title = 'Work', revision = 1) {
  return { schema_version: 2, session_id: sessionId, revision, items: [
    { id: 'one', title, status: 'pending', note: '' },
  ] };
}

async function harness(sessionId = 'main', extra = {}) {
  const requests = [], intervals = new Map(), timeouts = new Map();
  let nextTimer = 1;
  const document = element('document');
  document.hidden = false;
  document.createElement = element;
  const window = {
    setInterval(callback, ms) { const id = nextTimer++; intervals.set(id, { callback, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, ms) { const id = nextTimer++; timeouts.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
  };
  const context = vm.createContext({ document, window, AbortController });
  const module = new vm.SourceTextModule(source, { context, identifier: sourcePath });
  await module.link(() => { throw new Error('Unexpected Todo module import'); });
  await module.evaluate();
  const plugin = Object.assign({}, module.namespace.default);
  const state = {};
  const ctx = {
    el: element(), state, data: { sessionId, ...extra },
    api: { request(url, options) {
      const pending = deferred();
      requests.push({ url, options, ...pending });
      // Deliberately do not reject on abort: test late response guards as well.
      return pending.promise;
    } },
  };
  plugin.mount(ctx);
  return {
    plugin, ctx, document, intervals, timeouts, requests,
    async resolve(index, data) { requests[index].resolve(data); await drain(); },
    update(sessionId, extra = {}) { ctx.data = { sessionId, ...extra }; plugin.update(ctx); },
  };
}

let passed = 0;
async function check(name, test) {
  await test();
  passed++;
  fs.writeSync(1, `PASS ${name}\n`);
}

(async () => {
  await check('child session displays its own list without a workspace', async () => {
    const h = await harness('child', { isChildSession: true });
    try {
      assert.equal(h.requests.length, 1);
      assert.equal(h.requests[0].url, '/plugins/ide/todos?session_id=child');
      await h.resolve(0, list('child', 'Child task'));
      assert.equal(h.plugin.root.hidden, false);
      assert.equal(h.plugin.subject.textContent, 'Child task');
      assert.equal(h.plugin.data.session_id, 'child');
      assert.equal(h.plugin.toggle.attrs['aria-expanded'], 'false');
    } finally { h.plugin.destroy(); }
  });

  await check('workspace changes keep the same session list and expansion', async () => {
    const h = await harness();
    try {
      await h.resolve(0, list('main'));
      h.plugin.toggle.events.get('click')();
      assert.equal(h.ctx.state.expanded, true);
      h.update('main', { workspace: { name: 'different', path: '/other' }, isChildSession: true });
      assert.equal(h.requests.length, 1);
      assert.equal(h.ctx.state.expanded, true);
      assert.equal(h.plugin.subject.textContent, 'Work');
    } finally { h.plugin.destroy(); }
  });

  await check('same revision in different sessions never reuses old content', async () => {
    const h = await harness('first');
    try {
      await h.resolve(0, list('first', 'First task', 7));
      h.plugin.toggle.events.get('click')();
      h.update('second');
      assert.equal(h.plugin.data, null);
      assert.equal(h.plugin.root.hidden, true);
      assert.equal(h.plugin.body.children.length, 0);
      assert.equal(h.ctx.state.expanded, false);
      await h.resolve(1, list('second', 'Second task', 7));
      assert.equal(h.plugin.data.session_id, 'second');
      assert.equal(h.plugin.subject.textContent, 'Second task');
      assert.equal(h.plugin.root.hidden, false);
    } finally { h.plugin.destroy(); }
  });

  await check('aborted old responses cannot overwrite a new session', async () => {
    const h = await harness('first');
    try {
      h.update('second');
      assert.equal(h.requests[0].options.signal.aborted, true);
      assert.equal(h.timeouts.size, 1);
      await h.resolve(1, list('second', 'New task'));
      await h.resolve(0, list('first', 'Stale task'));
      assert.equal(h.plugin.subject.textContent, 'New task');
      assert.equal(h.plugin.data.session_id, 'second');
      assert.equal(h.timeouts.size, 0);
    } finally { h.plugin.destroy(); }
  });

  await check('a mismatched response session is rejected and can be retried', async () => {
    const h = await harness('main');
    try {
      await h.resolve(0, list('main', 'Visible task'));
      void h.plugin.load();
      await h.resolve(1, list('other', 'Foreign task'));
      assert.equal(h.plugin.data, null);
      assert.equal(h.plugin.count.textContent, '待办读取失败');
      assert.equal(h.plugin.subject.textContent, '点击重试');
      h.plugin.toggle.events.get('click')();
      await h.resolve(2, list('main', 'Recovered task'));
      assert.equal(h.plugin.subject.textContent, 'Recovered task');
      assert.equal(h.plugin.root.hidden, false);
    } finally { h.plugin.destroy(); }
  });

  await check('request failures retain retry UI and recovery rerenders the same revision', async () => {
    const h = await harness();
    try {
      await h.resolve(0, list('main', 'Task', 4));
      void h.plugin.load();
      h.requests[1].reject(new Error('offline'));
      await drain();
      assert.equal(h.plugin.root.hidden, false);
      assert.equal(h.plugin.data, null);
      assert.equal(h.plugin.body.hidden, true);
      assert.equal(h.plugin.toggle.attrs['aria-expanded'], 'false');
      assert.equal(h.plugin.controller, null);
      h.plugin.toggle.events.get('click')();
      await h.resolve(2, list('main', 'Task', 4));
      assert.equal(h.plugin.subject.textContent, 'Task');
      assert.equal(h.plugin.count.textContent, '待办 0/1');
    } finally { h.plugin.destroy(); }
  });

  await check('polling remains four seconds and pauses while hidden', async () => {
    const h = await harness();
    try {
      await h.resolve(0, list('main'));
      const interval = [...h.intervals.values()][0];
      assert.equal(interval.ms, 4000);
      h.document.hidden = true;
      interval.callback();
      h.document.events.get('visibilitychange')();
      assert.equal(h.requests.length, 1);
      h.document.hidden = false;
      h.document.events.get('visibilitychange')();
      assert.equal(h.requests.length, 2);
      interval.callback();
      assert.equal(h.requests.length, 2, 'no overlapping requests');
      await h.resolve(1, list('main'));
      interval.callback();
      assert.equal(h.requests.length, 3);
    } finally { h.plugin.destroy(); }
  });

  await check('titles and notes remain text and keyboard collapse stays accessible', async () => {
    const h = await harness();
    try {
      const data = list('main', '<img src=x onerror=alert(1)>');
      data.items[0].note = '<script>not executable</script>';
      await h.resolve(0, data);
      h.plugin.toggle.events.get('click')();
      assert.equal(h.plugin.subject.textContent, data.items[0].title);
      assert.ok(h.plugin.body.textContent.includes(data.items[0].note));
      assert.equal(h.plugin.toggle.attrs['aria-expanded'], 'true');
      assert.equal(h.plugin.toggle.attrs['aria-controls'], h.plugin.body.id);
      let stopped = false;
      h.plugin.root.events.get('keydown')({ key: 'Escape', stopPropagation() { stopped = true; } });
      assert.equal(stopped, true);
      assert.equal(h.plugin.toggle.attrs['aria-expanded'], 'false');
      assert.equal(h.plugin.toggle.focused, true);
    } finally { h.plugin.destroy(); }
  });

  await check('destroy removes timers and listeners and ignores pending responses', async () => {
    const h = await harness();
    assert.equal(h.timeouts.size, 1);
    assert.equal(h.intervals.size, 1);
    assert.equal(h.document.events.size, 2);
    h.plugin.destroy();
    assert.equal(h.requests[0].options.signal.aborted, true);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
    assert.equal(h.document.events.size, 0);
    assert.equal(h.plugin.controller, null);
    assert.equal(h.plugin.root.removed, true);
    assert.equal(h.plugin.style.removed, true);
    await h.resolve(0, list('main', 'Late task'));
    assert.equal(h.plugin.data, null);
    await h.plugin.load();
    assert.equal(h.requests.length, 1);
  });

  fs.writeSync(1, JSON.stringify({ tests: passed, result: 'passed' }) + '\n');
  // ponytail: exit explicitly after async checks to avoid old-kernel libuv teardown failures.
  process.exit(0);
})().catch(error => {
  fs.writeSync(2, String(error.stack || error) + '\n');
  process.exit(1);
});
