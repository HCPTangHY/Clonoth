import asyncio
import importlib
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

search = importlib.import_module('toolbox.builtins.search_in_files')
from engine.runner import _heartbeat


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_search_keeps_event_loop_responsive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.txt').write_text('hello banana')
            ctx = SimpleNamespace(workspace_root=root, workspace=None, effective_workspace=root)
            original = search._search_sync
            def slow(*args):
                time.sleep(0.15)
                return original(*args)
            async def allow(*args):
                return None
            with patch.object(search, '_search_sync', slow), patch.object(search, 'guard_external_read', allow):
                task = asyncio.create_task(search.search_in_files({'query': 'banana'}, ctx))
                await asyncio.sleep(0.03)
                self.assertFalse(task.done(), 'search blocked event loop')
                result = await task
            self.assertEqual(result['data']['count'], 1)

    async def test_replace_offloaded_after_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.txt').write_text('banana')
            ctx = SimpleNamespace(effective_workspace=root)
            original = search._replace_sync
            def slow(*args):
                time.sleep(0.15)
                return original(*args)
            async def allow(*args):
                return None, None
            import re
            with patch.object(search, '_replace_sync', slow), patch.object(search, 'request_guard', allow):
                task = asyncio.create_task(search._do_replace(root, re.compile('banana'), 'banana', 'apple', '*', 50, 2000000, ctx))
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                result = await task
            self.assertEqual(result['data']['filesModified'], 1)
            self.assertEqual((root / 'sample.txt').read_text(), 'apple')

    async def test_prunes_excluded_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'ignored.txt').write_text('banana')
            (root / 'keep.txt').write_text('banana')
            real_scandir = search.os.scandir
            def checked(path):
                self.assertNotIn('node_modules', Path(path).parts)
                return real_scandir(path)
            with patch.object(search.os, 'scandir', checked):
                files = list(search._collect_files(root, '*', 2000000, root))
            self.assertEqual([rel for _, rel in files], ['keep.txt'])

    async def test_heartbeat_logs_http_and_rejected_renewals(self):
        for status, body in ((500, {}), (200, {'ok': False})):
            async def handle(request):
                return httpx.Response(status, json=body)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertLogs('engine.runner', level=logging.WARNING) as logs:
                    task = asyncio.create_task(_heartbeat(client, 'http://test', 'task', 'worker', interval=0.01))
                    await asyncio.sleep(0.04)
                    task.cancel()
                    await task
                self.assertTrue(any('lease renewal' in message for message in logs.output))


if __name__ == '__main__':
    unittest.main()
