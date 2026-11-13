// Run: node tests/test_composer_temporary.cjs (Node 22.13+).
// Execute the real host callbacks with store/API substitutes; no network or React reimplementation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { stripTypeScriptTypes } = require('node:module');
const source = fs.readFileSync('adapters/web/frontend/src/components/chat/ChatInput.tsx', 'utf8');
const between = (start, end) => {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `Callback boundaries: ${start}`);
  return source.slice(from, to);
};
const modesSource = fs.readFileSync('adapters/web/frontend/src/store/composerModes.ts', 'utf8');
const resolver = modesSource.slice(modesSource.indexOf('export function resolveComposerSubmission'), modesSource.indexOf('export const composerDrafts')).replace('export function', 'function');
const executable = stripTypeScriptTypes(resolver
  + between('  const setMode = useCallback(', '  const registerMode = useCallback(')
  + between('  // The panel and form share one transaction', '  const handleDraftChange')
  + '\nglobalThis.api={submitDraft,handleSubmit};', { mode: 'strip' });

function harness({ temporary = false, welcome = false, noParent = false } = {}) {
  const actions = new Map(), writes = [], modes = new Map(), stores = new Map();
  const activeId = welcome || noParent ? null : 'sidebar-a';
  const sid = temporary ? 'temporary-b' : welcome ? '' : 'session-a';
  const state = { activeConversationId: activeId, viewingChildSessionId: temporary ? sid : null,
    conversations: activeId ? [{ id: activeId, sessionId: 'session-a' }] : [] };
  const c = {
    console, AbortController, Map, Set, Promise,
    alive: { current: true }, busyRef: { current: false },
    draftRef: { current: { text: 'draft', mode: 'ide.goal', attachments: [] } }, modes: { current: modes },
    contextSessionId: sid, activeConversationId: activeId, isTemporaryView: temporary,
    scope: temporary ? `child:${sid}` : activeId || 'welcome',
    conversationKey: temporary ? 'discord:123' : activeId ? `web:${activeId}` : '',
    preparedSessionRef: { current: '' }, scopeRef: { current: temporary ? `child:${sid}` : activeId || 'welcome' },
    composerDisabled: false, showStop: false, modeIntent: true, isGenerating: false,
    knownCommands: { current: new Set(['/goal']) }, submission: { current: null }, composerDrafts: new Map(),
    useCallback: fn => fn, useEffect: fn => { c.dispose = fn(); }, useChatStore: { getState: () => state },
    registerHostAction: (name, fn) => { actions.set(name, fn); return () => actions.delete(name); },
    getComposerModeState: (scope, id) => { const key = scope + id; if (!stores.has(key)) stores.set(key, {}); return stores.get(key); },
    captureStoreGeneration: () => 1, isCurrentStoreGeneration: value => value === 1,
    createConversationId: () => { writes.push(['create-conversation']); return 'new-c'; }, createClientRequestId: () => 'request-id',
    ensureComposerSession: async () => { writes.push(['ensure-session']); return 'new-session'; },
    uploadAttachment: () => {}, uploadComposerAttachments: async (items, key, upload, current) => {
      writes.push(['upload', key]); if (!current()) throw new Error('会话已切换'); return items;
    },
    bindComposerSession: (...args) => writes.push(['bind', ...args]), revokeLocalAttachmentUrls: () => {},
    requestAnimationFrame: fn => fn(), textareaRef: { current: { focus: () => {} } }, window: { confirm: () => true },
    cancelCurrentTask: async () => writes.push(['cancel']), preemptCurrentTask: async () => writes.push(['preempt']),
    onSend: async () => writes.push(['normal-send']),
  };
  for (const key of ['setInputMode', 'setDraft', 'setAttachments', 'setSubmitError', 'setSubmitBusy', 'setPreparedSessionId']) {
    c[key] = value => writes.push([key, value]);
  }
  let submitted;
  modes.set('ide.goal', { id: 'ide.goal', command: '/goal', onSubmit: async payload => { submitted = payload; } });
  vm.createContext(c); vm.runInContext(executable, c);
  return { c, state, actions, writes, modes, stores, get submitted() { return submitted; } };
}

