"""Real inference interruption and durable replay, without external model/service calls."""
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from engine import runner
from engine.conversation_store import ConversationStore, Message
from engine.hooks.types import HookResult
from engine.inference import ai_step
from engine.node import Node, ToolAccess
from engine.protocol import TaskAction
from engine.restart_recovery import (cancelled_task_result, current_context, repair_restart_history,
                                     restore_runtime_state)
from providers.base import ProviderResponse, ToolCall
from providers.tool_format import create_tool_formatter
from test_engine_execution_migration import FakeRegistry, make_ls, no_hook, spec


@pytest.mark.parametrize('mode', ['native', 'fake-native', 'json'])
def test_cancelled_real_run_keeps_completed_results_closes_unfinished_pairs_and_resumes_budget(tmp_path, mode):
    async def run():
        entered = asyncio.Event()
        async def blocked(args, ctx):
            entered.set()
            await asyncio.Event().wait()
        registry = FakeRegistry({name: spec(name) for name in ['done', 'slow', 'later']}, {'slow': blocked})
        store = ConversationStore(tmp_path/'data/conversations')
        node = Node(id='test', type='ai', tool_mode=mode, tool_access=ToolAccess(mode='all'))
        ls = make_ls(tmp_path, registry=registry, node=node, allowed_real={'done','slow','later'}, allowed_pseudo={'finish'}, store=store)
        ls.formatter = create_tool_formatter(mode)
        response = ProviderResponse(ok=True, text='', tool_calls=[
            ToolCall(id='a',name='done',arguments={}), ToolCall(id='b',name='slow',arguments={}),
            ToolCall(id='c',name='later',arguments={}), ToolCall(id='d',name='finish',arguments={'text':'no'})])
        async def fire(point, ctx):
            if point=='on_task_error': return HookResult(action=TaskAction(action='fail',error='limit'))
            return HookResult()
        with ExitStack() as patches:
            patches.enter_context(patch.object(ai_step,'auto_discover_and_register'))
            patches.enter_context(patch.object(ai_step,'load_external_plugins'))
            patches.enter_context(patch.object(ai_step,'load_runtime_config',return_value={'engine':{'max_steps':2}}))
            patches.enter_context(patch.object(ai_step,'assemble_initial_messages',return_value=([],False,[])))
            patches.enter_context(patch.object(ai_step,'build_node_tool_specs',return_value=({'done','slow','later'},[])))
            patches.enter_context(patch.object(ai_step,'allowed_pseudo_names',return_value={'finish'}))
            patches.enter_context(patch.object(ai_step,'_build_loop_state',return_value=ls))
            patches.enter_context(patch.object(ai_step.hook_registry,'afire',side_effect=fire))
            call=patches.enter_context(patch.object(ai_step,'_call_llm_with_retry',new=AsyncMock(return_value=response)))
            execution=asyncio.create_task(ai_step.run_ai_node(rctx=ls.rctx,provider=ls.provider,registry=registry,node=node,instruction='work',history=[]))
            await entered.wait(); execution.cancel()
            with pytest.raises(asyncio.CancelledError): await execution
            assert registry.started==['done','slow']
            persisted=store.load('session')
            assert [m.tool_call_id for m in persisted if m.role=='tool']==['a','b','c','d']
            outputs=[m for m in persisted if m.role=='tool']
            assert outputs[0].meta['tool_result_structured']['ok'] is True
            assert all(m.meta.get('tool_result_structured',{}).get('cancelled') for m in outputs[1:])
            assert 'external side effects' in outputs[1].content
            cancelled=cancelled_task_result(ls.rctx,node.id)
            assert cancelled['_runtime_state']['completed_steps']==1
            assert not cancelled.get('checkpoint_error')
            history=[runner._message_to_history_dict(m,current_task_id='task') for m in persisted]
            ls.messages=history
            restore_runtime_state(ls.rctx,cancelled['_runtime_state'])
            call.reset_mock(); call.return_value=ProviderResponse(ok=True,text='continue')
            result=await ai_step.run_ai_node(rctx=ls.rctx,provider=ls.provider,registry=registry,node=node,instruction='',history=history,resume_data={'type':'restart_interrupted'})
            assert call.await_count==1
            assert call.await_args.args[1]==1
            assert ls.rctx.completed_steps==2 and result.action=='fail'
            assert registry.started==['done','slow']
    asyncio.run(run())


