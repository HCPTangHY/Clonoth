"""MCP bridge reentry is non-destructive; explicit reload/unload still works."""
import asyncio
import contextlib
from copy import deepcopy
import os
import subprocess
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.builtin import mcp
from engine.hooks.loader import register_meta_handler
from engine.hooks.registry import HookRegistry
from toolbox.registry import ToolRegistry
from test_mcp_audit_fixes import _Server, _EngineCtx, _ToolCallCtx, fake_sdk, write_config, wait_until


def registry_at(root):
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.workspace_root = root
    registry._tool_funcs = {}
    registry._tool_specs = {}
    registry._builtin_funcs = {}
    registry._builtin_specs = {}
    return registry


def load(registry, hooks):
    # Exercise the real common loader and teardown ledger, without unrelated tools.
    return register_meta_handler(hooks, mcp, {
        'handler_class': 'McpPlugin', 'hook_points': [], 'wants_context': True,
    }, ledger_name='mcp', context=_EngineCtx(registry, hooks))


def test_no_loop_does_not_create_runtime_state_or_teardown_task():
    with pytest.raises(RuntimeError):
        mcp.mcp_runtime.shared_state()
    plugin = mcp.McpPlugin()
    assert plugin.teardown() is None
    plugin._bridge_generation = 1
    assert plugin.teardown() is None


def test_runtime_state_is_isolated_between_event_loops():
    async def state():
        return mcp.mcp_runtime._runtime_state()
    first, second = asyncio.run(state()), asyncio.run(state())
    for key in ('bridge', 'stdio_actors', 'tool_schema_cache'):
        assert first[key] is not second[key]


def test_plugin_teardown_cannot_target_another_loop(tmp_path):
    write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
    async def start():
        plugin = load(registry_at(tmp_path), HookRegistry())
        assert await mcp.wait_mcp_initial_discovery()
        return plugin, mcp._bridge_state()['task']
    async def check_wrong_loop(plugin, own_task):
        assert plugin.teardown() is None
        assert mcp._bridge_state()['task'] is own_task
        assert not mcp._bridge_state()['stop'].is_set()
    async def stop_own_loop(plugin, own_task):
        assert not own_task.done()
        await plugin.teardown()
        assert own_task.done()
    with fake_sdk(_Server()), asyncio.Runner() as first_loop, asyncio.Runner() as second_loop:
        try:
            first, first_task = first_loop.run(start())
            second, second_task = second_loop.run(start())
            assert first._bridge_generation == second._bridge_generation
            second_loop.run(check_wrong_loop(first, second_task))
            first_loop.run(check_wrong_loop(second, first_task))
            first_loop.run(stop_own_loop(first, first_task))
            second_loop.run(stop_own_loop(second, second_task))
        finally:
            first_loop.run(mcp.shutdown_bridge())
            second_loop.run(mcp.shutdown_bridge())


def test_nonshared_plugin_instances_keep_independent_teardowns():
    resources = set()
    class Plugin:
        def __init__(self):
            resources.add(self)
        def teardown(self):
            resources.remove(self)
    hooks = HookRegistry()
    module = SimpleNamespace(Plugin=Plugin, __name__='test_plugin')
    for _ in range(2):
        register_meta_handler(hooks, module, {'handler_class': 'Plugin', 'hook_points': []}, ledger_name='private')
    assert len(resources) == 2
    hooks.unload_plugin('private')
    assert not resources


def test_node_reentry_preserves_approval_wait_and_inflight_stdio_call(tmp_path):
    async def run():
        write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
        entered, approved, finish_call = asyncio.Event(), asyncio.Event(), asyncio.Event()
        server = _Server(gates={'echo': finish_call})
        registry, hooks = registry_at(tmp_path), HookRegistry()
        async def guard(*args):
            entered.set()
            await approved.wait()
            return {}, None
        with fake_sdk(server), patch('toolbox._common.request_guard', side_effect=guard):
            try:
                load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                state = mcp._bridge_state()
                original_task = state['task']
                original_generation = state['generation']
                original_func = registry._tool_funcs['mcp_demo_echo']
                original_owned = hooks.owned('mcp')
                execution = asyncio.create_task(registry.execute(name='mcp_demo_echo', arguments={}, ctx=_ToolCallCtx()))
                await entered.wait()
                for _ in range(5):
                    load(registry, hooks)
                assert state['task'] is original_task
                assert state['generation'] == original_generation
                assert registry._tool_funcs['mcp_demo_echo'] is original_func
                assert hooks.owned('mcp') == original_owned
                approved.set()
                await asyncio.wait_for(server.started_event('echo').wait(), 2)
                starts, exits = server.process_starts, server.process_exits
                for _ in range(5):
                    load(registry, hooks)
                assert (server.process_starts, server.process_exits) == (starts, exits)
                assert not execution.done()
                finish_call.set()
                assert (await execution)['ok']
                assert server.called == ['echo']
                with patch.object(mcp, 'shutdown_bridge', wraps=mcp.shutdown_bridge) as shutdown:
                    hooks.unload_plugin('mcp')
                    assert await wait_until(lambda: mcp._bridge_state()['task'] is None and server.live_processes == 0)
                    shutdown.assert_awaited_once()
                assert not registry._tool_funcs
                assert hooks.owned('mcp') == 0
            finally:
                finish_call.set()
                await mcp.shutdown_bridge()
    asyncio.run(run())