(async () => {
  let checks = 0;
  for (const noParent of [false, true]) {
    const h = harness({ temporary: true, noParent });
    const before = JSON.stringify(h.state);
    assert.equal(h.actions.get('setComposerMode')('ide.goal', 'B goal', { sessionId: 'temporary-b', focus: false }), true);
    assert.equal(h.actions.get('getComposerDraft')({ sessionId: 'temporary-b' }).text, 'B goal');
    assert.equal(h.actions.get('setComposerMode')('ide.goal', 'wrong', { sessionId: 'session-a' }), false);
    assert.equal(await h.actions.get('submitComposer')({ sessionId: 'temporary-b' }), true);
    assert.equal(h.submitted.sessionId, 'temporary-b');
    assert.equal(h.submitted.conversationKey, 'discord:123');
    assert.equal(h.submitted.text, 'B goal');
    assert.equal(JSON.stringify(h.state), before);
    assert.equal(h.writes.some(row => ['bind', 'ensure-session', 'create-conversation'].includes(row[0])), false);
    assert.equal(h.c.draftRef.current.text, ''); checks += 9;
  }
  {
    const h = harness({ temporary: true });
    h.state.viewingChildSessionId = 'temporary-c';
    assert.equal(h.actions.get('setComposerDraft')('late text', { sessionId: 'temporary-b' }), false);
    await assert.rejects(h.actions.get('submitComposer')({ sessionId: 'temporary-b' }), /失效/);
    assert.equal(await h.c.api.submitDraft(true), false);
    assert.equal(h.submitted, undefined); checks += 4;
  }
  {
    const h = harness({ temporary: true }); let release;
    h.c.uploadComposerAttachments = async () => { await new Promise(resolve => { release = resolve; }); return []; };
    const pending = h.actions.get('submitComposer')({ sessionId: 'temporary-b' });
    h.state.viewingChildSessionId = 'temporary-c'; release();
    assert.equal(await pending, false);
    assert.equal(h.submitted, undefined);
    assert.equal(h.c.draftRef.current.text, 'draft'); checks += 3;
  }
  {
    const h = harness({ temporary: true }); let release, entered;
    const started = new Promise(resolve => { entered = resolve; });
    h.modes.get('ide.goal').onSubmit = async () => { entered(); await new Promise(resolve => { release = resolve; }); };
    const pending = h.actions.get('submitComposer')({ sessionId: 'temporary-b' });
    await started;
    h.state.viewingChildSessionId = 'temporary-c'; release();
    assert.equal(await pending, false); assert.equal(h.c.draftRef.current.text, 'draft'); checks += 2;
  }
  {
    const h = harness({ temporary: true });
    h.modes.get('ide.goal').onSubmit = async () => { throw new Error('409 conflict'); };
    await assert.rejects(h.actions.get('submitComposer')({ sessionId: 'temporary-b' }), /409/);
    assert.equal(h.c.draftRef.current.text, 'draft'); assert.equal(h.c.busyRef.current, false); checks += 3;
  }
  for (const welcome of [false, true]) {
    const h = harness({ welcome });
    assert.equal(await h.actions.get('submitComposer')({ sessionId: welcome ? 'no-session' : 'session-a' }), true);
    assert.equal(h.submitted.sessionId, welcome ? 'new-session' : 'session-a');
    assert.equal(h.writes.filter(row => row[0] === 'bind').length, welcome ? 2 : 1); checks += 3;
  }
  {
    const h = harness({ temporary: true });
    h.c.draftRef.current.mode = ''; h.c.modeIntent = false;
    await h.c.api.submitDraft();
    assert.equal(h.writes.some(row => row[0] === 'normal-send'), true);
    h.c.isGenerating = true; h.c.draftRef.current.text = 'append'; await h.c.api.submitDraft();
    assert.equal(h.writes.some(row => row[0] === 'preempt'), true);
    h.c.showStop = true; await h.c.api.submitDraft();
    assert.equal(h.writes.some(row => row[0] === 'cancel'), true); checks += 3;
  }
  console.log(`PASS ${checks} host checks: temporary session binding, no sidebar changes, stale requests, drafts, ordinary/welcome/preempt/cancel paths`);
})().catch(error => { console.error(error); process.exitCode = 1; });
