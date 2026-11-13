"""Offline async registration/delivery checks; no tool or network side effects.

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest -p no:cacheprovider tests/test_async_tool_lifecycle.py
"""
from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from engine.builtin import async_scheduler as scheduler
from engine.inference import async_tools as lifecycle
from toolbox.context import ToolContext


class HTTP:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    async def post(self, url, *, json, timeout):
        payload = copy.deepcopy(json)
        self.calls.append((url, payload))
        if self.handler:
            value = await self.handler(url, payload)
            if isinstance(value, Exception):
                raise value
            code, body = value
        elif url.endswith('/async_tools'):
            code, body = 200, {'ok': True, 'async_id': payload['async_id'], 'boot_id': 'boot', 'target_generation': 1}
        else:
            code, body = 200, {'ok': True}
        return httpx.Response(code, json=body, request=httpx.Request('POST', url))


class Registry:
    def __init__(self, execute=None, denied=None):
        self.handler = execute
        self.denied = denied
        self.executions = 0
        self.authorizations = 0

    async def authorize(self, **kwargs):
        self.authorizations += 1
        return self.denied

    async def execute(self, **kwargs):
        self.executions += 1
        if self.handler:
            return await self.handler(**kwargs)
        return {'ok': True, 'data': {'result': 'output'}}

    def get_spec(self, name):
        return {'name': name}


def context(root, http, registry, name='generate_image', declared=True):
    tool = ToolContext(supervisor_url='https://supervisor', session_id='session', run_id='run',
                       worker_id='worker', workspace_root=root, http=http, registry=registry, task_id='task')
    rctx = SimpleNamespace(http=http, supervisor_url=tool.supervisor_url, task_id='task',
                           session_id='session', parent_session_id='', worker_id='worker', execution_id='claim')
    ls = SimpleNamespace(rctx=rctx, registry=registry, node=SimpleNamespace(id='main'),
                         runtime_cfg={'meta': {'execute_command': {'async_upgrade': {'threshold_sec': 0.01}}}})
    return SimpleNamespace(extra={'loop_state': ls, 'tool_ctx': tool, 'tool_name': name,
                                  'tool_args': {'timeout_sec': 90}, 'spec': {'async_mode': declared},
                                  'call_index': 0, 't0': time.monotonic()},
                           step=1, tool_call=SimpleNamespace(id='call'))


async def drain_background():
    for _ in range(100):
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                 and t.get_name().startswith(('async_tool_', 'async_upgrade_'))]
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError('background delivery did not settle')


class AsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        lifecycle._async_tool_tasks.clear()
        lifecycle._CANCELLED_ASYNC_IDS.clear()

    async def asyncTearDown(self):
        await drain_background()
        self.temp.cleanup()

    async def test_registration_ack_precedes_fast_execution(self):
        order = []
        registered = asyncio.Event()
        release = asyncio.Event()

        async def handle(url, payload):
            if url.endswith('/async_tools'):
                order.append('register')
                registered.set()
                await release.wait()
                order.append('ack')
                return 200, {'ok': True, 'target_generation': 1, 'boot_id': 'boot'}
            order.append('finish' if url.endswith('/finish') else 'deliver')
            return 200, {'ok': True}

        async def execute(**kwargs):
            order.append('execute')
            self.assertTrue(kwargs['_authorized'])
            return {'ok': True, 'data': {'result': 'output'}}

        http, registry = HTTP(handle), Registry(execute)
        running = asyncio.create_task(scheduler.AsyncScheduler().handle(context(self.root, http, registry)))
        await registered.wait()
        self.assertEqual(registry.executions, 0)
        release.set()
        result = await running
        aid = result.channels['execution']['async_id']
        self.assertEqual(len(aid), 32)
        await drain_background()
        self.assertEqual(order, ['register', 'ack', 'execute', 'finish', 'deliver'])
        self.assertEqual(registry.authorizations, 1)
        delivered = http.calls[-1][1]
        self.assertEqual(delivered['execution_id'], 'claim')
        self.assertEqual(delivered['boot_id'], 'boot')
        self.assertEqual(delivered['delivery_id'], aid + ':result')
        self.assertEqual(lifecycle._async_tool_tasks[aid]['delivery_status'], 'queued')

    async def test_denied_approval_does_not_register_or_leave_running(self):
        http = HTTP()
        registry = Registry(denied={'ok': False, 'error': 'denied'})
        result = await scheduler.AsyncScheduler().handle(context(self.root, http, registry))
        self.assertFalse(result.channels['execution']['ok'])
        self.assertEqual(http.calls, [])
        self.assertEqual(registry.executions, 0)
        self.assertEqual(lifecycle._async_tool_tasks, {})

    async def test_failed_registration_does_not_execute_declared_tool(self):
        async def reject(url, payload):
            return 403, {'ok': False}

        http, registry = HTTP(reject), Registry()
        result = await scheduler.AsyncScheduler().handle(context(self.root, http, registry))
        self.assertFalse(result.channels['execution']['ok'])
        self.assertEqual(registry.executions, 0)
        self.assertTrue(all(e['status'] == 'registration_failed' for e in lifecycle._async_tool_tasks.values()))
        ids = {payload['async_id'] for _, payload in http.calls}
        self.assertEqual(len(ids), 1)

    async def test_upgrade_registration_failure_keeps_original_command(self):
        async def reject(url, payload):
            return 403, {'ok': False}

        async def execute(**kwargs):
            await asyncio.sleep(0.03)
            return {'ok': True, 'data': {'result': 'single result'}}

        http, registry = HTTP(reject), Registry(execute)
        output = await scheduler.AsyncScheduler().handle(context(self.root, http, registry, 'execute_command', False))
        result = await output.channels['execution']
        self.assertEqual(result['data']['result'], 'single result')
        self.assertEqual(registry.executions, 1)
        self.assertFalse(result.get('async_started'))
        self.assertTrue(all(url.endswith('/async_tools') for url, _ in http.calls))

    async def test_uncertain_upgrade_is_reconciled_inline_with_same_id(self):
        registrations = 0

        async def handle(url, payload):
            nonlocal registrations
            if url.endswith('/async_tools'):
                registrations += 1
                if registrations <= 3:
                    return httpx.ReadTimeout('ack lost')
                return 200, {'ok': True, 'target_generation': 1, 'boot_id': 'boot'}
            return 200, {'ok': True}

        async def execute(**kwargs):
            await asyncio.sleep(0.03)
            return {'ok': True, 'data': {'result': 'single result'}}

        http, registry = HTTP(handle), Registry(execute)
        output = await scheduler.AsyncScheduler().handle(context(self.root, http, registry, 'execute_command', False))
        result = await output.channels['execution']
        self.assertEqual(registry.executions, 1)
        self.assertEqual(result['data']['result'], 'single result')
        ids = {p['async_id'] for u, p in http.calls if u.endswith('/async_tools')}
        self.assertEqual(len(ids), 1)
        self.assertTrue(http.calls[-1][0].endswith('/finish'))
        self.assertTrue(http.calls[-1][1]['delivered_inline'])
        self.assertEqual(http.calls[-1][1]['carrier_task_id'], 'task')
        self.assertFalse(any(u.endswith('/async_tool_result') for u, _ in http.calls))

    async def test_successful_upgrade_executes_once_and_delivers_same_result(self):
        async def execute(**kwargs):
            await asyncio.sleep(0.04)
            return {'ok': True, 'data': {'result': 'single upgrade result'}}

        http, registry = HTTP(), Registry(execute)
        output = await scheduler.AsyncScheduler().handle(context(self.root, http, registry, 'execute_command', False))
        marker = await output.channels['execution']
        self.assertTrue(marker['async_started'])
        await drain_background()
        self.assertEqual(registry.executions, 1)
        self.assertEqual(registry.authorizations, 1)
        self.assertIn('single upgrade result', http.calls[-1][1]['message'])
        self.assertEqual(http.calls[0][1]['async_id'], marker['async_id'])

    async def test_cancellation_during_adaptive_wait_cancels_original_execution(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        http, registry = HTTP(), Registry(execute)
        ctx = context(self.root, http, registry, 'execute_command', False)
        ctx.extra['loop_state'].runtime_cfg['meta']['execute_command']['async_upgrade']['threshold_sec'] = 60
        output = await scheduler.AsyncScheduler().handle(ctx)
        running = asyncio.create_task(output.channels['execution'])
        await started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(cancelled.is_set())
        self.assertEqual(http.calls, [])

    def delivery_kwargs(self, http):
        return dict(registry=Registry(), http=http, supervisor_url='https://supervisor', task_id='task',
                    session_id='session', tool_name='generate_image', tool_args={},
                    tool_ctx=SimpleNamespace(workspace_root=self.root), async_tool_id='work',
                    started_at=time.monotonic(), registration={'execution_id': 'claim', 'boot_id': 'boot', 'target_generation': 1})

    async def test_delivery_retries_same_payload_without_another_finish(self):
        deliveries = 0

        async def handle(url, payload):
            nonlocal deliveries
            if url.endswith('/async_tool_result'):
                deliveries += 1
                if deliveries < 3:
                    return httpx.ReadTimeout('delivery ack lost')
            return 200, {'ok': True}

        http = HTTP(handle)
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result={'ok': True, 'data': {'result': 'secret result', 'attachments': ['data/images/x.png']}})
        self.assertEqual(len(http.calls), 4)
        self.assertEqual(http.calls[1][1], http.calls[2][1])
        self.assertEqual(http.calls[2][1], http.calls[3][1])
        self.assertEqual(http.calls[-1][1]['attachment_paths'], ['data/images/x.png'])
        self.assertEqual(lifecycle._async_tool_tasks['work']['status'], 'done')

    async def test_delivery_exhaustion_is_not_computation_failure(self):
        async def handle(url, payload):
            if url.endswith('/async_tool_result'):
                return 503, {'ok': False}
            return 200, {'ok': True}

        http = HTTP(handle)
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result='worked')
        entry = lifecycle._async_tool_tasks['work']
        self.assertEqual(entry['status'], 'delivery_failed')
        self.assertEqual(entry['compute_status'], 'done')
        self.assertEqual(sum(u.endswith('/finish') for u, _ in http.calls), 1)
        self.assertFalse(any('failed:' in p.get('message', '') for _, p in http.calls))

    async def test_finish_without_ack_does_not_send_result(self):
        async def handle(url, payload):
            return 200, {'ok': False, 'error': 'unknown async_id'}

        http = HTTP(handle)
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result='worked')
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(all(url.endswith('/finish') for url, _ in http.calls))
        self.assertEqual(lifecycle._async_tool_tasks['work']['status'], 'delivery_failed')

    async def test_cancelled_result_never_delivers_original_text_or_attachments(self):
        lifecycle.mark_async_tool_cancelled('work')
        http = HTTP()
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result={'ok': True, 'data': {'result': 'sensitive', 'attachments': ['secret.png']}})
        self.assertEqual(http.calls[0][1]['status'], 'cancelled')
        self.assertNotIn('sensitive', http.calls[-1][1]['message'])
        self.assertNotIn('attachment_paths', http.calls[-1][1])

    async def test_cancel_before_background_coroutine_starts_prevents_execution(self):
        registry, http = Registry(), HTTP()
        lifecycle.mark_async_tool_cancelled('work')
        args = self.delivery_kwargs(http)
        args['registry'] = registry
        args.pop('started_at')
        await lifecycle._run_async_tool(**args, authorized=True)
        self.assertEqual(registry.executions, 0)
        self.assertEqual(http.calls[0][1]['status'], 'cancelled')

    async def test_cancel_while_reporting_finish_sends_only_cancellation_text(self):
        finishes = 0

        async def handle(url, payload):
            nonlocal finishes
            if url.endswith('/finish'):
                finishes += 1
                if finishes == 1:
                    lifecycle.mark_async_tool_cancelled('work')
            return 200, {'ok': True}

        http = HTTP(handle)
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result='secret output')
        delivered = [p for u, p in http.calls if u.endswith('/async_tool_result')]
        self.assertEqual(len(delivered), 1)
        self.assertNotIn('secret output', delivered[0]['message'])
        self.assertIn('cancelled', delivered[0]['message'])
        self.assertEqual(lifecycle._async_tool_tasks['work']['status'], 'cancelled')

    async def test_failure_return_is_reported_as_failure(self):
        http = HTTP()
        await lifecycle._deliver_async_result(**self.delivery_kwargs(http), result={'ok': False, 'error': 'process exited 1'})
        self.assertEqual(http.calls[0][1]['status'], 'failed')
        self.assertEqual(lifecycle._async_tool_tasks['work']['status'], 'failed')

    async def test_real_execution_identity_requires_boot_and_generation(self):
        async def handle(url, payload):
            return 200, {'ok': True}

        ctx = context(self.root, HTTP(handle), Registry())
        with self.assertRaisesRegex(ValueError, 'omitted boot'):
            await lifecycle.report_async_tool_started(rctx=ctx.extra['loop_state'].rctx, node_id='main', async_id='work', tool_name='x', tool_args={})


if __name__ == '__main__':
    unittest.main()