def test_restart_pair_repair_is_task_scoped_ordered_persistent_and_idempotent(tmp_path):
    store=ConversationStore(tmp_path/'history')
    store.append_batch('s',[
        Message(id='old',role='assistant',content='',tool_calls=[{'id':'old-call','name':'write_file'}],source_task_id='other'),
        Message(id='input',role='user',content='work',source_task_id='t'),
        Message(id='assistant',role='assistant',content='',tool_calls=[{'id':'restart','name':'request_restart'}, {'id':'lost','name':'write_file'}],source_task_id='t'),
        Message(id='suffix',role='user',content='already received callback',source_task_id='t'),
    ])
    ctx=SimpleNamespace(task_id='t',session_id='s',child_session_id='',conversation_store=store)
    node=Node(id='n',type='ai',tool_mode='native')
    resume={'type':'restart_interrupted','restart_tool_call_id':'restart'}
    history=[runner._message_to_history_dict(m,current_task_id='t') for m in store.load('s')]
    repair_restart_history(history,resume,create_tool_formatter('native'),ctx,node)
    rows=store.load('s')
    assert [m.id if m.role!='tool' else m.tool_call_id for m in rows][:6]==['old','input','assistant','restart','lost','suffix']
    assert rows[3].meta['tool_result_structured']['scheduled'] is True
    assert rows[4].meta['tool_result_structured']['cancelled'] is True
    before=(tmp_path/'history/s.jsonl').read_bytes()
    second=[runner._message_to_history_dict(m,current_task_id='t') for m in store.load('s')]
    repair_restart_history(second,resume,create_tool_formatter('native'),ctx,node)
    assert (tmp_path/'history/s.jsonl').read_bytes()==before
    assert len(second)==len(rows)


def test_shutdown_checkpoint_failure_is_visible_and_blocks_restore(tmp_path):
    async def run():
        class BrokenStore:
            def append(self,*args): raise OSError('disk full')
        registry=FakeRegistry({'x':spec('x')})
        ls=make_ls(tmp_path,registry=registry,allowed_real={'x'},store=BrokenStore())
        with patch.object(ai_step.hook_registry,'afire',side_effect=no_hook):
            await ai_step._handle_tool_calls(ls,ProviderResponse(ok=True,text='',tool_calls=[ToolCall(id='x',name='x',arguments={})]),0)
        result=cancelled_task_result(ls.rctx,'n')
        assert 'disk full' in result['checkpoint_error']
        with pytest.raises(ValueError,match='unconfirmed'):
            restore_runtime_state(SimpleNamespace(),result['_runtime_state'])
    asyncio.run(run())


def test_restart_before_initial_input_preserves_original_instruction(tmp_path, monkeypatch):
    from test_dynamic_history_foundation import run_task
    cap=run_task(tmp_path,monkeypatch,input_data={'instruction':'original work','resume_data':{'type':'restart_interrupted'}})
    assert cap['instruction']=='original work' and cap['resume_data'] is None
    assert cap['rctx']._restart_input_persisted is True
    assert any(m.content=='original work' for m in ConversationStore(tmp_path/'data/conversations').load('main'))


