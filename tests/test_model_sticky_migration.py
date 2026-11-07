"""Offline checks for model options and production sticky-session transport.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests \
    -p 'test_model_sticky_migration.py' -v
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.context import RunContext
from engine.model import (
    ResolvedProvider,
    StickyHttpProxy,
    _current_sticky_sid_var,
    compute_sticky_ip,
    parse_sticky_session_config,
    resolve_provider,
    wrap_http_with_sticky_session,
)
from engine.node import Node


class StickyConfigTests(unittest.TestCase):
    def test_none_and_boolean_configuration(self):
        self.assertIsNone(parse_sticky_session_config(None))
        for enabled in (False, True):
            self.assertEqual(parse_sticky_session_config(enabled), {
                'enabled': enabled, 'header': 'X-Forwarded-For', 'ip_prefix': '10',
            })

    def test_dict_defaults_and_custom_configuration(self):
        self.assertEqual(parse_sticky_session_config({}), parse_sticky_session_config(True))
        config = {'enabled': False, 'header': ' X-Session ', 'ip_prefix': ' .172.16. '}
        before = dict(config)
        self.assertEqual(parse_sticky_session_config(config), {
            'enabled': False, 'header': 'X-Session', 'ip_prefix': '172.16',
        })
        self.assertEqual(config, before)
        self.assertEqual(parse_sticky_session_config({'header': ' ', 'ip_prefix': '.'}),
                         parse_sticky_session_config(True))

    def test_invalid_enabled_values_are_not_truthy(self):
        for enabled in ('false', 'true', '', 0, 1, None, []):
            with self.subTest(enabled=enabled), self.assertRaisesRegex(ValueError, 'enabled must be a boolean'):
                parse_sticky_session_config({'enabled': enabled})

    def test_invalid_top_level_configuration_is_rejected(self):
        for config in ('false', 'true', 0, 1, [], object()):
            with self.subTest(config=type(config).__name__), self.assertRaisesRegex(ValueError, 'sticky_session must be'):
                parse_sticky_session_config(config)

    def test_stable_ipv4_matches_production_values(self):
        self.assertEqual(compute_sticky_ip('session-a'), '10.211.203.201')
        self.assertEqual(compute_sticky_ip('session-b'), '10.148.170.177')
        self.assertEqual(compute_sticky_ip('session-a', '10.20'), '10.20.211.204')
        self.assertEqual(compute_sticky_ip('session-a', '172.16.3'), '172.16.3.212')
        self.assertEqual(compute_sticky_ip('session-a', '127.0.0.1'), '127.0.0.1')
        self.assertEqual(compute_sticky_ip('session-a', 'invalid.999'), '10.211.203.201')
        self.assertEqual(compute_sticky_ip('', ''), compute_sticky_ip('default', '10'))

    def test_final_octet_avoids_zero_and_broadcast(self):
        for byte, expected in ((0, 1), (254, 1), (255, 2)):
            with self.subTest(byte=byte), patch('engine.model.hashlib.md5') as md5:
                md5.return_value.digest.return_value = bytes([byte] * 16)
                self.assertEqual(compute_sticky_ip('session', '10.20.30'), f'10.20.30.{expected}')


class StickyProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_httpx_methods_send_the_header_using_one_client(self):
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(200, text='ok')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            proxy = wrap_http_with_sticky_session(client, True, 'session-a')
            self.assertIs(proxy._client, client)
            self.assertIs(proxy.headers, client.headers)
            await proxy.post('https://example.invalid/post', json={'value': 1})
            await proxy.get('https://example.invalid/get', params={'q': 'value'})
            await proxy.request('PATCH', 'https://example.invalid/request', content='payload')
            async with proxy.stream('GET', 'https://example.invalid/stream') as response:
                self.assertEqual(await response.aread(), b'ok')
            built = proxy.build_request('PUT', 'https://example.invalid/build', json={'built': True})
            await proxy.send(built)
            await proxy.send(httpx.Request('GET', 'https://example.invalid/send'))
            self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed)
        self.assertEqual([request.method for request in seen], ['POST', 'GET', 'PATCH', 'GET', 'PUT', 'GET'])
        self.assertTrue(all(request.headers['X-Forwarded-For'] == '10.211.203.201' for request in seen))
        self.assertEqual(seen[1].url.params['q'], 'value')
        self.assertEqual(seen[2].content, b'payload')

    async def test_forwarding_keeps_kwargs_and_input_headers_unchanged(self):
        client = SimpleNamespace(post=AsyncMock(return_value='post'), get=AsyncMock(return_value='get'),
                                 request=AsyncMock(return_value='request'))
        proxy = StickyHttpProxy(client, 'X-Session', 'stable')
        headers = {'Authorization': 'test-token'}
        before = dict(headers)
        self.assertEqual(await proxy.post('/post', headers=headers, json={'x': 1}, timeout=7), 'post')
        self.assertEqual(await proxy.get('/get', headers=headers, params={'x': 2}), 'get')
        self.assertEqual(await proxy.request('DELETE', '/request', headers=headers, follow_redirects=True), 'request')
        client.post.assert_awaited_once_with('/post', headers={**headers, 'X-Session': 'stable'}, json={'x': 1}, timeout=7)
        client.get.assert_awaited_once_with('/get', headers={**headers, 'X-Session': 'stable'}, params={'x': 2})
        client.request.assert_awaited_once_with('DELETE', '/request', headers={**headers, 'X-Session': 'stable'}, follow_redirects=True)
        self.assertEqual(headers, before)

    async def test_existing_header_is_preserved_case_insensitively(self):
        client = SimpleNamespace(post=AsyncMock(), send=AsyncMock())
        proxy = StickyHttpProxy(client, 'X-Forwarded-For', 'generated')
        headers = {'x-forwarded-for': 'caller', 'Other': 'unchanged'}
        await proxy.post('/post', headers=headers)
        self.assertEqual(client.post.call_args.kwargs['headers'], headers)
        self.assertNotIn('X-Forwarded-For', client.post.call_args.kwargs['headers'])
        request = SimpleNamespace(headers=dict(headers))
        await proxy.send(request, stream=True)
        self.assertEqual(request.headers, headers)
        client.send.assert_awaited_once_with(request, stream=True)

    async def test_build_and_stream_delegate_without_awaiting(self):
        marker = object()
        client = SimpleNamespace(stream=Mock(return_value=marker), build_request=Mock(return_value=marker))
        proxy = StickyHttpProxy(client, 'X-Session', 'stable')
        self.assertIs(proxy.stream('POST', '/stream', json={'x': 1}), marker)
        client.stream.assert_called_once_with('POST', '/stream', json={'x': 1}, headers={'X-Session': 'stable'})
        self.assertIs(proxy.build_request('GET', '/build', params={'x': 1}), marker)
        client.build_request.assert_called_once_with('GET', '/build', params={'x': 1}, headers={'X-Session': 'stable'})

    async def test_disabled_and_missing_session_leave_the_client_unchanged(self):
        client = object()
        for config in (None, False, {'enabled': False}):
            with self.subTest(config=config):
                self.assertIs(wrap_http_with_sticky_session(client, config, 'session'), client)
        self.assertIs(wrap_http_with_sticky_session(client, True, ''), client)
        self.assertIsNone(wrap_http_with_sticky_session(None, True, 'session'))

    async def test_rewrapping_uses_the_original_client_and_new_header(self):
        client = object()
        first = wrap_http_with_sticky_session(client, True, 'session-a')
        second = wrap_http_with_sticky_session(first, {'header': 'X-Session', 'ip_prefix': '172.16'}, 'session-b')
        self.assertIs(second._client, client)
        self.assertEqual(second._header_name, 'X-Session')
        self.assertEqual(second._header_value, compute_sticky_ip('session-b', '172.16'))
        self.assertEqual(first._header_value, '10.211.203.201')

    async def test_foreign_client_private_attribute_is_not_unwrapped(self):
        client = SimpleNamespace(_client=object(), post=AsyncMock(return_value='outer-client'))
        proxy = wrap_http_with_sticky_session(client, True, 'session-a')
        self.assertIs(proxy._client, client)
        self.assertEqual(await proxy.post('/request'), 'outer-client')
        client.post.assert_awaited_once()


class ContextStickyTests(unittest.TestCase):
    def setUp(self):
        token = _current_sticky_sid_var.set('prior-task')
        self.addCleanup(_current_sticky_sid_var.reset, token)

    def test_default_sid_priority_and_explicit_sid(self):
        for extra, expected in (
            ({}, 'session'),
            ({'child_session_id': 'child'}, 'child'),
            ({'parent_session_id': 'parent', 'child_session_id': 'child'}, 'parent'),
            ({'sticky_session_id': 'explicit', 'parent_session_id': 'parent'}, 'explicit'),
        ):
            with self.subTest(extra=extra):
                context = RunContext(workspace_root=ROOT, supervisor_url='https://supervisor.invalid',
                                     session_id='session', worker_id='worker', http=object(), llm_http=object(), **extra)
                self.assertEqual(context.sticky_session_id, expected)
                self.assertEqual(_current_sticky_sid_var.get(), expected)

    def test_empty_context_clears_a_previous_sid(self):
        RunContext(workspace_root=ROOT, supervisor_url='https://supervisor.invalid',
                   session_id='', worker_id='worker', http=object(), llm_http=object())
        self.assertEqual(_current_sticky_sid_var.get(), '')


class ProviderOptionTests(unittest.TestCase):
    def test_defaults_and_session_overrides_remain_separate(self):
        node_options = {'max_tokens': 200, 'thinking_config': {'thinking_budget': 100}}
        node = Node(id='node', type='ai', provider='openai', provider_options=node_options)
        config = {'openai': {
            'model': 'configured', 'base_url': 'https://configured.invalid', 'api_key': 'configured-key',
            'reasoning_field': 'reasoning', 'reasoning_effort': 'low', 'max_tokens': 100,
            'max_completion_tokens': 110, 'thought_summary': False,
            'thinking_config': {'thinking_budget': 50}, 'sticky_session': {'enabled': True},
        }}
        override = {
            'provider_options': {'max_tokens': 300, 'custom': {'values': [1]},
                                 'thinking_config': {'include_thoughts': False, 'thinking_budget': 120},
                                 'sticky_session': False},
            'reasoning_field': 'session_reasoning', 'reasoning_effort': 'high',
            'max_tokens': 400, 'max_completion_tokens': 410, 'thought_summary': True,
            'thinking_config': {'thinking_budget': 150},
        }
        inputs = copy.deepcopy((node_options, config, override))
        result = resolve_provider(ROOT, node, 'fallback', session_override=override, provider_configs=config)
        self.assertEqual(result.provider_defaults, {
            'reasoning_field': 'reasoning', 'reasoning_effort': 'low', 'max_tokens': 100,
            'max_completion_tokens': 110, 'thought_summary': False,
            'thinking_config': {'thinking_budget': 50}, 'sticky_session': {'enabled': True},
        })
        self.assertEqual(result.provider_options, {
            'max_tokens': 400, 'max_completion_tokens': 410, 'custom': {'values': [1]},
            'reasoning_field': 'session_reasoning', 'reasoning_effort': 'high', 'thought_summary': True,
            'thinking_config': {'include_thoughts': False, 'thinking_budget': 150}, 'sticky_session': False,
        })
        self.assertNotIn('_provider_sticky_session', result.provider_options)
        result.provider_options['custom']['values'].append(2)
        result.provider_defaults['thinking_config']['thinking_budget'] = 999
        result.provider_defaults['sticky_session']['enabled'] = False
        self.assertEqual((node_options, config, override), inputs)

    def test_credentials_keep_session_node_provider_default_priority(self):
        config = {'openai': {'model': 'configured', 'api_key': 'configured-key', 'base_url': 'https://configured.invalid'}}
        node = Node(id='node', type='ai', model='node-model', api_key='node-key', base_url='https://node.invalid')
        result = resolve_provider(ROOT, node, 'global-model', provider_configs=config)
        self.assertEqual((result.model, result.api_key, result.base_url), ('node-model', 'node-key', 'https://node.invalid'))
        result = resolve_provider(ROOT, node, 'global-model', provider_configs=config, session_override={
            'model': 'session-model', 'api_key': 'session-key', 'base_url': 'https://session.invalid',
        })
        self.assertEqual((result.model, result.api_key, result.base_url), ('session-model', 'session-key', 'https://session.invalid'))
        result = resolve_provider(ROOT, Node(id='node', type='ai'), 'global-model', provider_configs=config)
        self.assertEqual((result.model, result.api_key, result.base_url), ('configured', 'configured-key', 'https://configured.invalid'))

    def test_selected_gemini_channel_does_not_inherit_another_channel(self):
        config = {
            'gemini-vertex': {'model': 'vertex', 'api_key': 'vertex-key', 'base_url': 'https://vertex.invalid',
                              'max_tokens': 500, 'sticky_session': True},
            'gemini': {'model': 'legacy', 'api_key': 'legacy-key', 'sticky_session': True},
        }
        node = Node(id='node', type='ai', provider='gemini-aistudio')
        result = resolve_provider(ROOT, node, 'fallback', provider_configs=config)
        self.assertEqual(result.provider_type, 'gemini-aistudio')
        self.assertEqual(result.model, 'fallback')
        self.assertIsNone(result.api_key)
        self.assertIsNone(result.base_url)
        self.assertEqual(result.provider_defaults, {})
        config['gemini-aistudio'] = {'model': 'aistudio', 'api_key': 'aistudio-key', 'sticky_session': False}
        result = resolve_provider(ROOT, node, 'fallback', provider_configs=config)
        self.assertEqual(result.model, 'aistudio')
        self.assertEqual(result.api_key, 'aistudio-key')
        self.assertEqual(result.provider_defaults, {'sticky_session': False})

    def test_session_provider_selection_selects_its_own_defaults(self):
        node = Node(id='node', type='ai', provider='openai')
        config = {'openai': {'max_tokens': 100}, 'deepseek': {'model': 'chosen', 'max_tokens': 200}}
        result = resolve_provider(ROOT, node, 'fallback', provider_configs=config, session_override={'provider': 'deepseek'})
        self.assertEqual(result.provider_type, 'deepseek')
        self.assertEqual(result.model, 'chosen')
        self.assertEqual(result.provider_defaults, {'max_tokens': 200})
        self.assertEqual(result.provider_options, {})

    def test_resolution_never_reads_an_implicit_installation_config(self):
        with patch('clonoth_runtime.load_yaml_dict', side_effect=AssertionError('implicit file read')):
            result = resolve_provider(ROOT, Node(id='node', type='ai'), 'fallback')
        self.assertEqual(result.model, 'fallback')
        self.assertEqual(result.provider_defaults, {})

    def test_persistent_node_does_not_invent_a_sticky_sid_during_resolution(self):
        token = _current_sticky_sid_var.set('parent')
        try:
            result = resolve_provider(ROOT, Node(id='node', type='ai', persistent=True), 'fallback')
        finally:
            _current_sticky_sid_var.reset(token)
        self.assertNotIn('_sticky_session_id', result.provider_options)
        self.assertNotIn('_sticky_session_id', result.provider_defaults)

    def test_empty_option_fields_do_not_share_state(self):
        first, second = ResolvedProvider(model='first'), ResolvedProvider(model='second')
        first.provider_defaults['max_tokens'] = 1
        first.provider_options['custom'] = True
        self.assertEqual(second.provider_defaults, {})
        self.assertEqual(second.provider_options, {})

    def test_none_scalar_overrides_do_not_erase_nested_options(self):
        result = resolve_provider(ROOT, Node(id='node', type='ai'), 'fallback', session_override={
            'provider_options': {'max_tokens': 10, 'thinking_config': {'include_thoughts': True}},
            'max_tokens': None, 'thinking_config': {'thinking_budget': 20},
        })
        self.assertEqual(result.provider_options, {
            'max_tokens': 10, 'thinking_config': {'include_thoughts': True, 'thinking_budget': 20},
        })


if __name__ == '__main__':
    unittest.main()
