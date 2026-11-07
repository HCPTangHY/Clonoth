"""Regression checks for the engine root a-c migration; no external services.

Run: python3.11 -B -m unittest discover -s tests -p 'test_engine_root_ac_migration.py' -v
"""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP = ROOT / 'data/temp'
sys.path.insert(0, str(ROOT))

from engine.builtin import preempt
from engine.compact import split_task_segments
from engine.context import RunContext
from engine.conversation_store import ConversationStore, Message


class InferenceLocationTests(unittest.TestCase):
    def test_root_shim_is_removed_and_has_no_source_imports(self):
        self.assertFalse((ROOT / 'engine/ai_step.py').exists())
        self.assertTrue((ROOT / 'engine/inference/ai_step.py').is_file())
        for folder in ('engine', 'supervisor', 'providers', 'tests'):
            for path in (ROOT / folder).rglob('*.py'):
                package = '.'.join(path.relative_to(ROOT).parts[:-1])
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if isinstance(node, ast.Import):
                        imports = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ''
                        if node.level:
                            module = resolve_name('.' * node.level + module, package)
                        imports = [module, *(module + '.' + alias.name for alias in node.names)]
                    else:
                        continue
                    self.assertNotIn('engine.ai_step', imports, f'{path}:{node.lineno}')


class StoreCacheLimitTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix='engine-ac-', dir=TEST_TEMP)
        self.addCleanup(directory.cleanup)
        self.store = ConversationStore(Path(directory.name))
        self.addCleanup(self.store.invalidate_cache, 'session')

    def test_default_cache_limit_matches_production(self):
        self.assertEqual(self.store._MAX_CACHEABLE_FILE_BYTES, 5 * 1024 * 1024)
        self.assertFalse(hasattr(self.store, '_MAX_LOAD_FILE_BYTES'))

    def test_small_history_is_cached_and_isolated(self):
        self.store.append('session', Message(id='first', role='user', content='original'))
        loaded = self.store.load('session')
        self.assertIn('session', self.store._cache)
        loaded[0].content = 'changed'
        self.assertEqual(self.store.load('session')[0].content, 'original')

    def test_oversized_history_is_complete_but_not_cached(self):
        rows = [
            Message(id='first', role='user', content='a' * 600),
            Message(id='last', role='assistant', content='b' * 600),
        ]
        self.store.append_batch('session', rows)
        with patch.object(ConversationStore, '_MAX_CACHEABLE_FILE_BYTES', 1024):
            loaded = self.store.load('session')
            self.assertEqual([row.id for row in loaded], ['first', 'last'])
            self.assertEqual([row.content for row in loaded], [row.content for row in rows])
            self.assertNotIn('session', self.store._cache)
            self.assertNotIn('session', self.store._cache_sigs)
            self.assertTrue(self.store.update_message('session', 'last', {'role': 'assistant', 'content': 'updated'}))
            self.assertEqual([row.content for row in self.store.load('session')], ['a' * 600, 'updated'])

    def test_external_growth_drops_the_previous_small_cache(self):
        self.store.append('session', Message(id='first', role='user', content='small'))
        with patch.object(ConversationStore, '_MAX_CACHEABLE_FILE_BYTES', 1024):
            self.store.load('session')
            self.assertIn('session', self.store._cache)
            # A second process does not invalidate this process's cache.
            with self.store._path('session').open('a', encoding='utf-8') as stream:
                stream.write(ConversationStore._serialize([
                    Message(id='external', role='user', content='x' * 1200),
                ]))
            self.assertEqual([row.id for row in self.store.load('session')], ['first', 'external'])
            self.assertNotIn('session', self.store._cache)
            self.assertNotIn('session', self.store._cache_sigs)


class PreemptAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_normalization_matches_production(self):
        http = SimpleNamespace(post=AsyncMock())
        context = RunContext(
            workspace_root=ROOT, supervisor_url='https://supervisor.invalid',
            session_id='session', worker_id='worker', http=http, llm_http=http, task_id='task',
        )
        for message, count, expected in (
            ('instruction', 2, {'consumed_message': 'instruction', 'consumed_attachment_count': 2}),
            ('', 0, {'consumed_message': '', 'consumed_attachment_count': 0}),
            (None, None, {'consumed_message': '', 'consumed_attachment_count': 0}),
            ('instruction', '2', {'consumed_message': 'instruction', 'consumed_attachment_count': 2}),
        ):
            with self.subTest(message=message, count=count):
                await context.consume_preempt(message, count)
                self.assertEqual(http.post.call_args.kwargs['json'], expected)


class PreemptSegmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TEST_TEMP.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def loop(root, store, messages, instruction, attachments=None):
        return SimpleNamespace(
            messages=messages, preempt_inject_info={'message': instruction, 'attachments': attachments or []},
            rctx=SimpleNamespace(
                workspace_root=root, session_id='session', task_id='task', child_session_id='',
                conversation_store=store, task_context={}, consume_preempt=AsyncMock(), emit_event=AsyncMock(),
            ),
            node=SimpleNamespace(id='node'), runtime_cfg={}, history=[],
            is_block_mode=False, system_prompt='',
        )

    async def test_live_and_stored_preempt_turns_have_identical_boundaries(self):
        for tagged in (False, True):
            with self.subTest(tagged=tagged), tempfile.TemporaryDirectory(prefix='engine-ac-', dir=TEST_TEMP) as directory:
                root = Path(directory)
                store = ConversationStore(root / 'conversations')
                self.addCleanup(store.invalidate_cache, 'session')
                messages = [
                    {'role': 'user', 'content': 'initial'},
                    {'role': 'assistant', 'content': 'first answer'},
                ]
                if tagged:
                    for message in messages:
                        message['_meta'] = {'source_task_id': 'task'}
                store.append_batch('session', [
                    Message(id=str(i), role=row['role'], content=row['content'], source_task_id='task')
                    for i, row in enumerate(messages)
                ])
                for index in range(2):
                    loop = self.loop(root, store, messages, f'follow-up {index}')
                    with patch.object(preempt, 'rebuild_dynamic_context'):
                        await preempt._inject_preempt_message(SimpleNamespace(step=index), loop)
                    self.assertTrue(messages[-1]['_meta']['preempt'])
                    self.assertEqual(messages[-1]['_meta']['message_type'], 'user_input')
                    loop.rctx.consume_preempt.assert_awaited_once_with(f'follow-up {index}', 0)
                    self.assertIsNone(loop.preempt_inject_info)
                    answer = {'role': 'assistant', 'content': f'answer {index}'}
                    if tagged:
                        answer['_meta'] = {'source_task_id': 'task'}
                    messages.append(answer)
                    store.append('session', Message(
                        id=f'answer-{index}', role='assistant', content=answer['content'], source_task_id='task',
                    ))
                _, live_segments = split_task_segments(messages)
                _, stored_segments = split_task_segments(store.load('session'))
                self.assertEqual(len(live_segments), 3)
                self.assertEqual(
                    [[row['content'] for row in segment] for segment in live_segments],
                    [[row.content for row in segment] for segment in stored_segments],
                )

    async def test_preempt_attachment_content_is_built_once(self):
        with tempfile.TemporaryDirectory(prefix='engine-ac-', dir=TEST_TEMP) as directory:
            root = Path(directory)
            store = ConversationStore(root / 'conversations')
            self.addCleanup(store.invalidate_cache, 'session')
            attachments = [{'path': 'data/attachments/image.png', 'type': 'image'}]
            content = [
                {'type': 'text', 'text': 'describe'},
                {'type': 'image_url', 'image_url': {'url': 'file://data/attachments/image.png'}},
            ]
            loop = self.loop(root, store, [], 'describe', attachments)
            with patch.object(preempt, 'rebuild_dynamic_context'), \
                    patch.object(preempt, 'build_multimodal_content', return_value=content) as build:
                await preempt._inject_preempt_message(SimpleNamespace(step=1), loop)
            build.assert_called_once_with('describe', attachments, workspace_root=root)
            stored = store.load('session')[0]
            self.assertEqual(loop.messages[0]['content'], stored.content)
            self.assertEqual(loop.messages[0]['_meta'], stored.meta)
            self.assertEqual(stored.message_type, 'user_input')
            loop.rctx.consume_preempt.assert_awaited_once_with('describe', 1)


if __name__ == '__main__':
    unittest.main()