def test_accepted_restart_receipt_is_persisted_and_returns_cancelled_without_running_later_calls(tmp_path):
    async def run():
        async def restarting(args,ctx):
            ctx._restart_accepted=True
            return {'ok':True,'scheduled':True,'data':{'scheduled':True}}
        registry=FakeRegistry({'restart_control':spec('restart_control'),'later':spec('later')},{'restart_control':restarting})
        store=ConversationStore(tmp_path/'history')
        ls=make_ls(tmp_path,registry=registry,allowed_real={'restart_control','later'},allowed_pseudo={'finish'},store=store)
        with patch.object(ai_step.hook_registry,'afire',side_effect=no_hook):
            action=await ai_step._handle_tool_calls(ls,ProviderResponse(ok=True,text='',tool_calls=[
                ToolCall(id='a',name='restart_control',arguments={}),ToolCall(id='b',name='later',arguments={}),
                ToolCall(id='c',name='finish',arguments={'text':'premature'})]),0)
            assert action is not None and action.action=='cancelled'
            assert action.error=='engine graceful shutdown'
            assert registry.started==['restart_control']
            rows=store.load('session')
            tools=[m for m in rows if m.role=='tool']
            assert [m.tool_call_id for m in tools]==['a','b','c']
            assert tools[0].meta['tool_result_structured']['scheduled'] is True
            assert all(m.meta['tool_result_structured']['cancelled'] for m in tools[1:])
    asyncio.run(run())


