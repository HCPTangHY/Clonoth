"""Offline runner integration checks for the engine d-p migration.

Run: python3.11 -B -m unittest discover -s tests -p 'test_engine_dp_cleanup.py' -v
Temporary workspaces are created under data/temp, never the live data directories.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
from importlib.util import resolve_name
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import runner
from engine.builtin import fallback_provider
from engine.context import RunContext
from engine.model import ResolvedProvider, StickyHttpProxy, _current_sticky_sid_var, compute_sticky_ip
from engine.node import Node
from engine.protocol import TaskAction
from providers.base import ProviderResponse


def temporary_workspace(test):
    directory = ROOT / 'data/temp'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='engine-dp-', dir=directory)
    test.addCleanup(temporary.cleanup)
    return Path(temporary.name)


class LegacyCleanupTests(unittest.TestCase):
    def test_unused_root_memory_extractor_is_removed(self):
        self.assertFalse((ROOT / 'engine/memory_extract.py').exists())
        self.assertTrue((ROOT / 'engine/builtin/memory_extract.py').is_file())
        for folder in ('engine', 'supervisor', 'providers', 'tests'):
            for path in (ROOT / folder).rglob('*.py'):
                package = '.'.join(path.relative_to(ROOT).parts[:-1])
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ''
                        if node.level:
                            module = resolve_name('.' * node.level + module, package)
                        names = [module, *(module + '.' + alias.name for alias in node.names)]
                    else:
                        continue
                    self.assertFalse(any(name == 'engine.memory_extract' or name.startswith('engine.memory_extract.')
                                         for name in names), f'{path}:{node.lineno}')


class RunnerNodeTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_workspace(self)
        runner._NODE_INFO_CACHE.clear()
        self.addCleanup(runner._NODE_INFO_CACHE.clear)
        no_declarations = patch.object(runner._plugin_nodes, 'declared_node', return_value=None)
        no_declarations.start()
        self.addCleanup(no_declarations.stop)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('type: ai\ntool_mode: native\noutput_mode: hybrid\n' + content, encoding='utf-8')
        return path

    def test_cached_metadata_follows_local_user_builtin_priority(self):
        self.write('engine/builtin/nodes/example.yaml', 'name: builtin\n')
        user = self.write('config/nodes/example.yaml', 'name: user\n')
        local = self.write('config/nodes/example.local.yaml', 'name: local\n')
        self.assertEqual(runner._load_cached_node_info(self.root, 'example')['name'], 'local')
        stat = local.stat()
        local.write_text('name: revised\n', encoding='utf-8')
        os.utime(local, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual(runner._load_cached_node_info(self.root, 'example')['name'], 'revised')
        local.unlink()
        self.assertEqual(runner._load_cached_node_info(self.root, 'example')['name'], 'user')
        user.unlink()
        self.assertEqual(runner._load_cached_node_info(self.root, 'example')['name'], 'builtin')

    def test_switch_discovery_normalizes_local_ids_and_filters_one_way_nodes(self):
        self.write('config/nodes/example.yaml', 'name: original\n')
        self.write('config/nodes/example.local.yaml', 'name: local\n')
        self.write('config/nodes/oneway.yaml', 'control_tools: [finish]\n')
        self.write('config/nodes/disabled.yaml', 'switchable: false\n')
        self.write('config/nodes/system.internal.yaml', 'name: internal\n')
        self.write('config/nodes/broken.yaml', 'provider: missing-test-provider\n')
        with patch.object(runner._plugin_nodes, 'iter_declared_nodes', return_value=[]), \
                self.assertLogs('engine.runner', level='WARNING'):
            nodes = runner._discover_switchable_nodes(self.root, '')
        self.assertEqual(nodes, [{'id': 'example', 'name': 'local', 'description': 'local'}])

    def test_plugin_metadata_uses_declared_source_or_inline_version(self):
        path = self.write('plugin/example.yaml', 'name: plugin\n')
        declaration = {'id': 'example', 'name': 'declared', 'type': 'ai'}
        with patch.object(runner._plugin_nodes, 'declared_node', return_value=declaration), \
                patch.object(runner._plugin_nodes, 'declared_node_path', return_value=str(path)):
            self.assertEqual(runner._node_info_path_and_mtime(self.root, 'example'), (path, path.stat().st_mtime_ns))
            self.assertEqual(runner._load_cached_node_info(self.root, 'example')['name'], 'declared')
        with patch.object(runner._plugin_nodes, 'declared_node', return_value=declaration), \
                patch.object(runner._plugin_nodes, 'declared_node_path', return_value=''):
            self.assertEqual(runner._node_info_path_and_mtime(self.root, 'example'), (None, -1))


class ProviderConstructionTests(unittest.TestCase):
    def setUp(self):
        token = _current_sticky_sid_var.set('context-session')
        self.addCleanup(_current_sticky_sid_var.reset, token)

    def test_defaults_do_not_override_explicit_options_or_session(self):
        rp = ResolvedProvider(model='test-model', provider_defaults={
            'max_tokens': 100, 'sticky_session': True, 'thinking_config': {'budget': 10, 'enabled': True},
        }, provider_options={'reasoning_effort': 'high', 'thinking_config': {'budget': 30}})
        explicit = {'max_tokens': 200, 'sticky_session': False, 'thinking_config': {'enabled': False}}
        before = copy.deepcopy((rp.provider_defaults, rp.provider_options, explicit))
        client = object()
        result = runner._provider_init_kwargs(rp, provider_name='openai', llm_http=client,
                                              api_key='test-key', base_url='', provider_options=explicit)
        self.assertIs(result['http'], client)
        self.assertEqual(result['provider_options'], {
            'max_tokens': 200, 'sticky_session': False, 'thinking_config': {'budget': 30, 'enabled': False},
            'reasoning_effort': 'high',
        })
        self.assertEqual((rp.provider_defaults, rp.provider_options, explicit), before)

    def test_model_scoped_routing_matches_production(self):
        for model, sid, expected in (
            ('ordinary', 'parent', 'parent'),
            ('ordinary', 'child_example', 'node:ordinary'),
            ('gemini-3.8-flash', 'parent', 'node:gemini-3.8-flash'),
        ):
            with self.subTest(model=model, sid=sid):
                rp = ResolvedProvider(model=model, provider_defaults={'sticky_session': True})
                result = runner._provider_init_kwargs(rp, provider_name='openai', llm_http=object(),
                                                      api_key='test', base_url='', provider_options={'_sticky_session_id': sid})
                self.assertIsInstance(result['http'], StickyHttpProxy)
                self.assertEqual(result['http']._header_value, compute_sticky_ip(expected))

    def test_dispatch_origin_is_forwarded_without_mutating_input(self):
        data = {'task_context': {'channel': 'test'}, '_system_task': True,
                '_dispatch_origin': {'context_mode': 'fork', 'fork_snapshot_path': 'data/temp/fork.json'}}
        before = copy.deepcopy(data)
        ctx = runner._build_task_context(data)
        self.assertTrue(ctx['is_system_task'])
        self.assertEqual(ctx['dispatch_origin'], data['_dispatch_origin'])
        ctx['dispatch_origin']['context_mode'] = 'changed'
        self.assertEqual(data, before)


class RunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = temporary_workspace(self)
        self.sticky_token = _current_sticky_sid_var.set('prior-context')

    async def asyncTearDown(self):
        _current_sticky_sid_var.reset(self.sticky_token)

    async def run_node(self, *, node=None, input_data=None, session='branch_test', overrides=None, runtime_options=None):
        node = node or Node(id='example', type='ai')
        input_data = {'instruction': 'test', 'use_context': False, **(input_data or {})}
        overrides = overrides or {}
        client = object()
        captures = {}

        def instantiate(cls, kwargs):
            captures['kwargs'] = kwargs
            return SimpleNamespace(_api_key=kwargs['api_key'], name='openai', model=kwargs['model'])

        async def ai(**kwargs):
            captures['rctx'] = kwargs['rctx']
            captures['active_sid'] = _current_sticky_sid_var.get()
            return TaskAction(action='finish', node_id=node.id, result={'text': 'ok'})

        fetch_override = AsyncMock(side_effect=lambda http, url, sid: overrides.get(sid, {}))
        config = {'provider': 'openai', 'openai': {'model': 'test-model', 'api_key': 'test-key',
                                                  'sticky_session': True, 'max_tokens': 100}}
        with patch.object(runner, 'load_node', return_value=node), \
                patch.object(runner, '_fetch_session_workspace', AsyncMock(return_value={})), \
                patch.object(runner, '_fetch_session_provider_override', fetch_override), \
                patch.object(runner, 'load_runtime_config', return_value={'providers': {'openai': {'options': {
                    'max_tokens': 50, **(runtime_options or {}),
                }}}}), \
                patch.object(runner, 'load_yaml_dict', return_value=config), \
                patch.object(runner, '_collect_node_info', return_value=[]), \
                patch.object(runner, '_discover_switchable_nodes', return_value=[]), \
                patch.object(runner, '_instantiate_provider', side_effect=instantiate), \
                patch.object(runner, 'run_ai_node', side_effect=ai), \
                patch.object(runner, 'get_bus', return_value=SimpleNamespace(enabled=False)), \
                patch.object(runner, 'install_event_bridge'), \
                patch.object(RunContext, 'emit_event', AsyncMock()), \
                patch('engine.task_record.write_task_record'):
            result = await runner._run_node_task(
                http=object(), llm_http=client, sup_url='https://supervisor.invalid', ws_root=self.root,
                registry=object(), task={'task_id': 'test', 'node_id': node.id, 'input': input_data},
                worker_id='worker', session_id=session, session_generation=1,
                api_key='test-key', base_url='', default_model='default',
            )
        self.assertEqual(result['action'], 'finish')
        transport = captures['kwargs']['http']
        self.assertIs(transport._client if isinstance(transport, StickyHttpProxy) else transport, client)
        captures['fetch_override'] = fetch_override
        return captures

    async def test_durable_session_ids_follow_production_lifecycle(self):
        for node, data, session, expected in (
            (Node(id='example', type='ai'), {'parent_session_id': 'parent'}, 'branch_one', 'parent'),
            (Node(id='system.summary', type='ai'), {}, 'session', 'node:system.summary'),
            (Node(id='worker', type='ai'), {'child_session_id': 'child_fresh', 'context_mode': 'fresh'}, 'parent', 'node:worker'),
            (Node(id='worker', type='ai', persistent=True), {'child_session_id': 'child_saved', 'context_mode': 'accumulate',
                                                          'parent_session_id': 'parent', '_context_key': 'topic'}, 'branch', 'parent:worker:topic'),
        ):
            with self.subTest(expected=expected):
                captured = await self.run_node(node=node, input_data=data, session=session)
                self.assertEqual(captured['rctx'].sticky_session_id, expected)
                self.assertEqual(captured['active_sid'], expected)
                self.assertEqual(captured['kwargs']['http']._header_value, compute_sticky_ip(expected))

    async def test_fork_inherits_parent_options_and_snapshot_origin(self):
        origin = {'context_mode': 'fork', 'parent_session_id': 'parent', 'parent_sticky_session_id': 'warm-parent',
                  'fork_snapshot_path': 'data/temp/fork_snapshots/example.json'}
        captured = await self.run_node(
            node=Node(id='worker', type='ai', provider_options={'max_tokens': 200}),
            input_data={'child_session_id': 'child_fork', '_dispatch_origin': origin},
            overrides={'parent': {'model': 'parent-model', 'max_tokens': 300}},
        )
        self.assertEqual([call.args[2] for call in captured['fetch_override'].await_args_list], ['child_fork', 'parent'])
        self.assertEqual(captured['rctx'].task_context['dispatch_origin'], origin)
        self.assertEqual(captured['rctx'].sticky_session_id, 'warm-parent')
        self.assertEqual(captured['kwargs']['model'], 'parent-model')
        self.assertEqual(captured['kwargs']['provider_options']['max_tokens'], 300)

    async def test_node_options_override_provider_and_runtime_defaults(self):
        captured = await self.run_node(node=Node(id='example', type='ai', provider_options={'max_tokens': 200}))
        self.assertEqual(captured['kwargs']['provider_options']['max_tokens'], 200)

    async def test_explicit_sticky_settings_override_provider_defaults(self):
        for node_options, override, enabled in (
            ({}, {}, False),
            ({'sticky_session': True}, {}, True),
            ({'sticky_session': True}, {'provider_options': {'sticky_session': False}}, False),
        ):
            with self.subTest(node_options=node_options, override=override):
                captured = await self.run_node(
                    node=Node(id='example', type='ai', provider_options=node_options),
                    runtime_options={'sticky_session': False}, overrides={'branch_test': override},
                )
                self.assertEqual(isinstance(captured['kwargs']['http'], StickyHttpProxy), enabled)

    async def test_child_override_prevents_parent_override_lookup(self):
        captured = await self.run_node(input_data={'child_session_id': 'child_fork',
                                                  '_dispatch_origin': {'context_mode': 'fork', 'parent_session_id': 'parent'}},
                                       overrides={'child_fork': {'max_tokens': 400}, 'parent': {'max_tokens': 300}})
        self.assertEqual([call.args[2] for call in captured['fetch_override'].await_args_list], ['child_fork'])
        self.assertEqual(captured['kwargs']['provider_options']['max_tokens'], 400)

    async def test_handle_task_restores_context_on_success_error_and_cancel(self):
        for failure in (None, ValueError('fixture'), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                _current_sticky_sid_var.set('caller')

                async def execute(**kwargs):
                    self.assertEqual(_current_sticky_sid_var.get(), '')
                    _current_sticky_sid_var.set('task-session')
                    if failure is not None:
                        raise failure
                    return {'action': 'finish'}

                http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200)))
                with patch.object(runner, 'fetch_llm_secret', AsyncMock(return_value={})), \
                        patch.object(runner, '_run_node_task', side_effect=execute), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    await runner._handle_task(http, object(), 'https://supervisor.invalid', self.root, object(),
                                              {'kind': 'node', 'task_id': 'task', 'node_id': 'example'}, 'worker')
                self.assertEqual(_current_sticky_sid_var.get(), 'caller')
                http.post.assert_awaited_once()

    async def test_fallback_uses_sticky_transport_and_preserves_images(self):
        client = object()
        backup = SimpleNamespace(name='backup', model='backup-model')
        messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}]
        call = AsyncMock(return_value=ProviderResponse(ok=True, text='answer'))
        rctx = SimpleNamespace(workspace_root=self.root, sticky_session_id='warm-parent', llm_http=client,
                               task_id='task', emit_event=AsyncMock())
        ctx = SimpleNamespace(response=ProviderResponse(ok=False, status_code=503, error='unavailable'),
                              rctx=rctx, messages=messages, provider=SimpleNamespace(name='primary', model='primary-model'),
                              node=SimpleNamespace(id='example'), extra={'call_provider': call})
        config = {'fallbacks': [{'provider': 'openai', 'model': 'backup-model', 'base_url': 'https://backup.invalid',
                                 'api_key': 'test-key', 'sticky_session': True}]}
        with patch.object(fallback_provider, '_load_config', return_value=config), \
                patch.object(fallback_provider, '_create_fallback_provider', return_value=backup) as create:
            result = await fallback_provider.FallbackProviderHandler().handle(ctx)
        self.assertTrue(result.modified)
        proxy = create.call_args.kwargs['http']
        self.assertIs(proxy._client, client)
        self.assertEqual(proxy._header_value, compute_sticky_ip('warm-parent'))
        call.assert_awaited_once_with(backup, messages=messages)
        self.assertEqual(messages[0]['content'][0]['type'], 'image_url')


if __name__ == '__main__':
    unittest.main()