def test_scheduled_old_teardown_cannot_close_new_bridge(tmp_path):
    async def run():
        write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
        server = _Server()
        registry, hooks = registry_at(tmp_path), HookRegistry()
        with fake_sdk(server):
            try:
                old = load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                old_task = mcp._bridge_state()['task']
                cleanup = old.teardown()  # Scheduled, but no await before new load.
                new = load(registry, hooks)
                new_task = mcp._bridge_state()['task']
                assert new_task is not old_task
                await cleanup
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                assert mcp._bridge_state()['task'] is new_task
                assert 'mcp_demo_echo' in registry._tool_funcs
                assert old.teardown() is None
                assert not new_task.done()
                await new.teardown()
                assert not registry._tool_funcs
            finally:
                await mcp.shutdown_bridge()
    asyncio.run(run())


def test_explicit_module_reload_replaces_bridge_on_same_registry(tmp_path):
    async def run():
        write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
        registry, hooks = registry_at(tmp_path), HookRegistry()
        with fake_sdk(_Server()):
            try:
                old = load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                old_task = mcp._bridge_state()['task']
                importlib.reload(mcp)
                load(registry, hooks)
                assert mcp._bridge_state()['task'] is not old_task
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                assert old_task.done()
                assert old.teardown() is None
                assert 'mcp_demo_echo' in registry._tool_funcs
                assert len([t for t in asyncio.all_tasks() if t.get_name() == 'mcp-bridge']) == 1
            finally:
                await mcp.shutdown_bridge()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['delete', 'disable', 'reconfigure'])
def test_client_config_change_does_not_interrupt_other_clients(tmp_path, change):
    async def run():
        clients = {name: {'transport': 'stdio', 'command': 'fake'} for name in ('demo', 'second')}
        write_config(tmp_path, clients)
        registry, hooks = registry_at(tmp_path), HookRegistry()
        server = _Server()
        entered, approved = asyncio.Event(), asyncio.Event()
        execution = None
        async def guard(*args):
            entered.set()
            await approved.wait()
            return {}, None
        with fake_sdk(server), patch('toolbox._common.request_guard', side_effect=guard):
            try:
                load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                second = registry._tool_funcs['mcp_second_echo']
                assert (await second({}, _ToolCallCtx()))['ok']
                actor = mcp.mcp_runtime.shared_state()['actors']['second']
                state = mcp._bridge_state()
                watcher = state['task']
                execution = asyncio.create_task(registry.execute(name='mcp_second_echo', arguments={}, ctx=_ToolCallCtx()))
                await asyncio.wait_for(entered.wait(), 2)
                if change == 'delete':
                    del clients['demo']
                elif change == 'disable':
                    clients['demo']['enabled'] = False
                else:
                    clients['demo']['command'] = 'different-fake'
                write_config(tmp_path, clients)
                await mcp.refresh_mcp_tools(registry)
                assert state['task'] is watcher
                assert mcp.mcp_runtime.shared_state()['actors']['second'] is actor
                assert actor.is_alive()
                assert registry._tool_funcs['mcp_second_echo'] is second
                approved.set()
                assert (await asyncio.wait_for(execution, 2))['ok']
                assert server.called == ['echo', 'echo']
                assert ('mcp_demo_echo' in registry._tool_funcs) == (change == 'reconfigure')
            finally:
                approved.set()
                if execution is not None:
                    await asyncio.gather(execution, return_exceptions=True)
                await mcp.shutdown_bridge()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['unchanged', 'order', 'schema_type', 'description', 'purity', 'config', 'implementation', 'remove'])