def test_handle_task_cancellation_reports_runtime_not_only_terminal_error(tmp_path):
    async def run():
        entered=asyncio.Event(); posted=[]
        ctx=SimpleNamespace(completed_steps=4,tool_call_log=[{'name':'read_file'}],total_usage={'total_tokens':7},_restart_tool_attachments=[{'path':'result.png'}])
        async def node_run(**kwargs):
            current_context.set(ctx);entered.set();await asyncio.Event().wait()
        def request(req):
            import json
            posted.append(json.loads(req.content));return httpx.Response(200,json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
            with patch.object(runner,'_run_node_task',side_effect=node_run), patch.object(runner,'fetch_llm_secret',new=AsyncMock(return_value={})), patch.object(runner,'normalize_llm_secret',return_value=('','','')):
                task=asyncio.create_task(runner._handle_task(http,http,'http://sup',tmp_path,None,{'task_id':'t','session_id':'s','kind':'node','node_id':'n','session_generation':1},'w'))
                await entered.wait();task.cancel();await task
        result=posted[-1]['result']
        assert result['action']=='cancelled'
        assert result['_runtime_state']['completed_steps']==4
        assert result['_runtime_state']['tool_produced_attachments']==[{'path':'result.png'}]
    asyncio.run(run())


def test_worker_loop_manual_cancel_and_restart_immediately_interrupt_inflight_requests(tmp_path, monkeypatch):
    import json
    import time
    monkeypatch.delenv('CLONOTH_SUPERVISOR_URL', raising=False)

    async def run():
        # Part 1: manual cancel immediately interrupts an unending in-flight request
        in_request = asyncio.Event()
        trigger_cancel = asyncio.Event()
        completed_payloads = []
        claimed_once = False

        async def blocked_node_task(**kwargs):
            ctx = SimpleNamespace(completed_steps=2, tool_call_log=[], total_usage={},
                                  _restart_tool_attachments=[], emit_event=AsyncMock())
            current_context.set(ctx)
            in_request.set()
            await asyncio.Event().wait()  # Never finishes unless task.cancel() interrupts it

        async def handler_manual(req: httpx.Request) -> httpx.Response:
            nonlocal claimed_once
            path = req.url.path
            if path == '/v1/health':
                return httpx.Response(200, json={'ok': True})
            if path == '/v1/engine/register':
                return httpx.Response(200, json={'ok': True, 'orphans_cancelled': 0})
            if path == '/v1/remote/tools':
                return httpx.Response(200, json={'tools': []})
            if path == '/v1/tools/reload-seq':
                return httpx.Response(200, json={'seq': 0})
            if path == '/v1/tasks/next':
                if not claimed_once:
                    claimed_once = True
                    return httpx.Response(200, json={
                        'task_id': 'manual-t1', 'execution_id': 'exec-1', 'kind': 'node',
                        'node_id': 'n', 'session_id': 's1', 'session_generation': 1, 'input': {},
                    })
                return httpx.Response(204)
            if path == '/v1/async_tools/pending_cancels':
                if trigger_cancel.is_set():
                    trigger_cancel.clear()
                    return httpx.Response(200, json={'ok': True, 'async_ids': [], 'task_ids': ['manual-t1']})
                return httpx.Response(200, json={'ok': True, 'async_ids': [], 'task_ids': []})
            if path.endswith('/complete'):
                completed_payloads.append(json.loads(req.content))
                return httpx.Response(200, json={'ok': True})
            return httpx.Response(200, json={'ok': True})

        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            runner.httpx, 'AsyncClient',
            lambda **kw: real_async_client(transport=httpx.MockTransport(handler_manual), headers=kw.get('headers'), timeout=kw.get('timeout')),
        )
        monkeypatch.setattr(runner, '_run_node_task', blocked_node_task)
        monkeypatch.setattr(runner, 'fetch_llm_secret', AsyncMock(return_value={}))
        monkeypatch.setattr(runner, 'normalize_llm_secret', lambda _: ('', '', ''))
        monkeypatch.setattr(runner, 'load_runtime_config', lambda _: {'engine': {'poll_interval_sec': 0.1, 'max_workers': 2}})

        loop_task = asyncio.create_task(runner.worker_loop(supervisor_url='http://sup.local', workspace_root=tmp_path, worker_id='w1'))
        await asyncio.wait_for(in_request.wait(), timeout=2.0)
        t0 = time.monotonic()
        trigger_cancel.set()
        for _ in range(40):
            if completed_payloads:
                break
            await asyncio.sleep(0.02)
        elapsed_manual = time.monotonic() - t0
        assert completed_payloads, 'manual cancel did not complete task'
        assert elapsed_manual < 0.5
        assert completed_payloads[0]['result']['action'] == 'cancelled'
        assert completed_payloads[0]['result']['error'] == 'task cancelled'
        assert '_runtime_state' not in completed_payloads[0]['result']

        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

        # Part 2: engine restart interrupts BOTH a hung poll request AND an active in-flight task immediately
        in_request_2 = asyncio.Event()
        in_slow_poll = asyncio.Event()
        completed_restart = []
        claimed_2 = False

        async def blocked_node_task_2(**kwargs):
            ctx = SimpleNamespace(completed_steps=3, tool_call_log=[{'name': 'grep'}], total_usage={'total_tokens': 9},
                                  _restart_tool_attachments=[])
            current_context.set(ctx)
            in_request_2.set()
            await asyncio.Event().wait()

        async def handler_restart(req: httpx.Request) -> httpx.Response:
            nonlocal claimed_2
            path = req.url.path
            if path == '/v1/health':
                return httpx.Response(200, json={'ok': True})
            if path == '/v1/engine/register':
                return httpx.Response(200, json={'ok': True, 'orphans_cancelled': 0})
            if path == '/v1/remote/tools':
                return httpx.Response(200, json={'tools': []})
            if path == '/v1/tools/reload-seq':
                return httpx.Response(200, json={'seq': 0})
            if path == '/v1/async_tools/pending_cancels':
                return httpx.Response(200, json={'ok': True, 'async_ids': [], 'task_ids': []})
            if path == '/v1/tasks/next':
                if not claimed_2:
                    claimed_2 = True
                    return httpx.Response(200, json={
                        'task_id': 'restart-t2', 'execution_id': 'exec-2', 'kind': 'node',
                        'node_id': 'n', 'session_id': 's2', 'session_generation': 1, 'input': {},
                    })
                in_slow_poll.set()
                await asyncio.sleep(30.0)
                return httpx.Response(204)
            if path.endswith('/complete'):
                completed_restart.append(json.loads(req.content))
                return httpx.Response(200, json={'ok': True})
            return httpx.Response(200, json={'ok': True})

        monkeypatch.setattr(
            runner.httpx, 'AsyncClient',
            lambda **kw: real_async_client(transport=httpx.MockTransport(handler_restart), headers=kw.get('headers'), timeout=kw.get('timeout')),
        )
        monkeypatch.setattr(runner, '_run_node_task', blocked_node_task_2)

        import signal
        captured_stop = {}
        orig_get_loop = asyncio.get_running_loop

        class LoopProxy:
            def __init__(self, real):
                self._real = real
            def add_signal_handler(self, sig, cb, *args):
                if sig == signal.SIGTERM:
                    captured_stop['cb'] = cb
            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(runner.asyncio, 'get_running_loop', lambda: LoopProxy(orig_get_loop()))
        loop_task_2 = asyncio.create_task(runner.worker_loop(supervisor_url='http://sup.local', workspace_root=tmp_path, worker_id='w2'))
        await asyncio.wait_for(in_request_2.wait(), timeout=2.0)
        await asyncio.wait_for(in_slow_poll.wait(), timeout=2.0)

        t1 = time.monotonic()
        captured_stop['cb']()
        await asyncio.wait_for(loop_task_2, timeout=1.0)
        elapsed_restart = time.monotonic() - t1
        assert elapsed_restart < 0.5
        assert len(completed_restart) == 1
        assert completed_restart[0]['result']['error'] == 'engine graceful shutdown'
        assert completed_restart[0]['result']['_runtime_state']['completed_steps'] == 3

    asyncio.run(run())


