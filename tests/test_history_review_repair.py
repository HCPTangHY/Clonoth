"""Regression checks for input identity and durable preempt failure boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import asyncio

import pytest

from engine import runner
from engine.builtin.preempt import PreemptChecker
from engine.conversation_store import ConversationStore, Message
from engine.hooks.registry import HookRegistry
from engine.inference import ai_step
from engine.inference.llm_call import prepare_request_history
from engine.inference.message_assembly import assemble_messages_with_injections
from engine.node import Node
from toolbox.registry import ToolRegistry
import plugins.ide as ide
from test_dynamic_history_foundation import run_task


def configure_ai(monkeypatch, hooks):
    monkeypatch.setattr(ai_step, 'auto_discover_and_register', Mock())
    monkeypatch.setattr(ai_step, 'load_external_plugins', Mock())
    monkeypatch.setattr(ai_step, 'load_runtime_config', lambda _: {})
    monkeypatch.setattr(ai_step, 'hook_registry', hooks)
    monkeypatch.setattr('engine.builtin.mcp.wait_mcp_initial_discovery', AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_dynamic', [False, True])
async def test_real_afire_stops_provider_on_preempt_write_failure(tmp_path, monkeypatch, fail_dynamic):
    store = ConversationStore(tmp_path / 'conversations')
    rctx = SimpleNamespace(
        workspace_root=tmp_path, workspace_name='', session_id='s', child_session_id='',
        task_id='T', task_context={}, conversation_store=store,
        check_cancelled=AsyncMock(return_value=False), consume_preempt=AsyncMock(),
        emit_event=AsyncMock(),
    )
    ls = SimpleNamespace(
        messages=[], preempt_after_step=False, preempt_inject_info={'message': 'new input'},
        rctx=rctx, node=Node(id='n', type='ai', prompt='SYS'), runtime_cfg={}, history=[],
        is_block_mode=False, system_prompt=[], openai_tools=[], provider=SimpleNamespace(name='offline'),
    )
    hooks = HookRegistry()
    hooks.register('before_step', PreemptChecker())
    configure_ai(monkeypatch, hooks)
    monkeypatch.setattr(ai_step, '_build_loop_state', lambda **kw: ls)
    provider_call = AsyncMock()
    monkeypatch.setattr(ai_step, '_call_llm_with_retry', provider_call)
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / 'tools')
    if fail_dynamic:
        # Initial pass succeeds, but persistence after the injected input fails.
        monkeypatch.setattr(ai_step, '_persist_dynamic_messages', Mock(side_effect=[None, OSError('disk')]))
        failure = patch('engine.builtin.preempt.rebuild_dynamic_context', return_value=None)
    else:
        failure = patch.object(store, 'append', side_effect=OSError('disk'))
    with failure:
        result = await ai_step.run_ai_node(rctx=rctx, node=ls.node, provider=ls.provider,
                                          registry=registry, instruction='', history=[])
    assert result.action == 'fail'
    provider_call.assert_not_awaited()
    rctx.consume_preempt.assert_not_awaited()
    assert len(ls.messages) == (1 if fail_dynamic else 0)
    assert len(store.load('s')) == (1 if fail_dynamic else 0)


@pytest.mark.parametrize('kind', ['dispatch_result', 'async_tool_result'])
@pytest.mark.parametrize('attachment', [False, True])
def test_callback_first_real_ai_assembly_matches_reload_and_skips_ide(tmp_path, monkeypatch, kind, attachment):
    (tmp_path / 'code.py').write_text('MUST NOT EXPAND')
    data = {'instruction': 'result @code.py', 'inbound_message_type': kind,
            'inbound_child_task_id': 'child-task', 'inbound_child_node_id': 'child-node'}
    if attachment:
        (tmp_path / 'data').mkdir(exist_ok=True)
        from PIL import Image
        Image.new('RGB', (1, 1)).save(tmp_path / 'data/p.png')
        data['attachments'] = [{'path': 'data/p.png', 'type': 'image'}]
    cap = run_task(tmp_path, monkeypatch, input_data=data)
    captured = {}
    class Stop(Exception):
        pass
    def capture(**kwargs):
        captured.update(kwargs)
        raise Stop()
    hooks = HookRegistry()
    configure_ai(monkeypatch, hooks)
    monkeypatch.setattr(ai_step, '_build_loop_state', capture)
    async def check():
        with pytest.raises(Stop):
            await ai_step.run_ai_node(**cap)
        live = next(m for m in captured['messages'] if m.get('id') == cap['rctx'].inbound_message_id)
        stored = cap['rctx'].conversation_store.load('main')[-1]
        replay = runner._message_to_history_dict(stored)
        assert live == replay
        assert live['message_type'] == kind
        hooks.register('before_llm_call', ide._expand_file_mentions)
        with patch('engine.inference.llm_call.hook_registry', hooks):
            first, _ = await prepare_request_history([live], node=cap['node'], provider=cap['provider'], rctx=cap['rctx'])
            second, _ = await prepare_request_history([replay], node=cap['node'], provider=cap['provider'], rctx=cap['rctx'])
        assert first[0]['content'] == second[0]['content']
        assert 'MUST NOT EXPAND' not in str(first)
        assert 'ide.reference_snapshot' not in cap['rctx'].conversation_store.load('main')[-1].meta
    asyncio.run(check())


@pytest.mark.parametrize('block', [False, True])
def test_same_text_new_id_keeps_old_snapshot_and_reads_new_file(tmp_path, monkeypatch, block):
    store = ConversationStore(tmp_path / 'data/conversations')
    text = 'read @code.py'
    file = tmp_path / 'code.py'
    file.write_text('old version')
    old = Message(id='old-id', role='user', content=text, source_task_id='old-task', message_type='user_input')
    store.append('main', old)
    hooks = HookRegistry()
    hooks.register('before_llm_call', ide._expand_file_mentions)
    rctx = SimpleNamespace(workspace_root=tmp_path, workspace=tmp_path, session_id='main',
                           child_session_id='', conversation_store=store)
    async def render(messages, context):
        with patch('engine.inference.llm_call.hook_registry', hooks):
            return (await prepare_request_history(messages, node=None, provider=None, rctx=context))[0]
    original = asyncio.run(render([runner._message_to_history_dict(old)], rctx))
    file.write_text('new version')
    cap = run_task(tmp_path, monkeypatch, task_id='new-task', input_data={'instruction': text})
    current = cap['rctx'].inbound_message
    prompt = [{'role': 'system', 'content': 'SYS'}]
    if block:
        prompt.append({'role': 'history'})
    messages, _ = assemble_messages_with_injections(workspace_root=tmp_path, system_prompt=prompt,
        history=cap['history'], instruction=cap['instruction'], inbound_message=current)
    assert [m.get('id') for m in messages if m.get('role') == 'user'] == ['old-id', current['id']]
    rendered = asyncio.run(render(messages, cap['rctx']))
    assert rendered[1]['content'] == original[0]['content']
    assert 'new version' in rendered[2]['content']
    # Passing the same identity already in history does not append it twice.
    replay, _ = assemble_messages_with_injections(workspace_root=tmp_path, system_prompt=prompt,
        history=[current], instruction=text, inbound_message=current)
    assert sum(m.get('id') == current['id'] for m in replay) == 1


@pytest.mark.parametrize('child', [False, True])
@pytest.mark.parametrize('multimodal', [False, True])
@pytest.mark.parametrize('use_context', [False, True])
def test_retry_dedup_shared_for_child_multimodal_and_context_disabled(tmp_path, monkeypatch, child, multimodal, use_context):
    sid = 'child' if child else 'main'
    store = ConversationStore(tmp_path / 'data/conversations')
    content = [{'type': 'text', 'text': 'question'}, {'type': 'image_url', 'image_url': {'url': 'file://p.png'}}] if multimodal else 'question'
    store.append(sid, Message(id='parent', role='user', content='question', source_task_id='other'))
    store.append(sid, Message(id='previous', role='user', content=content, source_task_id='T', message_type='user_input'))
    store.append(sid, Message(id='delta', role='user', content='context', source_task_id='T', message_type='dynamic_context'))
    data = {'instruction': 'question', 'use_context': use_context}
    if child:
        data['child_session_id'] = sid
    if multimodal:
        (tmp_path / 'data').mkdir(exist_ok=True)
        from PIL import Image
        Image.new('RGB', (1, 1)).save(tmp_path / 'data/p.png')
        data['attachments'] = [{'path': 'data/p.png', 'type': 'image'}]
    cap = run_task(tmp_path, monkeypatch, input_data=data)
    rows = store.load(sid)
    assert [m.id for m in rows][:1] == ['parent']
    assert len(rows) == 2
    assert rows[-1].id == cap['rctx'].inbound_message_id
    assert not {'previous', 'delta'} & {m.get('id') for m in cap['history']}
    assert isinstance(rows[-1].content, list) == multimodal


def test_fork_retry_does_not_overwrite_existing_child_history(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'data/conversations')
    store.append('main', Message(id='parent', role='user', content='parent latest'))
    store.append('child', Message(id='child-history', role='assistant', content='child output', source_task_id='prior'))
    store.append('child', Message(id='old-attempt', role='user', content='q', source_task_id='T'))
    run_task(tmp_path, monkeypatch, input_data={'instruction': 'q', 'child_session_id': 'child',
        'context_mode': 'fork', 'fork_from_session_id': 'main'})
    assert [m.id for m in store.load('child')][:-1] == ['child-history']


def test_retry_delete_failure_preserves_input_and_never_enters_ai(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'data/conversations')
    store.append('main', Message(id='old', role='user', content='q', source_task_id='T'))
    before = (tmp_path / 'data/conversations/main.jsonl').read_bytes()
    with patch.object(ConversationStore, 'remove_messages', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            run_task(tmp_path, monkeypatch, input_data={'instruction': 'q'})
    assert (tmp_path / 'data/conversations/main.jsonl').read_bytes() == before
