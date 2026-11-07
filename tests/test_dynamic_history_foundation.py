"""Offline checks for the durable dynamic-context foundation (not full activation)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from engine.conversation_store import ConversationStore, Message, MessageType, is_dynamic_context
from engine.inference.message_assembly import _estimate_history_prompt_tokens, conversational_history
from engine.protocol import TaskAction
from engine import runner
from toolbox.registry import ToolRegistry


def test_remove_streams_preserves_bytes_and_rollback(tmp_path):
    store = ConversationStore(tmp_path)
    store.append('s', Message(id='one', role='user', content='old'))
    store.append('s', Message(id='two', role='user', content='new'))
    path = tmp_path / 's.jsonl'
    with path.open('ab') as file:
        file.write(b'not-json\n{"id": [1]}\n')
    before = path.read_bytes()
    with patch('engine.conversation_store.os.replace', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            store.remove_messages('s', {'two'})
    assert path.read_bytes() == before
    assert store.remove_messages('s', {'two'}) == 1
    assert path.read_bytes() == b''.join(line for line in before.splitlines(keepends=True) if b'"two"' not in line)
    assert [m.id for m in store.load('s')][:1] == ['one']
    assert store.remove_messages('s', {'absent'}) == 0
    assert not list(tmp_path.glob('*.tmp'))
    assert not hasattr(store, '_MAX_LOAD_FILE_BYTES')


def test_dynamic_type_is_filtered_from_fork_and_keyword_scan(tmp_path):
    store = ConversationStore(tmp_path)
    dynamic = Message(id='d', role='user', content='context', message_type=MessageType.DYNAMIC_CONTEXT)
    store.append_batch('p', [Message(id='u', role='user', content='q'), dynamic])
    assert is_dynamic_context(dynamic)
    assert is_dynamic_context({'_meta': {'message_type': 'dynamic_context'}})
    assert store.fork('p', 'c') == 1
    history = [runner._message_to_history_dict(m) for m in store.load('p')]
    assert [m['id'] for m in conversational_history(history)] == ['u']


def test_callback_multimodal_roundtrip():
    original = [{'type': 'text', 'text': 'done\n[Attached images: p.png]'}, {'type': 'image_url', 'image_url': {'url': 'file://p.png'}}]
    msg = Message(id='cb', role='user', content=original, message_type='async_tool_result', meta={'child_task_id': 'ct'})
    replay = runner._message_to_history_dict(msg)['content']
    assert replay[0]['text'] == runner._wrap_dispatch_result_text('done', message_type='async_tool_result', task_id='ct') + '\n[Attached images: p.png]'
    assert replay[1] == original[1]
    assert msg.content == original
    msg.content = replay
    assert runner._message_to_history_dict(msg)['content'] == replay


def run_task(tmp_path, monkeypatch, *, input_data, task_id='T', session_id='main'):
    nodes = tmp_path / 'config' / 'nodes'
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / 'n.yaml').write_text('id: n\ntype: ai\nprompt: SYS\ntool_access:\n  mode: none\n')
    captured = {}
    async def fake(**kwargs):
        captured.update(kwargs)
        return TaskAction(action='finish', node_id='n', result={'text': 'ok'})
    monkeypatch.setattr(runner, 'run_ai_node', fake)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404, json={}))) as http:
            await runner._run_node_task(
                http=http, llm_http=http, sup_url='http://sup.invalid', ws_root=tmp_path,
                registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / 'tools'),
                task={'task_id': task_id, 'node_id': 'n', 'input': input_data}, worker_id='w',
                session_id=session_id, session_generation=1, api_key='test',
                base_url='http://llm.invalid/v1', default_model='test',
            )
    asyncio.run(run())
    return captured


@pytest.mark.parametrize('rtype', ['child_result', 'compact_done', 'output_rejected', 'tool_results'])
def test_child_resume_keeps_own_history(tmp_path, monkeypatch, rtype):
    store = ConversationStore(tmp_path / 'data' / 'conversations')
    store.append('child', Message(id='cu', role='user', content='child request', source_task_id='T'))
    store.append('main', Message(id='pu', role='user', content='parent request'))
    cap = run_task(tmp_path, monkeypatch, input_data={'instruction': 'child request', 'child_session_id': 'child', 'resume_data': {'type': rtype}})
    assert [m['id'] for m in cap['history']] == ['cu']
    assert cap['instruction'] == '' and cap['attachments'] is None
    assert len(store.load('child')) == 1


@pytest.mark.parametrize('owner', ['T', 'other'])
def test_retry_only_removes_same_task_tail(tmp_path, monkeypatch, owner):
    store = ConversationStore(tmp_path / 'data' / 'conversations')
    store.append('main', Message(id='u', role='user', content='request', source_task_id=owner))
    store.append('main', Message(id='d', role='user', content='context', source_task_id=owner, message_type='dynamic_context'))
    cap = run_task(tmp_path, monkeypatch, input_data={'instruction': 'request'})
    if owner == 'T':
        assert cap['history'] == []
        assert len(store.load('main')) == 1
    else:
        assert [m['id'] for m in cap['history']] == ['u', 'd']
        assert len(store.load('main')) == 3


def test_usage_estimate_uses_actual_usage_and_discards_stale():
    assert _estimate_history_prompt_tokens([]) == 0
    assert _estimate_history_prompt_tokens([{'role': 'assistant', 'content': 'ok', '_meta': {'usage': {'prompt_tokens': 50000, 'completion_tokens': 2}}}]) == 50002
    assert _estimate_history_prompt_tokens([{'role': 'user', 'content': 'x' * 20000}]) == 10000