def test_remote_tool_aborts_broker_call_immediately_on_task_cancellation(monkeypatch):
    from toolbox import remote_runtime

    async def run():
        in_poll = asyncio.Event()
        cancelled_calls = []

        async def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path == '/v1/remote/calls':
                return httpx.Response(200, json={'ok': True, 'call_id': 'rc-1'})
            if path == '/v1/remote/calls/rc-1/result':
                in_poll.set()
                await asyncio.sleep(30.0)
                return httpx.Response(202)
            if path == '/v1/remote/calls/rc-1/cancel':
                cancelled_calls.append(path)
                return httpx.Response(200, json={'call_id': 'rc-1', 'status': 'cancelled'})
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(remote_runtime, '_get_http_client', lambda timeout=360.0: client)
        try:
            ctx = SimpleNamespace(session_id='s', task_id='t')
            call_task = asyncio.create_task(remote_runtime.call_remote_tool(ctx, 'rmt_echo', {}, 60.0, 'http://sup.local'))
            await asyncio.wait_for(in_poll.wait(), timeout=2.0)
            call_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call_task, timeout=1.0)
            assert cancelled_calls == ['/v1/remote/calls/rc-1/cancel']
        finally:
            await client.aclose()

    asyncio.run(run())


def test_duplicate_manual_cancels_and_shutdown_do_not_abort_complete_post(tmp_path, monkeypatch):
    """Regression: WS push + pending_cancels poll + shutdown must not cancel in-flight /complete POST."""
    import json
    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config/runtime.yaml').write_text(
        'engine:\n  poll_interval_sec: 0.05\n  max_workers: 4\n',
        encoding='utf-8',
    )

    monkeypatch.delenv('CLONOTH_SUPERVISOR_URL', raising=False)

    async def run():
        in_task = asyncio.Event()
        in_complete_post = asyncio.Event()
        allow_complete_finish = asyncio.Event()
        completed_payloads = []
        dispatched = {'done': False}
        poll_cancel_sent = {'count': 0}

        async def slow_node(**kwargs):
            ctx = SimpleNamespace(completed_steps=1, tool_call_log=[], total_usage={},
                                  _restart_tool_attachments=[], emit_event=AsyncMock())
            current_context.set(ctx)
            in_task.set()
            await asyncio.sleep(30.0)
            return {'action': 'finish', 'result': {'text': 'never'}}

        monkeypatch.setattr(runner, '_run_node_task', slow_node)
        monkeypatch.setattr(runner, 'fetch_llm_secret', AsyncMock(return_value={}))
        monkeypatch.setattr(runner, 'normalize_llm_secret', lambda _: ('', '', ''))
        monkeypatch.setattr(runner.ToolRegistry, 'load_remote_tools', AsyncMock(return_value=0))

        async def sup_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path == '/v1/health':
                return httpx.Response(200, json={'ok': True})
            if path == '/v1/engine/register':
                return httpx.Response(200, json={'ok': True, 'orphans_cancelled': 0})
            if path == '/v1/async_tools/pending_cancels':
                if in_complete_post.is_set() and poll_cancel_sent['count'] < 2:
                    poll_cancel_sent['count'] += 1
                    return httpx.Response(200, json={'task_ids': ['dup-cancel-1'], 'async_ids': []})
                return httpx.Response(200, json={'task_ids': [], 'async_ids': []})
            if path == '/v1/tools/reload-seq':
                return httpx.Response(200, json={'seq': 0})
            if path == '/v1/internal/llm_secret':
                return httpx.Response(200, json={'api_key': 'k', 'base_url': 'http://llm', 'model': 'm'})
            if path == '/v1/tasks/next':
                if not dispatched['done']:
                    dispatched['done'] = True
                    return httpx.Response(200, json={
                        'task_id': 'dup-cancel-1', 'execution_id': 'ex-dup', 'kind': 'node',
                        'node_id': 'main', 'session_id': 'sid-dup', 'session_generation': 1, 'input': {},
                    })
                return httpx.Response(200, json={})
            if path == '/v1/tasks/dup-cancel-1/complete':
                in_complete_post.set()
                await allow_complete_finish.wait()
                completed_payloads.append(json.loads(req.content.decode('utf-8')))
                return httpx.Response(200, json={'ok': True})
            return httpx.Response(200, json={'ok': True})

        real_client_cls = httpx.AsyncClient
        monkeypatch.setattr(
            runner.httpx,
            'AsyncClient',
            lambda **kw: real_client_cls(transport=httpx.MockTransport(sup_handler), headers=kw.get('headers'), timeout=kw.get('timeout')),
        )

        ws_queue: asyncio.Queue = asyncio.Queue()

        class FakeWS:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await ws_queue.get()
                if item is None:
                    raise StopAsyncIteration
                return json.dumps(item)

        import sys
        monkeypatch.setitem(sys.modules, 'websockets', SimpleNamespace(connect=lambda *a, **kw: FakeWS()))

        import signal
        captured_stop = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            'add_signal_handler',
            lambda sig, cb, *args: captured_stop.__setitem__('cb', cb) if sig == signal.SIGTERM else None,
        )

        loop_task = asyncio.create_task(runner.worker_loop(supervisor_url='http://sup.local', workspace_root=tmp_path, worker_id='w-dup'))
        try:
            await asyncio.wait_for(in_task.wait(), timeout=2.0)

            # 1st cancel via WS push -> interrupts slow_node and enters /complete POST
            await ws_queue.put({'type': 'task_cancel_requested', 'payload': {'task_id': 'dup-cancel-1'}})
            await asyncio.wait_for(in_complete_post.wait(), timeout=2.0)

            # Fire duplicate WS cancel + pending_cancels poll + stop signal WHILE /complete is in flight!
            await ws_queue.put({'type': 'task_cancelled', 'payload': {'task_id': 'dup-cancel-1'}})
            await asyncio.sleep(0.12)
            captured_stop['cb']()
            await asyncio.sleep(0.05)
            allow_complete_finish.set()

            await asyncio.wait_for(loop_task, timeout=2.0)
            assert len(completed_payloads) == 1
            assert completed_payloads[0]['result']['action'] == 'cancelled'
            assert completed_payloads[0]['result']['error'] == 'task cancelled'
        finally:
            allow_complete_finish.set()
            if not loop_task.done():
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)

    asyncio.run(run())
