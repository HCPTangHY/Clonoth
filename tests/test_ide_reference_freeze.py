"""Durable IDE snapshots through the real request-copy and JSONL paths."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import importlib
from unittest.mock import patch, Mock

import pytest

from engine.conversation_store import ConversationStore, Message
from engine.hooks.registry import HookRegistry
from engine.inference.llm_call import prepare_request_history
from engine.runner import _message_to_history_dict
import plugins.ide as ide


def setup_case(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    rctx = SimpleNamespace(workspace_root=tmp_path, workspace=tmp_path,
                           session_id='s', child_session_id='', conversation_store=store)
    return store, rctx


def add(store, ident, content):
    row = Message(id=ident, role='user', content=content, message_type='user_input', source_task_id=ident)
    store.append('s', row)
    return _message_to_history_dict(row)


async def request(messages, rctx):
    hooks = HookRegistry()
    hooks.register('before_llm_call', ide._expand_file_mentions)
    with patch('engine.inference.llm_call.hook_registry', hooks):
        result, action = await prepare_request_history(messages, node=None, provider=None, rctx=rctx)
    assert action is None
    return result


@pytest.mark.asyncio
async def test_snapshot_survives_copy_reload_plugin_reload_and_compaction(tmp_path):
    store, rctx = setup_case(tmp_path)
    file = tmp_path / 'code.py'
    file.write_text('old body')
    messages = [add(store, 'old', 'read @code.py')]
    initial = await request(messages, rctx)
    assert 'old body' in initial[0]['content']
    assert messages[0]['content'] == 'read @code.py'
    assert store.load('s')[0].content == 'read @code.py'
    file.write_text('new body')
    assert (await request(messages, rctx))[0]['content'] == initial[0]['content']
    importlib.reload(ide)
    reloaded = [_message_to_history_dict(m) for m in store.load('s')]
    assert (await request(reloaded, rctx))[0]['content'] == initial[0]['content']
    # The compactor replaces earlier segments and retains complete recent rows.
    kept = store.load('s')
    store.replace_all('s', [Message(id='summary', role='user', content='summary', message_type='summary'), *kept])
    compacted = [_message_to_history_dict(m) for m in store.load('s')]
    assert (await request(compacted, rctx))[1]['content'] == initial[0]['content']
    compacted.append(add(store, 'new', 'read @code.py'))
    result = await request(compacted, rctx)
    assert 'old body' in result[1]['content'] and 'new body' in result[2]['content']


@pytest.mark.asyncio
async def test_multimodal_and_missing_file_negative_snapshot(tmp_path):
    store, rctx = setup_case(tmp_path)
    content = [{'type': 'text', 'text': 'read @missing.py'},
               {'type': 'image_url', 'image_url': {'url': 'https://example.org/a.png'}}]
    msg = add(store, 'first', content)
    await request([msg], rctx)
    (tmp_path / 'missing.py').write_text('now exists')
    assert (await request([msg], rctx))[0]['content'] == content
    fresh = add(store, 'second', content)
    result = await request([fresh], rctx)
    assert 'now exists' in result[0]['content'][0]['text']
    assert result[0]['content'][1] == content[1]
    assert fresh['content'] == content


@pytest.mark.asyncio
async def test_traversal_and_symlink_outside_workspace_are_not_frozen(tmp_path):
    root = tmp_path / 'ws'; root.mkdir()
    secret = tmp_path / 'secret.py'; secret.write_text('OUTSIDE')
    (root / 'link.py').symlink_to(secret)
    store, rctx = setup_case(root)
    msg = add(store, 'one', 'read @../secret.py @link.py')
    result = await request([msg], rctx)
    assert 'OUTSIDE' not in result[0]['content']
    assert store.load('s')[0].meta['ide.reference_snapshot']['suffix'] == ''


@pytest.mark.asyncio
async def test_snapshots_keep_read_bounds_and_binary_filter(tmp_path):
    store, rctx = setup_case(tmp_path)
    (tmp_path / 'large.py').write_text('x' * (ide._MAX_EXPAND_CHARS + 100))
    (tmp_path / 'binary.py').write_bytes(b'\x00binary')
    msg = add(store, 'one', '@large.py @binary.py')
    result = await request([msg], rctx)
    suffix = store.load('s')[0].meta['ide.reference_snapshot']['suffix']
    assert '(truncated)' in suffix
    assert suffix.count('x') == ide._MAX_EXPAND_CHARS
    assert '[Referenced file: binary.py]' not in result[0]['content']


@pytest.mark.asyncio
async def test_atomic_failure_does_not_publish_metadata_or_change_body(tmp_path):
    store, rctx = setup_case(tmp_path)
    (tmp_path / 'code.py').write_text('body')
    msg = add(store, 'one', 'read @code.py')
    before = deepcopy(msg)
    disk = (tmp_path / 'conversations/s.jsonl').read_bytes()
    with patch('engine.conversation_store.os.replace', side_effect=OSError('write failure')):
        with pytest.raises(OSError):
            await request([msg], rctx)
    assert msg == before
    assert (tmp_path / 'conversations/s.jsonl').read_bytes() == disk


@pytest.mark.asyncio
async def test_same_mtime_new_input_sees_current_file_and_session_isolation(tmp_path):
    import os
    store, rctx = setup_case(tmp_path)
    file = tmp_path / 'code.py'; file.write_text('old')
    stat = file.stat()
    first = add(store, 'one', '@code.py')
    await request([first], rctx)
    file.write_text('new'); os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = add(store, 'two', '@code.py')
    assert 'new' in (await request([second], rctx))[0]['content']
    rctx.session_id = 'other'
    store.append('other', Message(id='one', role='user', content='@code.py', message_type='user_input'))
    other = [_message_to_history_dict(m) for m in store.load('other')]
    assert 'new' in (await request(other, rctx))[0]['content']


@pytest.mark.asyncio
async def test_snapshot_factory_failure_stops_request_without_partial_commit(tmp_path):
    store, rctx = setup_case(tmp_path)
    (tmp_path / 'code.py').write_text('body')
    msg = add(store, 'one', '@code.py')
    before = deepcopy(msg)
    with patch.object(ide, '_reference_suffix', side_effect=OSError('read failure')):
        with pytest.raises(RuntimeError, match='snapshot failed'):
            await request([msg], rctx)
    assert msg == before
    assert 'ide.reference_snapshot' not in store.load('s')[0].meta


@pytest.mark.asyncio
async def test_rendered_fork_prefix_is_not_expanded_twice(tmp_path):
    store, rctx = setup_case(tmp_path)
    (tmp_path / 'code.py').write_text('old')
    message = add(store, 'one', '@code.py')
    rendered = await request([message], rctx)
    (tmp_path / 'code.py').write_text('new')
    assert (await request(rendered, rctx))[0]['content'] == rendered[0]['content']


def test_runner_carries_durable_input_id_to_ai_entry(tmp_path, monkeypatch):
    from test_dynamic_history_foundation import run_task
    cap = run_task(tmp_path, monkeypatch, input_data={'instruction': '@code.py'})
    store = ConversationStore(tmp_path / 'data/conversations')
    row = store.load('main')[-1]
    assert cap['rctx'].inbound_message_id == row.id
    assert row.content == '@code.py'


@pytest.mark.asyncio
async def test_real_ai_assembly_binds_id_after_prompt_hooks(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from engine.inference import ai_step
    from engine.node import Node
    from engine.builtin import mcp
    from toolbox.registry import ToolRegistry
    store, rctx = setup_case(tmp_path)
    row = add(store, 'one', '@code.py')
    rctx.inbound_message_id = row['id']
    rctx.task_context = {}; rctx.workspace_name = ''
    monkeypatch.setattr(ai_step, 'auto_discover_and_register', Mock())
    monkeypatch.setattr(ai_step, 'load_external_plugins', Mock())
    monkeypatch.setattr(ai_step, 'load_runtime_config', lambda _: {})
    monkeypatch.setattr(mcp, 'wait_mcp_initial_discovery', AsyncMock())
    hooks = HookRegistry()
    def rebuild(ctx):
        # A real before_prompt_build contribution can replace the fresh skeleton.
        ctx.messages[:] = [{'role': 'system', 'content': 'SYS'},
                           {'role': 'user', 'content': '@code.py'}]
    hooks.register('before_prompt_build', rebuild)
    monkeypatch.setattr(ai_step, 'hook_registry', hooks)
    captured = {}
    class Stop(Exception): pass
    def capture(**kwargs):
        captured.update(kwargs); raise Stop()
    monkeypatch.setattr(ai_step, '_build_loop_state', capture)
    with pytest.raises(Stop):
        await ai_step.run_ai_node(
            rctx=rctx, node=Node(id='n', type='ai', prompt='SYS'),
            provider=SimpleNamespace(name='offline'),
            registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / 'tools'),
            instruction='@code.py', history=[],
        )
    assert captured['messages'][-1]['id'] == 'one'


@pytest.mark.asyncio
async def test_real_plugin_package_reload_keeps_durable_snapshot(tmp_path):
    import sys
    from engine.context import EngineContext
    from engine.hooks.loader import load_single_plugin, plugin_module_key, drop_plugin_module
    hooks = HookRegistry()
    context = EngineContext(hooks=hooks)
    plugins_dir = Path(__file__).resolve().parents[1] / 'plugins'
    store, rctx = setup_case(tmp_path)
    file = tmp_path / 'code.py'; file.write_text('before reload')
    message = add(store, 'one', '@code.py')
    try:
        load_single_plugin(hooks, plugins_dir, 'ide', context=context, process='engine')
        original_module = sys.modules[plugin_module_key('ide')]
        with patch('engine.inference.llm_call.hook_registry', hooks):
            first, _ = await prepare_request_history([message], node=None, provider=None, rctx=rctx)
        file.write_text('after reload')
        hooks.unload_plugin('ide')
        load_single_plugin(hooks, plugins_dir, 'ide', context=context, process='engine')
        assert sys.modules[plugin_module_key('ide')] is not original_module
        history = [_message_to_history_dict(m) for m in store.load('s')]
        with patch('engine.inference.llm_call.hook_registry', hooks):
            second, _ = await prepare_request_history(history, node=None, provider=None, rctx=rctx)
        assert second[0]['content'] == first[0]['content']
    finally:
        hooks.unload_plugin('ide')
        drop_plugin_module('ide')
