"""Receipt admission and workspace confirmation checks; no model/network calls."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from engine.context import RunContext
from engine.conversation_store import ConversationStore
from engine.builtin.preempt import _inject_preempt_message
from toolbox.builtins.set_workspace import set_workspace


def response(status=200, **body):
    return httpx.Response(status, json=body, request=httpx.Request('POST', 'http://supervisor/test'))


def context(root, http):
    return RunContext(workspace_root=root, supervisor_url='http://supervisor', session_id='main',
                      worker_id='worker', http=http, llm_http=http, task_id='task', execution_id='claim')


def test_admission_requires_real_confirmation(tmp_path):
    async def run():
        http = SimpleNamespace(post=AsyncMock(return_value=response(503)))
        ctx = context(tmp_path, http)
        with pytest.raises(httpx.HTTPStatusError):
            await ctx.admit_input(['delivery'], 'message')
        http.post.return_value = response(ok=False)
        with pytest.raises(RuntimeError):
            await ctx.consume_preempt('result', 0, delivery_ids=['delivery'], message_id='message')
        http.post.return_value = response(ok=True)
        await ctx.admit_input(['delivery'], 'message')
        assert http.post.call_args.kwargs['json'] == {
            'delivery_ids': ['delivery'], 'message_id': 'message', 'execution_id': 'claim'}
        with pytest.raises(ValueError):
            await ctx.admit_input(['delivery'], '')
    asyncio.run(run())


def test_preempt_persists_identity_before_ack_and_deduplicates(tmp_path):
    async def run():
        store = ConversationStore(tmp_path / 'conversations')
        http = SimpleNamespace(post=AsyncMock(return_value=response(ok=True)))
        ctx = context(tmp_path, http)
        ctx.conversation_store = store
        ctx.emit_event = AsyncMock()
        info = {'message': 'a result', 'attachments': [], 'delivery_ids': ['delivery']}
        ls = SimpleNamespace(rctx=ctx, messages=[], preempt_inject_info=dict(info),
                             node=SimpleNamespace(id='entry'), runtime_cfg={}, history=[],
                             is_block_mode=False, system_prompt='')
        async def confirm(*args, **kwargs):
            row = store.load('main')[0]
            assert row.meta['delivery_ids'] == ['delivery']
            assert kwargs['json']['message_id'] == row.id
            return response(ok=True)
        http.post.side_effect = confirm
        with patch('engine.builtin.preempt.rebuild_dynamic_context'), \
             patch('engine.inference.ai_step._persist_dynamic_messages'):
            await _inject_preempt_message(SimpleNamespace(step=1), ls)
            ls.preempt_inject_info = dict(info)
            await _inject_preempt_message(SimpleNamespace(step=2), ls)
        assert len(store.load('main')) == len(ls.messages) == 1
    asyncio.run(run())


def tool_context(root, http):
    return SimpleNamespace(workspace_root=root, workspace=root / 'old', workspace_name='old',
                           supervisor_url='http://supervisor', http=http, route_session_id=lambda: 'main')


def test_workspace_failed_put_changes_neither_context_nor_registry(tmp_path):
    async def run():
        ctx = tool_context(tmp_path, SimpleNamespace(put=AsyncMock(return_value=response(500))))
        with patch('engine.workspace_registry.resolve_workspace') as save:
            result = await set_workspace({'name': 'new'}, ctx)
        assert result['ok'] is False
        assert ctx.workspace_name == 'old'
        assert ctx.workspace == tmp_path / 'old'
        save.assert_not_called()
    asyncio.run(run())


def test_workspace_success_then_registry_failure_reports_actual_session(tmp_path):
    async def run():
        ctx = tool_context(tmp_path, SimpleNamespace(put=AsyncMock(return_value=response(name='new'))))
        with patch('engine.workspace_registry.resolve_workspace', side_effect=OSError('disk full')):
            result = await set_workspace({'name': 'new'}, ctx)
        assert result['ok'] is True
        assert ctx.workspace_name == 'new' and ctx.workspace is None
        assert 'registry' in result['data']['warning']
    asyncio.run(run())
