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


def test_accepted_restart_receipt_is_persisted_before_wait_and_later_calls_never_run(tmp_path):
    async def run():
        accepted=asyncio.Event()
        async def restarting(args,ctx):
            ctx._restart_accepted=True;accepted.set()
            return {'ok':True,'scheduled':True,'data':{'scheduled':True}}
        registry=FakeRegistry({'restart_control':spec('restart_control'),'later':spec('later')},{'restart_control':restarting})
        store=ConversationStore(tmp_path/'history')
        ls=make_ls(tmp_path,registry=registry,allowed_real={'restart_control','later'},allowed_pseudo={'finish'},store=store)
        with patch.object(ai_step.hook_registry,'afire',side_effect=no_hook):
            task=asyncio.create_task(ai_step._handle_tool_calls(ls,ProviderResponse(ok=True,text='',tool_calls=[
                ToolCall(id='a',name='restart_control',arguments={}),ToolCall(id='b',name='later',arguments={}),
                ToolCall(id='c',name='finish',arguments={'text':'premature'})]),0))
            await accepted.wait();await asyncio.sleep(0)
            assert not task.done() and registry.started==['restart_control']
            rows=store.load('session')
            tools=[m for m in rows if m.role=='tool']
            assert [m.tool_call_id for m in tools]==['a','b','c']
            assert tools[0].meta['tool_result_structured']['scheduled'] is True
            assert all(m.meta['tool_result_structured']['cancelled'] for m in tools[1:])
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
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
