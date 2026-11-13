"""Cross-layer restart: real task store, ASGI, runner, inference, and durable history.
Only model responses and test-tool bodies are supplied locally; no live restart.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from engine import runner
from engine.hooks.types import HookResult
from engine.inference import ai_step
from engine.protocol import TaskAction
from providers.base import ProviderResponse, ToolCall
from supervisor import admin_api
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from test_engine_execution_migration import FakeRegistry, spec
from test_restart_supervisor import make_state


def app_for(state, root):
    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=root/'data/config.yaml'))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://restart.invalid',
                             headers={'Authorization': 'Bearer restart-test-token'})


@pytest.mark.parametrize('branch', [False, True])
def test_interrupted_tools_resume_same_task_and_history_without_restart_inbound(tmp_path, branch):
    async def run():
        state = make_state(tmp_path, 'before')
        (tmp_path/'config/nodes/main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\ntool_mode: native\ntool_access:\n  mode: all\n')
        sid = state.get_or_create_session(channel='web', conversation_key='web:restart')
        evt = state.eventlog.append(session_id=sid, component='test', type_='inbound_message',
                                   payload={'text': 'Keep original work', 'use_branch': branch})
        state.record_inbound_message_event(evt)
        item = state.assign_next_task(worker_id='worker')
        tid = item['task_id']
        entered = asyncio.Event()
        async def blocked(args, ctx):
            entered.set()
            await asyncio.Event().wait()
        registry = FakeRegistry({'done': spec('done'), 'slow': spec('slow')}, {'slow': blocked})
        response = ProviderResponse(ok=True, text='', tool_calls=[
            ToolCall(id='done-call', name='done', arguments={}),
            ToolCall(id='slow-call', name='slow', arguments={})])
        async def hooks(point, context):
            if point == 'before_response':
                return HookResult(action=TaskAction(action='finish', node_id='main', result={'text': 'Continued original work'}))
            return HookResult()
        with patch.object(admin_api, '_admin_token', 'restart-test-token'), patch.object(admin_api, '_web_auth', None), \
             patch.object(runner, 'fetch_llm_secret', new=AsyncMock(return_value={})), \
             patch.object(runner, 'normalize_llm_secret', return_value=('test-key', 'http://model.invalid/v1', 'test-model')), \
             patch.object(ai_step, 'auto_discover_and_register'), patch.object(ai_step, 'load_external_plugins'), \
             patch.object(ai_step.hook_registry, 'afire', side_effect=hooks), \
             patch.object(ai_step, '_call_llm_with_retry', new=AsyncMock(return_value=response)) as call:
            async with app_for(state, tmp_path) as http:
                running = asyncio.create_task(runner._handle_task(http, http, 'http://restart.invalid', tmp_path, registry, item, 'worker'))
                await asyncio.wait_for(entered.wait(), 5)
                state.restart_recovery.begin()
                running.cancel()
                await asyncio.wait_for(running, 5)
            assert state.tasks[tid].status == 'suspended'
            assert state.tasks[tid].cancel_reason == 'service_restart'
            assert not state.tasks[tid].cancel_requested
            state.restart_recovery.finalize()
            restored = make_state(tmp_path, 'after')
            next_item = restored.assign_next_task(worker_id='worker')
            assert next_item['task_id'] == tid
            assert next_item['source_inbound_seq'] == item['source_inbound_seq']
            before_count = call.await_count
            seen = []
            async def continue_model(ls, step):
                seen.extend(ls.messages)
                return ProviderResponse(ok=True, text='Continued original work')
            call.side_effect = continue_model
            async with app_for(restored, tmp_path) as http:
                await runner._handle_task(http, http, 'http://restart.invalid', tmp_path, registry, next_item, 'worker')
            assert restored.tasks[tid].status == 'completed', restored.tasks[tid].result
            assert restored.tasks[tid].completion_state == 'routed', restored.tasks[tid].model_dump()
            assert call.await_count == before_count + 1
            assert registry.started == ['done', 'slow']
            user_rows = [m for m in seen if m.get('role') == 'user' and m.get('content') == 'Keep original work']
            assert len(user_rows) == 1
            outputs = {m.get('tool_call_id'): m for m in seen if m.get('role') == 'tool'}
            assert outputs['done-call']['_meta']['tool_result_structured']['ok']
            assert outputs['slow-call']['_meta']['tool_result_structured']['cancelled']
            assert len(restored.tasks) == 1
            assert not [e for e in restored.eventlog.events if e['run_id'] == 'after' and e['type'] == 'inbound_message']
            outbound = [e for e in restored.eventlog.events if e['type'] == 'outbound_message']
            assert len(outbound) == 1 and outbound[0]['payload']['text'] == 'Continued original work'
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['tool', 'script'])
def test_non_ai_interruption_returns_cancel_to_original_caller_without_execution(tmp_path, kind):
    from supervisor.types import TaskKind, TaskStatus
    from test_restart_supervisor import task, complete

    async def run():
        state = make_state(tmp_path, 'before')
        sid = state.get_or_create_session(channel='web', conversation_key='web:script')
        parent = task(state, sid)
        parent.status = TaskStatus.suspended
        child = task(state, sid, caller_task_id=parent.task_id,
                     kind=TaskKind.tool if kind == 'tool' else TaskKind.node,
                     tool_name='write_file' if kind == 'tool' else None)
        state.assign_next_task(worker_id='worker')
        state.restart_recovery.begin()
        complete(state, child, 'cancelled')
        state.restart_recovery.finalize()
        restored = make_state(tmp_path, 'after')
        if kind == 'script':
            (tmp_path/'config/nodes/main.yaml').write_text('id: main\ntype: script\nscript: |\n  raise AssertionError("must not execute")\n')
        item = restored.assign_next_task(worker_id='worker')
        registry = FakeRegistry({'write_file': spec('write_file')})
        with patch.object(admin_api, '_admin_token', 'restart-test-token'), patch.object(admin_api, '_web_auth', None), \
             patch.object(runner, 'fetch_llm_secret', new=AsyncMock(return_value={})), \
             patch.object(runner, 'normalize_llm_secret', return_value=('', '', '')):
            async with app_for(restored, tmp_path) as http:
                await runner._handle_task(http, http, 'http://restart.invalid', tmp_path, registry, item, 'worker')
        assert restored.tasks[child.task_id].status == TaskStatus.cancelled
        assert restored.tasks[child.task_id].completion_state == 'routed'
        assert restored.tasks[parent.task_id].status == TaskStatus.pending
        assert registry.started == []
        resume = restored.tasks[parent.task_id].input['resume_data']
        assert 'cancelled' in str(resume).lower()
    asyncio.run(run())