def test_refresh_preserves_only_unchanged_pending_approvals(tmp_path, change):
    async def run():
        clients = {'demo': {'transport': 'stdio', 'command': 'fake'}}
        write_config(tmp_path, clients)
        registry, hooks = registry_at(tmp_path), HookRegistry()
        server = _Server()
        tools = [
            {'name': 'echo', 'description': 'First tool', 'input_schema': {'type': 'object', 'properties': {'value': {'const': 1}}}},
            {'name': 'other', 'description': 'Second tool', 'input_schema': {'type': 'object'}},
        ]
        listing = AsyncMock(side_effect=lambda *args: {'ok': True, 'tools': deepcopy(tools)})
        entered, approved = asyncio.Event(), asyncio.Event()
        execution = None
        async def guard(*args):
            entered.set()
            await approved.wait()
            return {}, None
        with fake_sdk(server), patch.object(mcp.mcp_runtime, 'list_tools', new=listing), \
                patch('toolbox._common.request_guard', side_effect=guard), contextlib.ExitStack() as patches:
            try:
                load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                original = registry._tool_funcs['mcp_demo_echo']
                assert registry.get_spec('mcp_demo_echo')['description'] == '[MCP:demo] First tool'
                assert registry.get_spec('mcp_demo_other')['description'] == '[MCP:demo] Second tool'
                execution = asyncio.create_task(registry.execute(name='mcp_demo_echo', arguments={}, ctx=_ToolCallCtx()))
                await asyncio.wait_for(entered.wait(), 2)
                if change == 'order':
                    tools[:] = [dict(reversed(list(t.items()))) for t in reversed(tools)]
                elif change == 'schema_type':
                    tools[0]['input_schema']['properties']['value']['const'] = True
                elif change == 'description':
                    tools[0]['description'] = 'Changed'
                elif change == 'purity':
                    tools[0]['annotations'] = {'readOnlyHint': True}
                elif change == 'config':
                    clients['demo']['command'] = 'different-fake'
                    write_config(tmp_path, clients)
                elif change == 'implementation':
                    patches.enter_context(patch.object(mcp, '_make_mcp_tool', wraps=mcp._make_mcp_tool))
                elif change == 'remove':
                    del tools[0]
                await mcp.refresh_mcp_tools(registry)
                assert listing.await_count == 2, 'Identity reuse must not skip tool discovery'
                preserved = change in ('unchanged', 'order')
                assert (registry._tool_funcs.get('mcp_demo_echo') is original) == preserved
                approved.set()
                result = await asyncio.wait_for(execution, 2)
                if preserved:
                    assert result['ok'] and server.called == ['echo']
                else:
                    assert result['rejection_code'] == ('tool_unavailable' if change == 'remove' else 'tool_changed')
                    assert not server.called
            finally:
                approved.set()
                if execution is not None:
                    await asyncio.gather(execution, return_exceptions=True)
                await mcp.shutdown_bridge()
    asyncio.run(run())


def test_unchanged_refresh_retry_preserves_identity_and_dropped_client_rebuilds(tmp_path):
    async def run():
        clients = {'demo': {'transport': 'stdio', 'command': 'fake'}}
        write_config(tmp_path, clients)
        registry, hooks = registry_at(tmp_path), HookRegistry()
        with fake_sdk(_Server()):
            try:
                load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                original = registry._tool_funcs['mcp_demo_echo']
                owned = hooks.owned('mcp')
                with patch.object(mcp.mcp_runtime, 'list_tools', new=AsyncMock(side_effect=ValueError('offline'))):
                    await mcp.refresh_mcp_tools(registry)
                assert registry._tool_funcs['mcp_demo_echo'] is original
                assert mcp._bridge_state()['failed_clients'] == {'demo'}
                await mcp.refresh_mcp_tools(registry, retry_only=True)
                assert registry._tool_funcs['mcp_demo_echo'] is original
                assert not mcp._bridge_state()['failed_clients']
                assert hooks.owned('mcp') == owned
                write_config(tmp_path, {})
                await mcp.refresh_mcp_tools(registry)
                assert not mcp._bridge_state()['registration_signatures']
                write_config(tmp_path, clients)
                await mcp.refresh_mcp_tools(registry)
                assert registry._tool_funcs['mcp_demo_echo'] is not original
                assert hooks.owned('mcp') == owned
            finally:
                await mcp.shutdown_bridge()
    asyncio.run(run())


