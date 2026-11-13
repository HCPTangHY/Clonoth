// node --experimental-vm-modules tests/test_goal_composer.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { stripTypeScriptTypes } = require('node:module');
let count = 0;
async function check(name, fn) { await fn(); count++; console.log('PASS ' + name); }
function el() { return { hidden: false, attrs: {}, style: {}, events: {}, children: [], setAttribute(k,v) { this.attrs[k] = v; }, addEventListener(k,v) { this.events[k] = v; }, append(...c) { this.children.push(...c); }, remove() {} }; }
async function moduleAt(path, context) {
  let code = fs.readFileSync(path, 'utf8');
  if (path.endsWith('.ts')) code = stripTypeScriptTypes(code, { mode: 'strip' });
  const mod = new vm.SourceTextModule(code, { context });
  await mod.link(() => { throw new Error('unexpected runtime import'); });
  await mod.evaluate(); return mod.namespace;
}
(async () => {
  const core = await moduleAt('adapters/web/frontend/src/store/composerModes.ts', vm.createContext({ URL: { revokeObjectURL() {} } }));
  const mode = { id: 'ide.goal', command: '/goal' };
  const resolve = (text, selected='') => core.resolveComposerSubmission(text, selected, [mode], new Set(['/goal']));
  await check('plain text remains plain; command and selected mode share body parsing', () => {
    assert.equal(resolve('hello').mode, undefined);
    assert.equal(resolve('/goal target').text, 'target');
    assert.equal(resolve('/goal target', 'ide.goal').text, 'target');
    assert.equal(resolve('target', 'ide.goal').text, 'target');
    assert.equal(resolve('/goal pause').text, 'pause');
    assert.equal(resolve('/goalkeeper').mode, undefined);
    assert.throws(() => resolve('/goal'), /正文/);
    assert.throws(() => resolve('', 'ide.goal'), /正文/);
  });
  await check('unloaded mode or known command never falls through to ordinary send', () => {
    assert.throws(() => core.resolveComposerSubmission('target', 'ide.goal', [], new Set(['/goal'])), /卸载/);
    assert.throws(() => core.resolveComposerSubmission('/goal target', '', [], new Set(['/goal'])), /不可用/);
  });
  await check('successful uploads cached for same conversation, never use blob or malformed paths', async () => {
    const file = {};
    const attachments = [{ file, name: 'a' }]; let calls = 0;
    const upload = async () => { calls++; return { path: 'data/attachments/a.png', name: 'a' }; };
    await core.uploadComposerAttachments(attachments, 'one', upload, () => true);
    await core.uploadComposerAttachments(attachments, 'one', upload, () => true);
    assert.equal(calls, 1);
    await core.uploadComposerAttachments(attachments, 'two', upload, () => true);
    assert.equal(calls, 2);
    await assert.rejects(core.uploadComposerAttachments([{name:'x', path:'blob:x'}], 'one', upload, () => true), /路径/);
    await assert.rejects(core.uploadComposerAttachments([{file:{}, name:'x'}], 'one', async () => ({path:123}), () => true), /路径/);
    await assert.rejects(core.uploadComposerAttachments(attachments, 'one', upload, () => false), /切换/);
    core.clearComposerDrafts();
    await core.uploadComposerAttachments(attachments, 'one', upload, () => true);
    assert.equal(calls, 3);
  });
  async function drain() { for (let i=0;i<8;i++) await Promise.resolve(); }
  function harness(options={}) { return (async () => {
    const calls = []; let fail = ''; let registered; let deleted = false;
    let resolveIdentity;
    const data = {status:'running',capabilities:{create:true},revision:1,...options.response};
    let confirm = true;
    const ctx = { state:{}, data:{sessionId:options.sessionId || 'main',...options.data}, el:el(), composer:{mode:'',busy:false,
      registerMode(mode) { registered=mode; return () => { deleted=true; }; }, setMode(id) {this.mode=id;} },
      api:{async call(...args) { (ctx.hostCalls ||= []).push(args); if (args[0]==='openPanel') Object.assign(ctx.data,{panelId:args[1],rightPanelOpen:true}); }, async request(url, requestOptions={}) { calls.push({url,options:requestOptions}); if (requestOptions.method === 'POST' && fail) throw new Error(fail);
        const sid=decodeURIComponent(url.split('session_id=')[1]);
        const result={session_id:sid, is_child:false, ...data, revision: requestOptions.method === 'POST' ? data.revision + 1 : data.revision};
        if (options.deferIdentity && !resolveIdentity) return new Promise(resolve=>{resolveIdentity=resolve;});
        return result; }} };
    const ns = await moduleAt('plugins/ide/web/goal_composer.js', vm.createContext({ AbortController, document:{createElement:el}, window:{confirm:()=>confirm,innerWidth:1440,addEventListener(){},removeEventListener(){},setTimeout(){return 1;},clearTimeout(){}} }));
    const plugin = Object.assign({}, ns.default); plugin.mount(ctx);
    await drain();
    const identityCall=calls[0];calls.length=0;
    return {ctx,plugin,calls,data,identityCall,async resolveIdentity(body){resolveIdentity(body);await drain();},get mode(){return registered;},get deleted(){return deleted;},set fail(value){fail=value;},set confirm(value){confirm=value;}};
  })(); }
  await check('Goal controls follow the square Duties palette rather than rounded indigo chips', async () => {
    const h=await harness();
    const css=h.plugin.style.textContent;
    assert.match(css, /border-radius:0/);
    assert.match(css, /min-height:24px/);
    assert.match(css, /border:1px solid var\(--duties-border/);
    assert.match(css, /background:var\(--duties-accent,#feaf2c\)/);
    assert.ok(!css.includes('99 102 241') && !css.includes('scale('));
    h.plugin.destroy();
  });
  const submit = (h, extra={}) => h.mode.onSubmit({text:'target',attachments:[],sessionId:'main',requestId:'r1',signal:new AbortController().signal,...extra});
  await check('Goal button opens panel; submit uses only Goal API and quotes attachment paths', async () => {
    const h=await harness(); await h.plugin.button.events.click(); h.plugin.update(h.ctx);
    assert.equal(h.ctx.composer.mode, 'ide.goal'); assert.equal(h.calls.length,0);
    assert.equal(h.ctx.hostCalls[0][0], 'openPanel');
    assert.equal(h.ctx.hostCalls[0][1], 'plugin:ide:goal');
    await submit(h,{attachments:[{name:'line\nname',path:'data/attachments/file.png'}]});
    assert.equal(h.calls.length,2);
    const body=JSON.parse(h.calls[1].options.body);
    assert.equal(body.action,'submit'); assert.equal(body.expected_revision,1);
    assert.ok(body.goal.includes('Clonoth 安装根目录')); assert.ok(body.goal.includes('"line\\nname"'));
    assert.ok(h.calls.every(call=>call.url.startsWith('/plugins/ide/goal?')));
    h.plugin.destroy(); assert.equal(h.deleted,true);
  });
  await check('lost HTTP acknowledgement reuses exact request body and identity', async () => {
    const h=await harness(); h.fail='network'; await assert.rejects(submit(h),/network/);
    const before=h.calls[1].options.body; h.fail=''; await submit(h,{requestId:'r2'});
    assert.equal(h.calls.length,3); assert.equal(h.calls[2].options.body,before);
    h.plugin.destroy();
  });
  await check('409 blocks repeat submit until explicit new-version action', async () => {
    const h=await harness(); h.fail='409 conflict'; await assert.rejects(submit(h),/409/);
    await assert.rejects(submit(h,{requestId:'r2'}),/核对/); assert.equal(h.calls.length,2);
    h.plugin.retry.events.click(); h.fail=''; await submit(h,{requestId:'r3'});
    assert.equal(h.calls.length,4); assert.equal(JSON.parse(h.calls[3].options.body).request_id,'r3');
    h.plugin.destroy();
  });
  await check('conflict UI is scoped to its session; temporary-view flags are not child identity', async () => {
    const h=await harness(); h.fail='409 conflict'; await assert.rejects(submit(h),/409/);
    assert.equal(h.plugin.retry.hidden,false);
    h.ctx.data={sessionId:'other'}; h.plugin.update(h.ctx);
    assert.equal(h.plugin.retry.hidden,true);
    h.ctx.data={sessionId:'main'}; h.plugin.update(h.ctx);
    assert.equal(h.plugin.retry.hidden,false);
    h.ctx.data={sessionId:'main',isChildSession:true}; h.plugin.update(h.ctx);await drain();
    assert.equal(h.plugin.retry.hidden,false);
    h.plugin.destroy();
  });
  await check('editing retains original CAS baseline instead of adopting a concurrent version', async () => {
    const h=await harness(); h.ctx.state.metadata={sessionId:'main',revision:0};
    await assert.rejects(submit(h),/核对/);
    assert.equal(h.calls.length,1,'only GET, never overwrites using fresh revision');
    assert.equal(h.plugin.retry.hidden,false);
    h.plugin.destroy();
  });
  await check('host-owned mode state survives plugin remount but logout clears retained data', async () => {
    const h=await harness();
    const state=core.getComposerModeState('conversation','ide.goal');
    h.fail='network'; await assert.rejects(submit(h,{state}),/network/);
    const before=h.calls[1].options.body;
    h.plugin.destroy();
    const next=await harness(); await submit(next,{state,requestId:'new-id'});
    assert.equal(next.calls.length,1,'no fresh revision GET');
    assert.equal(next.calls[0].options.body,before);
    state.privateText='secret'; core.clearComposerDrafts();
    assert.deepEqual(Object.keys(state),[]);
    next.plugin.destroy();
  });
  await check('empty and oversized objective never reaches API', async () => {
    const h=await harness(); await assert.rejects(submit(h,{text:''}),/长度/);
    await assert.rejects(submit(h,{text:'x'.repeat(16001)}),/长度/);
    assert.equal(h.calls.length,0); h.plugin.destroy();
  });
  await check('all rail close routes exit Goal mode without a business write', async () => {
    for (const next of [{rightPanelOpen:false,panelId:'plugin:ide:goal'}, {rightPanelOpen:true,panelId:null}, {rightPanelOpen:true,panelId:'files'}]) {
      const h=await harness();
      h.ctx.data={sessionId:'main',rightPanelOpen:true,panelId:'plugin:ide:goal'};h.plugin.update(h.ctx);await drain();
      assert.equal(h.ctx.composer.mode,'ide.goal');
      h.ctx.data={sessionId:'main',...next};h.plugin.update(h.ctx);
      assert.equal(h.ctx.composer.mode,'');assert.equal(h.calls.length,0);
      h.plugin.destroy();
    }
  });
  await check('desktop keeps Goal mode after submit while panel remains open', async () => {
    const h=await harness();h.ctx.data={sessionId:'main',rightPanelOpen:true,panelId:'plugin:ide:goal'};h.plugin.update(h.ctx);
    h.ctx.composer.mode='';h.plugin.update(h.ctx);assert.equal(h.ctx.composer.mode,'ide.goal');
    h.ctx.composer.busy=true;h.ctx.composer.mode='';h.plugin.update(h.ctx);assert.equal(h.ctx.composer.mode,'');
    h.ctx.composer.busy=false;h.plugin.update(h.ctx);assert.equal(h.ctx.composer.mode,'ide.goal');
    h.ctx.data.rightPanelOpen=false;h.plugin.update(h.ctx);assert.equal(h.ctx.composer.mode,'');h.plugin.destroy();
  });
  await check('restored Goal mode exits when the current rail is closed without dropping text or parsing fresh slash input', async () => {
    const h=await harness();let text='saved objective';
    h.ctx.composer.getDraft=()=>({text});h.ctx.composer.setDraft=value=>{text=value;return true;};
    h.ctx.data={sessionId:'A',conversationId:'a',panelId:'plugin:ide:goal',rightPanelOpen:true};h.plugin.update(h.ctx);await drain();
    h.ctx.data={sessionId:'B',conversationId:'b',panelId:null,rightPanelOpen:false};h.ctx.composer.mode='';h.plugin.update(h.ctx);await drain();
    h.ctx.data={sessionId:'A',conversationId:'a',panelId:null,rightPanelOpen:false};h.plugin.update(h.ctx);await drain();
    h.ctx.composer.mode='ide.goal';h.plugin.update(h.ctx);
    assert.equal(h.ctx.composer.mode,'');assert.equal(text,'saved objective');assert.equal(h.calls.some(c=>c.options.method==='POST'),false);
    text='/goal deliberate command';h.plugin.update(h.ctx);assert.equal(text,'/goal deliberate command');
    h.plugin.openingPanel=true;h.ctx.composer.mode='ide.goal';h.plugin.update(h.ctx);assert.equal(h.ctx.composer.mode,'ide.goal');
    h.plugin.openingPanel=false;h.plugin.destroy();
  });
  await check('empty and completed goals create while unfinished goals submit', async () => {
    for (const status of ['empty','completed','running','waiting','reviewing','paused','blocked']) {
      const h=await harness();h.data.status=status;await submit(h);
      assert.equal(JSON.parse(h.calls[1].options.body).action,['empty','completed'].includes(status)?'create':'submit');h.plugin.destroy();
    }
  });
  await check('completed creation cancellation and old backend never downgrade to submit', async () => {
    const h=await harness();h.data.status='completed';h.confirm=false;
    await assert.rejects(submit(h),/已取消/);assert.equal(h.calls.length,1);
    h.confirm=true;h.data.capabilities={};await assert.rejects(submit(h),/版本/);
    assert.equal(h.calls.length,2);h.plugin.destroy();
  });
  await check('conflict cannot change a frozen update to create by editing text', async () => {
    const h=await harness();h.fail='409';await assert.rejects(submit(h),/409/);
    h.data.status='completed';h.data.revision=2;h.fail='';
    await assert.rejects(submit(h,{text:'changed body'}),/核对/);
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
    assert.equal(h.ctx.state.metadata.action,'submit');
    h.plugin.retry.events.click();await submit(h,{text:'changed body',requestId:'r2'});
    assert.equal(JSON.parse(h.calls.at(-1).options.body).action,'create');h.plugin.destroy();
  });
  await check('lost create acknowledgement reuses the original create body', async () => {
    const h=await harness();h.data.status='empty';h.fail='network';await assert.rejects(submit(h),/network/);
    const body=h.calls[1].options.body;h.data.status='running';h.data.revision=2;h.fail='';await submit(h,{requestId:'new-id'});
    assert.equal(h.calls.length,3);assert.equal(h.calls[2].options.body,body);assert.equal(JSON.parse(body).action,'create');h.plugin.destroy();
  });
  await check('temporary primary sessions use backend identity rather than navigation flags', async () => {
    const h=await harness({data:{isChildSession:true,composerScope:'temporary:main'}});
    assert.equal(h.plugin.button.hidden,false);assert.equal(h.plugin.button.disabled,false);
    await h.plugin.button.events.click();assert.equal(h.ctx.composer.mode,'ide.goal');
    await submit(h);assert.equal(JSON.parse(h.calls.at(-1).options.body).expected_session_id,'main');h.plugin.destroy();
  });
  await check('identity loading stays quiet and late reads cannot change the newly viewed session', async () => {
    const h=await harness({deferIdentity:true});
    assert.equal(h.plugin.button.disabled,true);assert.equal(h.plugin.message.hidden,true);assert.equal(h.plugin.message.textContent,'');assert.equal(h.plugin.button.textContent,'◎ Goal');assert.equal(h.plugin.button.attrs['aria-busy'],'true');
    h.ctx.data={sessionId:'other',composerScope:'temporary:other'};h.plugin.update(h.ctx);await drain();
    assert.equal(h.identityCall.options.signal.aborted,true);
    await h.resolveIdentity({session_id:'main',is_child:true,status:'paused',revision:1});
    assert.equal(h.plugin.identity.session_id,'other');assert.equal(h.plugin.button.disabled,false);h.plugin.destroy();
    const destroyed=await harness({deferIdentity:true});destroyed.plugin.destroy();
    assert.equal(destroyed.identityCall.options.signal.aborted,true);
    await destroyed.resolveIdentity({session_id:'main',is_child:false,status:'paused',revision:1});
    assert.equal(destroyed.plugin.identity,null);
  });
  await check('backend child restriction applies even without a child navigation flag', async () => {
    const h=await harness({response:{is_child:true}});
    assert.equal(h.plugin.button.disabled,true);assert.equal(h.plugin.message.hidden,true);assert.equal(h.plugin.message.textContent,'');
    await assert.rejects(submit(h),/真实子会话/);
    assert.equal(h.calls.some(c=>c.options.method==='POST'),false);h.plugin.destroy();
  });
  await check('verified aliases read raw view but submit canonical owner and keep raw metadata', async () => {
    const h=await harness({sessionId:'branch',data:{isChildSession:true,composerScope:'temporary:branch'},response:{session_id:'owner',requested_session_id:'branch'}});
    assert.equal(h.plugin.button.disabled,false);assert.equal(h.plugin.message.hidden,true);assert.equal(h.plugin.message.textContent,'');
    await submit(h,{sessionId:'branch'});
    assert.ok(h.calls[0].url.endsWith('session_id=branch'));
    assert.ok(h.calls[1].url.endsWith('session_id=owner'));
    assert.equal(JSON.parse(h.calls[1].options.body).expected_session_id,'owner');
    assert.equal(h.plugin.identitySession,'branch');h.plugin.destroy();
  });
  await check('unverified owner redirection is rejected and a failed identity read can be retried', async () => {
    const h=await harness({response:{session_id:'foreign',requested_session_id:'not-main'}});
    assert.equal(h.plugin.message.hidden,true);assert.match(h.plugin.button.textContent,/重试/);assert.equal(h.plugin.button.disabled,false);
    await assert.rejects(submit(h),/会话/);assert.equal(h.calls.some(c=>c.options.method==='POST'),false);
    h.data.session_id='main';h.data.requested_session_id='main';
    await h.plugin.button.events.click();assert.equal(h.ctx.composer.mode,'ide.goal');assert.equal(h.plugin.identityError,'');h.plugin.destroy();
  });
  await check('composerScope isolates pending Goal state despite the same selected sidebar conversation', async () => {
    const h=await harness({sessionId:'A',data:{conversationId:'selected',composerScope:'temporary:A'}});
    const states=new Map();h.ctx.composer.getState=()=>{const key=h.ctx.data.composerScope;if(!states.has(key))states.set(key,{});return states.get(key);};
    h.plugin.update(h.ctx);h.fail='network';await assert.rejects(submit(h,{sessionId:'A'}),/network/);
    const pending=states.get('temporary:A').pendingSubmissions.A;
    h.ctx.data={sessionId:'B',conversationId:'selected',composerScope:'temporary:B'};h.plugin.update(h.ctx);await drain();
    assert.notEqual(h.plugin.store,states.get('temporary:A'));
    assert.equal(h.plugin.store.pendingSubmissions.A,undefined);
    assert.equal(states.get('temporary:A').pendingSubmissions.A,pending);h.plugin.destroy();
  });
  await check('switching the raw view while a submission read is pending never writes the old or new Goal', async () => {
    const h=await harness();let resolveRead;
    h.ctx.api.request=()=>new Promise(resolve=>{resolveRead=resolve;});
    const pending=submit(h);h.ctx.data={sessionId:'other'};
    resolveRead({session_id:'main',is_child:false,status:'running',revision:1});
    await assert.rejects(pending,/切换/);h.plugin.destroy();
  });
  fs.writeSync(1, JSON.stringify({tests:count,result:'passed'}) + '\n');
  process.exit(0);
})().catch(error=>{fs.writeSync(2, String(error.stack || error) + '\n');process.exit(1);});
