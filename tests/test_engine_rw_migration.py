"""Offline checks for runner history, callback presentation and r-w cleanup.

Run with python3.11 -B -m unittest discover -s tests -p 'test_engine_rw_migration.py'.
All writable fixtures live under data/temp; providers and Supervisor are mocked.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import runner, task_record, tool_step
from engine.context import RunContext
from engine.conversation_store import ConversationStore, Message
from engine.node import Node
from engine.protocol import TaskAction


def workspace(test):
    directory = ROOT / 'data/temp'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='rw-runner-', dir=directory)
    test.addCleanup(temporary.cleanup)
    return Path(temporary.name)


class CleanupTests(unittest.TestCase):
    def test_obsolete_modules_and_functions_are_removed(self):
        for name in ('stream_buffer.py', 'turn_summary.py'):
            self.assertFalse((ROOT / 'engine' / name).exists())
        self.assertTrue((ROOT / 'engine/inference/stream_buffer.py').is_file())
        self.assertTrue((ROOT / 'engine/builtin/turn_summary.py').is_file())
        for name in ('snip_history', 'snip_store'):
            self.assertFalse(hasattr(task_record, name))
        self.assertFalse(hasattr(tool_step, 'format_tool_trace'))
        self.assertFalse(hasattr(runner, '_strip_images_from_content'))

    def test_read_media_uses_media_budget_and_explicit_override_wins(self):
        self.assertEqual(tool_step.get_tool_inline_limit('read_media', {}), 8000)
        cfg = {'engine': {'tool_trace': {'max_inline_chars': {'default': 32000, 'media': 1200}}}}
        self.assertEqual(tool_step.get_tool_inline_limit('read_media', cfg), 1200)
        cfg['engine']['tool_trace']['max_inline_chars']['read_media'] = 2100
        self.assertEqual(tool_step.get_tool_inline_limit('read_media', cfg), 2100)
        self.assertEqual(tool_step.get_tool_inline_limit('unrelated', cfg), 32000)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_images_survive_all_task_contexts_without_changing_metadata(self):
        content = [{'type': 'text', 'text': 'describe'},
                   {'type': 'image_url', 'image_url': {'url': 'file://picture.png'}}]
        message = Message(id='image', role='user', content=content, source_task_id='old',
                          message_type='user_input', created_at='2026-11-01T00:00:00Z',
                          meta={'timestamp': '', 'other': 'retained'})
        before = copy.deepcopy(message)
        for current in ('', 'old', 'new'):
            with self.subTest(current=current):
                restored = runner._message_to_history_dict(message, current_task_id=current)
                self.assertEqual(restored['content'], content)
                self.assertEqual(restored['_meta']['source_task_id'], 'old')
                self.assertEqual(restored['_meta']['timestamp'], message.created_at)
                self.assertEqual(restored['_meta']['other'], 'retained')
                self.assertEqual(restored['message_type'], 'user_input')
        self.assertEqual(message, before)

    def test_malformed_metadata_and_native_pairs_remain_supported(self):
        message = Message(id='tool', role='tool', content='result', meta='invalid',
                          message_type='tool_result', source_task_id='task',
                          tool_call_id='call', name='example')
        restored = runner._message_to_history_dict(message)
        self.assertEqual(restored['tool_call_id'], 'call')
        self.assertEqual(restored['name'], 'example')
        self.assertEqual(restored['_meta']['source_task_id'], 'task')

    async def test_http_history_retains_multimodal_content(self):
        content = [{'type': 'text', 'text': 'past'},
                   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
        response = SimpleNamespace(status_code=200, json=lambda: [
            {'role': 'user', 'content': content, 'message_type': 'dispatch_result'},
            {'role': 'assistant', 'content': 'answer'},
        ])
        context = SimpleNamespace(http=SimpleNamespace(get=AsyncMock(return_value=response)),
                                  supervisor_url='https://supervisor.invalid', session_id='session')
        history = await runner._fetch_history(context)
        self.assertEqual(history[0]['content'], content)
        self.assertEqual(history[0]['message_type'], 'dispatch_result')
        self.assertEqual(history[1]['content'], 'answer')


class RunnerCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = workspace(self)

    async def run_node(self, kind, *, resume=False, raw='callback result', actions=None):
        node = Node(id='example', type='ai')
        captured = {}

        async def ai(**kwargs):
            captured.update(kwargs)
            kwargs['rctx'].tool_call_log = list(actions or [])
            return TaskAction(action='finish', node_id='example', result={'text': 'ok'})

        data = {'instruction': raw, 'use_context': False, 'inbound_message_type': kind}
        if kind == 'dispatch_result':
            data.update(inbound_child_node_id='child', inbound_child_task_id='child-task',
                        inbound_child_session_id='child-session')
        if resume:
            data['resume_data'] = {'kind': 'child_result'}
        config = {'provider': 'openai', 'openai': {'model': 'test-model', 'api_key': 'test-key'}}
        with patch.object(runner, 'load_node', return_value=node), \
                patch.object(runner, '_fetch_session_workspace', AsyncMock(return_value={})), \
                patch.object(runner, '_fetch_session_provider_override', AsyncMock(return_value={})), \
                patch.object(runner, 'load_runtime_config', return_value={}), \
                patch.object(runner, 'load_yaml_dict', return_value=config), \
                patch.object(runner, '_collect_node_info', return_value=[]), \
                patch.object(runner, '_discover_switchable_nodes', return_value=[]), \
                patch.object(runner, '_instantiate_provider', return_value=SimpleNamespace(_api_key='test-key')), \
                patch.object(runner, 'run_ai_node', side_effect=ai), \
                patch.object(runner, 'get_bus', return_value=SimpleNamespace(enabled=False)), \
                patch.object(runner, 'install_event_bridge'), \
                patch.object(RunContext, 'emit_event', AsyncMock()), \
                patch('engine.task_record.write_task_record'):
            result = await runner._run_node_task(
                http=object(), llm_http=object(), sup_url='https://supervisor.invalid',
                ws_root=self.root, registry=object(), task={'task_id': 'receiver', 'node_id': 'example', 'input': data},
                worker_id='worker', session_id='session', session_generation=1,
                api_key='test-key', base_url='', default_model='test-model',
            )
        return result, captured

    async def test_dispatch_model_input_is_labelled_but_stored_body_remains_raw(self):
        _, captured = await self.run_node('dispatch_result')
        text = captured['instruction']
        self.assertTrue(text.startswith('<dispatch_result kind="dispatch">\n'))
        self.assertEqual(json.loads(text.splitlines()[1]), {'source': 'child', 'task_id': 'child-task'})
        self.assertTrue(text.endswith('\ncallback result\n</dispatch_result>'))
        stored = ConversationStore(self.root / 'data/conversations').load('session')
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].content, 'callback result')
        self.assertEqual(stored[0].message_type, 'dispatch_result')
        self.assertEqual(stored[0].source_task_id, 'receiver')
        self.assertEqual(stored[0].meta['child_task_id'], 'child-task')

    async def test_async_tool_model_input_does_not_invent_a_child_task(self):
        _, captured = await self.run_node('async_tool_result')
        self.assertTrue(captured['instruction'].startswith('<dispatch_result kind="async_tool">\n{}\n'))
        stored = ConversationStore(self.root / 'data/conversations').load('session')
        self.assertEqual(stored[0].content, 'callback result')
        self.assertEqual(stored[0].message_type, 'async_tool_result')
        self.assertNotIn('child_task_id', stored[0].meta)

    async def test_regular_input_is_not_wrapped(self):
        _, captured = await self.run_node('')
        self.assertEqual(captured['instruction'], 'callback result')

    async def test_resume_does_not_add_an_empty_callback_message(self):
        _, captured = await self.run_node('dispatch_result', resume=True)
        self.assertEqual(captured['instruction'], '')
        self.assertEqual(ConversationStore(self.root / 'data/conversations').load('session'), [])

    async def test_recorded_actions_are_forwarded_without_old_name_aliases(self):
        result, _ = await self.run_node('', actions=[
            {'name': 'manage_memory', 'action': 'upsert'},
            {'name': 'read_file', 'args_summary': '{}'},
        ])
        self.assertEqual(result['_tool_actions'], [{'name': 'manage_memory', 'action': 'upsert'}])
        self.assertNotIn('_tool_names', result)


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_and_cancelled_tasks_release_metrics_and_clients(self):
        from engine import runtime_metrics
        for completes in (True, False):
            with self.subTest(completes=completes):
                root = workspace(self)
                count = 0
                handled = []
                metrics = SimpleNamespace(claimed=0, reaped=0, sample=Mock(), close=Mock())

                async def handle(*args):
                    handled.append(asyncio.current_task())
                    if not completes:
                        await asyncio.Event().wait()

                async def get(url, **kwargs):
                    nonlocal count
                    await asyncio.sleep(0)
                    if url.endswith('/next'):
                        count += 1
                        if count == 1:
                            return SimpleNamespace(status_code=200, json=lambda: {'task_id': 'task'})
                        if count == 2:
                            return SimpleNamespace(status_code=204, json=lambda: {})
                        raise asyncio.CancelledError()
                    if not completes and handled:
                        raise asyncio.CancelledError()
                    return SimpleNamespace(status_code=200, json=lambda: {'seq': 0})

                http = SimpleNamespace(get=get)
                clients = [AsyncMock(), AsyncMock()]
                for client in clients:
                    client.__aenter__.return_value = http
                    client.__aexit__.return_value = False
                registry = SimpleNamespace(load_remote_tools=AsyncMock(return_value=0))
                with patch.object(runner, 'load_runtime_config', return_value={'engine': {'poll_interval_sec': 0.1}}), \
                        patch.object(runner, 'ToolRegistry', return_value=registry), \
                        patch.object(runner, 'wait_supervisor', AsyncMock()), \
                        patch.object(runner, '_register_engine', AsyncMock()), \
                        patch.object(runner, '_handle_task', side_effect=handle), \
                        patch.object(runner.httpx, 'AsyncClient', side_effect=clients), \
                        patch.object(asyncio.get_running_loop(), 'add_signal_handler'), \
                        patch.object(runtime_metrics, 'WorkerMetrics', return_value=metrics), \
                        contextlib.redirect_stdout(io.StringIO()):
                    await runner.worker_loop(supervisor_url='https://supervisor.invalid', workspace_root=root)
                self.assertEqual(metrics.claimed, 1)
                self.assertEqual(metrics.reaped, 1 if completes else 0)
                metrics.close.assert_called_once_with()
                self.assertEqual(len(handled), 1)
                self.assertTrue(handled[0].done())
                self.assertEqual(handled[0].cancelled(), not completes)
                for client in clients:
                    client.__aexit__.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