def test_same_definitions_are_registered_on_a_different_registry(tmp_path):
    async def run():
        write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
        first, second = registry_at(tmp_path), registry_at(tmp_path)
        with fake_sdk(_Server()):
            try:
                await mcp.refresh_mcp_tools(first)
                await mcp.refresh_mcp_tools(second)
                assert 'mcp_demo_echo' not in first._tool_funcs
                assert 'mcp_demo_echo' in second._tool_funcs
            finally:
                await mcp.shutdown_bridge()
    asyncio.run(run())


def test_full_package_reload_with_old_teardown_pending(tmp_path):
    # Isolate sys.modules deletion so other tests keep their original imports.
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([
        sys.executable, '-B', '-c',
        'import asyncio, sys; from pathlib import Path; '
        'from test_mcp_reentry import full_package_reload_case; '
        'asyncio.run(full_package_reload_case(Path(sys.argv[1])))', str(tmp_path),
    ], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


async def full_package_reload_case(root):
    from engine.builtin.loader import load_single_builtin
    write_config(root, {'demo': {'transport': 'stdio', 'command': 'fake'}})
    registry, hooks = registry_at(root), HookRegistry()
    server = _Server()
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = mcp._stop_bridge_task
    new_module = None
    async def delayed_stop(*args):
        entered.set()
        await release.wait()
        await original_stop(*args)
    with fake_sdk(server), contextlib.ExitStack() as patches:
        try:
            load(registry, hooks)
            assert await mcp.wait_mcp_initial_discovery(registry=registry)
            assert (await registry._tool_funcs['mcp_demo_echo']({}, _ToolCallCtx()))['ok']
            old_state = mcp._bridge_state()
            old_actor = old_state['actors']['demo']
            old_task = old_state['task']
            patches.enter_context(patch.object(mcp, '_stop_bridge_task', side_effect=delayed_stop))
            hooks.unload_plugin('mcp')
            await asyncio.wait_for(entered.wait(), 2)
            cleanups = [t for t in asyncio.all_tasks() if t.get_name() == 'mcp-bridge-teardown']
            assert len(cleanups) == 1
            load_single_builtin(hooks, 'mcp', context=_EngineCtx(registry, hooks), tool_registry=registry)
            new_module = importlib.import_module('engine.builtin.mcp')
            new_runtime = new_module.mcp_runtime
            assert new_runtime is not mcp.mcp_runtime
            # The newly imported runtime must also use the offline SDK.
            patches.enter_context(patch.multiple(new_runtime, **{
                key: getattr(mcp.mcp_runtime, key)
                for key in ('_MCP_IMPORT_ERROR', 'StdioServerParameters', 'stdio_client', 'ClientSession')
            }))
            assert new_runtime.shared_state() is old_state
            assert await new_module.wait_mcp_initial_discovery(registry=registry)
            new_func = registry._tool_funcs['mcp_demo_echo']
            assert not old_actor.is_alive()
            assert (await new_func({}, _ToolCallCtx()))['ok']
            release.set()
            await asyncio.gather(*cleanups)
            assert registry._tool_funcs['mcp_demo_echo'] is new_func
            assert old_task.done()
            assert len([t for t in asyncio.all_tasks() if t.get_name() == 'mcp-bridge']) == 1
            assert server.live_processes == 1
        finally:
            release.set()
            if new_module is not None:
                await new_module.shutdown_bridge()
            await mcp.shutdown_bridge()
        assert server.live_processes == 0


def test_started_teardown_finishes_before_replacement_discovery(tmp_path):
    async def run():
        write_config(tmp_path, {'demo': {'transport': 'stdio', 'command': 'fake'}})
        registry, hooks = registry_at(tmp_path), HookRegistry()
        entered, release = asyncio.Event(), asyncio.Event()
        close = mcp.mcp_runtime.close_stdio_actors
        async def delayed_close():
            entered.set()
            await release.wait()
            await close()
        with fake_sdk(_Server()):
            try:
                old = load(registry, hooks)
                assert await mcp.wait_mcp_initial_discovery(registry=registry)
                with patch.object(mcp.mcp_runtime, 'close_stdio_actors', side_effect=delayed_close):
                    cleanup = old.teardown()
                    await asyncio.wait_for(entered.wait(), 2)
                    load(registry, hooks)
                    new_task = mcp._bridge_state()['task']
                    release.set()
                    await cleanup
                    assert await mcp.wait_mcp_initial_discovery(registry=registry)
                    assert mcp._bridge_state()['task'] is new_task
                    assert 'mcp_demo_echo' in registry._tool_funcs
            finally:
                release.set()
                await mcp.shutdown_bridge()
    asyncio.run(run())
