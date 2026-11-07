"""Offline regression checks for configuration, task completion and preempt ACKs."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from engine.context import RunContext
from engine.inference.pseudo_tools import allowed_pseudo_names, build_node_tool_specs
from engine.inference import resume_builder
from engine.node import Node, load_node
from engine.prompt import assemble_prompt
from engine import runner
from supervisor.task_store import TaskStoreMixin
from supervisor.types import Task, TaskKind, TaskStatus


def make_task():
    now = datetime.now(timezone.utc)
    return Task(task_id='task', session_id='session', node_id='example', kind=TaskKind.node,
                status=TaskStatus.running, created_at=now, updated_at=now)


class StoreFixture(TaskStoreMixin):
    def __init__(self):
        self._lock = threading.RLock()
        self.tasks = {'task': make_task()}
        self.eventlog = Mock()


class ConfigurationTests(unittest.TestCase):
    def test_manage_workers_survives_yaml_parsing_and_spec_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / 'config/nodes'
            nodes.mkdir(parents=True)
            (nodes / 'example.yaml').write_text(
                'id: example\ntype: ai\ncontrol_tools: [manage_workers, preempt_task]\n'
                'tool_access:\n  mode: allowlist\n  allow:\n    - manage_memory: [list]\n'
                'memories: {mode: constant}\n', encoding='utf-8')
            node = load_node(root, 'example')
            self.assertEqual(node.control_tools, ['manage_workers'])
            self.assertEqual(node.tool_access.allow, ['manage_memory:list'])
            self.assertEqual(node.memory_access.mode, 'constant')
            _, tools = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: []), node=node)
            self.assertIn('manage_workers', allowed_pseudo_names(tools, node))

    def test_invalid_permission_list_is_rejected_instead_of_ignored(self):
        from engine.node import _node_from_dict
        for field in ('allow', 'deny'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'must be lists'):
                _node_from_dict(Path('/unused'), 'example', {'tool_access': {'mode': 'all', field: 'manage_memory'}})

    def test_new_management_names_cannot_be_shadowed_by_script_tools(self):
        from toolbox.builtins import RESERVED_TOOL_NAMES
        self.assertTrue({'manage_memory', 'manage_skill', 'manage_mcp_client', 'finish', 'ask',
                         'intermediate_reply', 'compact_context', 'switch_node'} <= RESERVED_TOOL_NAMES)

    def test_default_policy_requires_approval_for_mcp_configuration(self):
        from supervisor.policy import PolicyEngine
        from supervisor.types import SafetyLevel
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = PolicyEngine(workspace_root=root)
            self.assertEqual(policy.evaluate_tool(name='manage_mcp_client').safety_level, SafetyLevel.auto)
            self.assertEqual(policy.evaluate_write_file(path=str(root / 'data/mcp_clients.yaml')).safety_level,
                             SafetyLevel.approval_required)

    def test_frontend_settings_do_not_offer_retired_management_tools(self):
        source = (Path(__file__).resolve().parents[1] / 'adapters/web/frontend/src/components/settings/pages/ClientSettingsPage.tsx').read_text()
        for name in ('save_memory', 'list_memories', 'delete_memory', 'create_or_update_skill',
                     'list_skills', 'delete_skill', 'create_or_update_mcp_client', 'list_mcp_clients',
                     'delete_mcp_client', 'create_schedule', 'list_schedules', 'delete_schedule',
                     'create_or_update_tool', 'reload_tools', 'preempt_task', 'cancel_active_tasks'):
            self.assertNotIn(f"toolName: '{name}'", source)
        for name in ('manage_memory', 'manage_skill', 'manage_mcp_client', 'manage_workers'):
            self.assertIn(f"toolName: '{name}'", source)

    def render(self, template, instruction):
        return assemble_prompt(Path('/unused'), Node(id='example', type='ai', prompt=template),
                               variables={'instruction': instruction})

    def test_user_escape_sequences_are_not_decoded(self):
        instruction = r'C:\users\new\x \u1234 \n'
        messages = self.render('Static\\n# %%DYNAMIC%%\\n{{instruction}}', instruction)
        self.assertEqual(messages, [{'role': 'system', 'content': 'Static'},
                                    {'role': 'system', 'content': instruction}])

    def test_invalid_template_escape_does_not_crash(self):
        self.assertEqual(self.render(r'Broken \x {{instruction}}', 'text')[0]['content'], r'Broken \x text')

    def test_variable_cannot_insert_a_template_boundary(self):
        self.assertEqual(len(self.render('Static {{instruction}}', '# %%DYNAMIC%% text')), 1)

    def test_unicode_and_block_prompt_variables_are_preserved(self):
        self.assertEqual(self.render('说明 {{instruction}}', '中文\\x')[0]['content'], '说明 中文\\x')
        blocks = self.render([{'role': 'system', 'content': '{{instruction}}'}], r'\x')
        self.assertEqual(blocks[0]['content'], r'\x')

    def test_restored_history_preserves_structural_message_type(self):
        from engine.conversation_store import Message, MessageType
        message = Message(id='summary', role='user', content='结构化摘要', message_type=MessageType.SUMMARY,
                          created_at='2026-01-01T00:00:00Z', meta='malformed')
        restored = runner._message_to_history_dict(message)
        self.assertEqual(restored['message_type'], MessageType.SUMMARY)
        self.assertEqual(restored['_meta']['message_type'], MessageType.SUMMARY)
        self.assertEqual(restored['_meta']['timestamp'], message.created_at)
        self.assertNotIn('response_info', restored)

    def test_template_includes_keep_their_existing_path_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / 'config/nodes'
            nodes.mkdir(parents=True)
            (root / 'outside.txt').write_text('must not be included')
            prompt = '{{include:../../outside.txt}}'
            result = assemble_prompt(root, Node(id='example', type='ai', prompt=prompt))
            self.assertEqual(result[0]['content'], prompt)


class ToolActionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_action_matches_the_real_entrypoint(self):
        from toolbox.builtins import schedules
        from engine.inference.pseudo_tools import resolve_effective_action, is_tool_action_allowed
        from engine.node import ToolAccess
        node = Node(id='limited', type='ai', tool_access=ToolAccess(mode='all', deny=['manage_schedule:upsert']))
        cases = [
            {}, {'id': 'job'}, {'id': 'job', 'cron': '* * * * *'},
            {'id': 'job', 'enabled': False}, {'id': 'job', 'timeout': 0},
            {'id': 'job', 'entry_node_id': 'example'}, {'id': 'job', 'timeout': None},
            {'id': '', 'text': 'no identifier'}, {'action': 'list', 'id': 'job', 'enabled': True},
        ]
        with patch.object(schedules, '_list_schedules', AsyncMock(return_value={'selected': 'list'})), \
                patch.object(schedules, '_upsert_schedule', AsyncMock(return_value={'selected': 'upsert'})):
            for args in cases:
                with self.subTest(args=args):
                    actual = (await schedules.manage_schedule(args, SimpleNamespace()))['selected']
                    self.assertEqual(resolve_effective_action('manage_schedule', args)[1], actual)
                    self.assertEqual(is_tool_action_allowed(node, 'manage_schedule', args), actual != 'upsert')

    async def test_tool_management_default_matches_empty_and_false_fields(self):
        from toolbox.builtins import tool_manage
        from engine.inference.pseudo_tools import resolve_effective_action
        with patch.object(tool_manage, '_reload_tools', AsyncMock(return_value={'selected': 'reload'})), \
                patch.object(tool_manage, '_upsert_tool', AsyncMock(return_value={'selected': 'upsert'})):
            for args in ({}, {'name': '', 'script': ''}, {'name': False}, {'name': 'demo'}, {'script': 'print(1)'}):
                with self.subTest(args=args):
                    actual = (await tool_manage.manage_tool(args, SimpleNamespace()))['selected']
                    self.assertEqual(resolve_effective_action('manage_tool', args)[1], actual)

    async def test_external_mode_selector_cannot_be_bypassed_by_extra_action(self):
        from engine.inference.pseudo_tools import is_tool_action_allowed, resolve_effective_action
        from engine.node import ToolAccess
        node = Node(id='limited', type='ai', tool_access=ToolAccess(mode='all', deny=['external:write']))
        spec = {'name': 'external', 'input_schema': {'properties': {'mode': {'enum': ['read', 'write']}}}}
        self.assertFalse(is_tool_action_allowed(node, 'external', {'action': 'read', 'mode': 'write'}, spec))
        self.assertFalse(is_tool_action_allowed(node, 'external', {'action': 'read'}, spec))
        self.assertTrue(is_tool_action_allowed(node, 'external', {'mode': 'read'}, spec))
        self.assertEqual(resolve_effective_action('external', {'mode': 'write'}, spec), ('external', 'write'))
        # External servers may treat unrecognized spellings as a different mode.
        for value in (' READ ', 'read ', True):
            self.assertFalse(is_tool_action_allowed(node, 'external', {'mode': value}, spec))
        node.control_tools = 'none'
        _, tools = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: [spec]), node=node)
        self.assertIn('mode', tools[0]['function']['parameters']['required'])

    async def test_allow_entries_are_alternatives_and_deny_still_wins(self):
        from engine.inference.pseudo_tools import is_tool_action_allowed
        from engine.node import ToolAccess
        spec = {'name': 'manage_memory', 'input_schema': {'properties': {'action': {'enum': ['list', 'upsert', 'delete']}}}}
        cases = [
            (['manage_memory:list', 'manage_memory:upsert'], [], {'list', 'upsert'}),
            (['manage_memory:!delete'], [], {'list', 'upsert'}),
            (['manage_memory:!delete', 'manage_memory:delete'], [], {'list', 'upsert', 'delete'}),
            (['manage_memory', 'manage_memory:list'], [], {'list', 'upsert', 'delete'}),
            (['manage_memory:!delete'], ['manage_memory:upsert'], {'list'}),
            (['manage_memory:!delete'], ['manage_memory:!list'], {'list'}),
            (['manage_memory', 'manage_memory:!delete'], [], {'list', 'upsert', 'delete'}),
            (['manage_memory:!delete', 'manage_memory:!upsert'], [], {'list', 'upsert', 'delete'}),
            (['manage_memory:!delete,list', 'manage_memory:!delete,upsert'], [], {'list', 'upsert'}),
            (['manage_memory', 'manage_memory:!delete'], ['manage_memory:delete'], {'list', 'upsert'}),
        ]
        for allow, deny, expected in cases:
            with self.subTest(allow=allow, deny=deny):
                node = Node(id='limited', type='ai', control_tools='none', tool_access=ToolAccess(mode='allowlist', allow=allow, deny=deny))
                _, tools = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: [spec]), node=node)
                visible = set(tools[0]['function']['parameters']['properties']['action']['enum'])
                self.assertEqual(visible, expected)
                for action in ('list', 'upsert', 'delete'):
                    self.assertEqual(is_tool_action_allowed(node, 'manage_memory', {'action': action}, spec), action in expected)

    async def test_mcp_action_matches_the_explicit_only_entrypoint(self):
        from engine.builtin import mcp
        from engine.inference.pseudo_tools import resolve_effective_action
        with patch.object(mcp, '_list_mcp_clients', AsyncMock(return_value={'selected': 'list'})), \
                patch.object(mcp, '_upsert_mcp_client', AsyncMock(return_value={'selected': 'upsert'})), \
                patch.object(mcp, '_delete_mcp_client', AsyncMock(return_value={'selected': 'delete'})):
            for args in ({}, {'id': 'client', 'transport': 'stdio'},
                         {'id': 'client', 'enabled': False}, {'action': 'upsert'},
                         {'action': 'delete'}, {'action': 'list'}):
                with self.subTest(args=args):
                    actual = (await mcp.manage_mcp_client(args, SimpleNamespace()))['selected']
                    self.assertEqual(resolve_effective_action('manage_mcp_client', args)[1], actual)
            for action in ('create', 'update', 'remove'):
                with self.subTest(action=action):
                    self.assertFalse((await mcp.manage_mcp_client({'action': action}, SimpleNamespace()))['ok'])
                    self.assertEqual(resolve_effective_action('manage_mcp_client', {'action': action})[1], action)

    async def test_external_enum_case_uses_the_same_policy_in_gate_and_schema(self):
        from engine.inference.pseudo_tools import is_tool_action_allowed
        from engine.node import ToolAccess
        node = Node(id='limited', type='ai', control_tools='none',
                    tool_access=ToolAccess(mode='all', deny=['external:write']))
        spec = {'name': 'external', 'input_schema': {'properties': {'mode': {'enum': ['READ', 'WRITE']}}}}
        self.assertFalse(is_tool_action_allowed(node, 'external', {'mode': 'WRITE'}, spec))
        self.assertTrue(is_tool_action_allowed(node, 'external', {'mode': 'READ'}, spec))
        self.assertFalse(is_tool_action_allowed(node, 'external', {'mode': 'read'}, spec))
        _, tools = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: [spec]), node=node)
        self.assertEqual(tools[0]['function']['parameters']['properties']['mode']['enum'], ['READ'])

    async def test_malformed_action_rules_do_not_expand_permissions(self):
        from engine.inference.pseudo_tools import is_tool_action_allowed
        from engine.node import ToolAccess
        for field in ('allow', 'deny'):
            for rule in ('manage_memory:', 'manage_memory:list,!delete'):
                with self.subTest(field=field, rule=rule):
                    node = Node(id='limited', type='ai', tool_access=ToolAccess(mode='allowlist' if field == 'allow' else 'all', **{field: [rule]}))
                    self.assertFalse(is_tool_action_allowed(node, 'manage_memory', {'action': 'delete'}))


class PreemptStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = StoreFixture()

    def test_append_then_ack_preserves_concurrent_arrival(self):
        self.state.preempt_task('task', 'first', [{'path': 'first'}])
        observed = self.state.is_task_preempted('task')
        self.state.preempt_task('task', 'second', [{'path': 'second'}])
        result = self.state.consume_preempt_message('task', observed['message'], len(observed['attachments']))
        self.assertEqual(result['message'], 'first')
        self.assertEqual(result['attachments'], [{'path': 'first'}])
        self.assertEqual(self.state.is_task_preempted('task'),
                         {'preempted': True, 'message': 'second', 'attachments': [{'path': 'second'}]})

    def test_empty_ack_does_not_discard_a_new_message(self):
        self.state.preempt_task('task', 'new')
        self.state.consume_preempt_message('task', '', 0)
        self.assertEqual(self.state.is_task_preempted('task')['message'], 'new')

    def test_mismatched_prefix_keeps_messages_and_attachments(self):
        self.state.preempt_task('task', 'abcd', [{'path': 'file'}])
        before = self.state.is_task_preempted('task')
        self.state.consume_preempt_message('task', 'abc', 1)
        self.assertEqual(self.state.is_task_preempted('task'), before)

    def test_only_observed_attachments_are_removed(self):
        self.state.preempt_task('task', '', [{'path': 'one'}])
        self.state.preempt_task('task', '', [{'path': 'two'}])
        self.state.consume_preempt_message('task', '', 1)
        self.assertEqual(self.state.is_task_preempted('task'),
                         {'preempted': True, 'message': '', 'attachments': [{'path': 'two'}]})

    def test_all_observed_data_clears_flag(self):
        self.state.preempt_task('task', 'text', [{'path': 'file'}])
        self.state.consume_preempt_message('task', 'text', 1)
        self.assertEqual(self.state.is_task_preempted('task'),
                         {'preempted': False, 'message': '', 'attachments': []})

    def test_invalid_attachment_count_keeps_entire_queue(self):
        for count in (-1, 100, True, '1'):
            with self.subTest(count=count):
                state = StoreFixture()
                state.preempt_task('task', 'text', [{'path': 'file'}])
                before = state.is_task_preempted('task')
                state.consume_preempt_message('task', 'text', count)
                self.assertEqual(state.is_task_preempted('task'), before)

    def test_legacy_clear_all_remains_explicitly_supported(self):
        self.state.preempt_task('task', 'first')
        self.state.preempt_task('task', 'second')
        self.assertEqual(self.state.consume_preempt_message('task')['message'], 'first\n\nsecond')
        self.assertFalse(self.state.is_task_preempted('task')['preempted'])

    def test_terminal_task_is_not_reactivated(self):
        self.state.tasks['task'].status = TaskStatus.cancelled
        self.assertFalse(self.state.preempt_task('task', 'late'))


class HostAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_exit_closes_metrics_and_http_clients(self):
        from engine import runtime_metrics
        loop = asyncio.get_running_loop()
        for mode in ('signal', 'cancelled'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                registry = SimpleNamespace(load_remote_tools=AsyncMock(return_value=0))
                client = SimpleNamespace(get=AsyncMock(side_effect=asyncio.CancelledError))
                manager = AsyncMock()
                manager.__aenter__.return_value = client
                manager.__aexit__.return_value = False
                signal_handler = lambda _signal, callback: callback() if mode == 'signal' else None
                with patch.object(runner, 'ToolRegistry', return_value=registry), \
                        patch.object(runner, 'wait_supervisor', AsyncMock()), \
                        patch.object(runner, '_register_engine', AsyncMock()), \
                        patch.object(runner.httpx, 'AsyncClient', return_value=manager), \
                        patch.object(loop, 'add_signal_handler', side_effect=signal_handler), \
                        patch.object(runtime_metrics, 'WorkerMetrics') as metrics, \
                        contextlib.redirect_stdout(io.StringIO()):
                    await runner.worker_loop(supervisor_url='https://supervisor.invalid', workspace_root=Path(directory))
                metrics.return_value.close.assert_called_once_with()
                self.assertEqual(manager.__aexit__.await_count, 2)

    async def test_compact_failure_does_not_reset_breaker(self):
        context = SimpleNamespace(parent_session_id='parent', session_id='branch', emit_event=AsyncMock())
        with patch.object(resume_builder, 'record_compact_success') as record, \
                patch.object(resume_builder, 'record_compact_failure') as failed, \
                patch.object(resume_builder, 'get_bus', return_value=Mock()):
            await resume_builder._emit_compact_done(resume_data={'success': False}, node=SimpleNamespace(id='node'), rctx=context)
            record.assert_not_called()
            failed.assert_called_once_with('parent')
            self.assertFalse(context.emit_event.call_args.args[1]['success'])

    async def test_compact_success_resets_parent_breaker(self):
        context = SimpleNamespace(parent_session_id='parent', session_id='branch', emit_event=AsyncMock())
        with patch.object(resume_builder, 'record_compact_success') as record, \
                patch.object(resume_builder, 'get_bus', return_value=Mock()):
            await resume_builder._emit_compact_done(resume_data={'success': True}, node=SimpleNamespace(id='node'), rctx=context)
            record.assert_called_once_with('parent')

    async def test_compact_resume_updates_the_engine_local_breaker(self):
        from engine.compact import is_compact_circuit_open, record_compact_success
        sid = 'isolated-engine-resume-test'
        context = SimpleNamespace(parent_session_id='', session_id=sid, emit_event=AsyncMock())
        try:
            with patch.object(resume_builder, 'get_bus', return_value=Mock()):
                for _ in range(3):
                    await resume_builder._emit_compact_done(resume_data={'success': False}, node=SimpleNamespace(id='node'), rctx=context)
                self.assertTrue(is_compact_circuit_open(sid))
                await resume_builder._emit_compact_done(resume_data={'success': True}, node=SimpleNamespace(id='node'), rctx=context)
                self.assertFalse(is_compact_circuit_open(sid))
        finally:
            record_compact_success(sid)

    async def test_compact_resume_uses_the_child_session_when_present(self):
        context = SimpleNamespace(child_session_id='child', parent_session_id='parent', session_id='runtime', emit_event=AsyncMock())
        with patch.object(resume_builder, 'record_compact_failure') as failed, \
                patch.object(resume_builder, 'record_compact_success') as succeeded, \
                patch.object(resume_builder, 'get_bus', return_value=Mock()):
            for success in (False, True):
                await resume_builder._emit_compact_done(resume_data={'success': success}, node=SimpleNamespace(id='node'), rctx=context)
            failed.assert_called_once_with('child')
            succeeded.assert_called_once_with('child')

    async def test_config_failure_reports_terminal_task_result(self):
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200)))
        with patch.object(runner, 'fetch_llm_secret', AsyncMock(side_effect=RuntimeError('offline'))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            await runner._handle_task(http, object(), 'https://supervisor.invalid', Path('/unused'), object(),
                                     {'task_id': 'task', 'node_id': 'node', 'kind': 'node'}, 'worker')
        self.assertEqual(http.post.await_count, 1)
        self.assertTrue(http.post.call_args.args[0].endswith('/task/complete'))
        self.assertEqual(http.post.call_args.kwargs['json']['result']['action'], 'fail')

    async def test_malformed_task_generation_is_reported(self):
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200)))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            await runner._handle_task(http, object(), 'https://supervisor.invalid', Path('/unused'), object(),
                                     {'task_id': 'task', 'session_generation': 'bad', 'kind': 'node'}, 'worker')
        self.assertEqual(http.post.call_args.kwargs['json']['result']['action'], 'fail')

    async def test_run_context_sends_consumed_prefix(self):
        http = SimpleNamespace(post=AsyncMock())
        context = RunContext(workspace_root=Path('/unused'), supervisor_url='https://supervisor.invalid',
                             session_id='session', worker_id='worker', http=http, llm_http=http, task_id='task')
        await context.consume_preempt('first', 1)
        self.assertEqual(http.post.call_args.kwargs['json'], {'consumed_message': 'first', 'consumed_attachment_count': 1})

    async def test_attachment_only_preempt_is_injected_instead_of_stopping(self):
        from engine.builtin import preempt
        payload = {'preempted': True, 'message': '', 'attachments': [{'path': 'file'}]}
        context = SimpleNamespace(check_cancelled=AsyncMock(return_value=False),
                                  check_preempted=AsyncMock(return_value=payload))
        loop = SimpleNamespace(rctx=context, preempt_after_step=False, preempt_inject_info=None)
        with patch.object(preempt, '_inject_preempt_message', AsyncMock()) as inject:
            result = await preempt.PreemptChecker().handle(SimpleNamespace(extra={'loop_state': loop}))
        inject.assert_awaited_once()
        self.assertTrue(result.modified)
        self.assertFalse(loop.preempt_after_step)

    async def test_api_ack_validates_count_and_preserves_newer_data(self):
        from supervisor import admin_api
        from supervisor.api import create_app
        from supervisor.config_store import ConfigStore
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(admin_api, '_admin_token', 'offline-engine-token'), \
                patch.object(admin_api, '_web_auth', None), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            state = SupervisorState(workspace_root=root, eventlog=EventLog(root / 'data/events.jsonl', 'test'),
                                    policy=PolicyEngine(workspace_root=root))
            state.tasks['task'] = make_task()
            app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=root / 'data/config.yaml'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://supervisor.invalid',
                                         headers={'Authorization': 'Bearer offline-engine-token'}) as client:
                state.preempt_task('task', 'first', [{'path': 'one'}])
                context = RunContext(workspace_root=root, supervisor_url='https://supervisor.invalid', session_id='session',
                                     worker_id='worker', http=client, llm_http=client, task_id='task')
                state.preempt_task('task', 'second', [{'path': 'two'}])
                await context.consume_preempt('first', 1)
                remaining = state.is_task_preempted('task')
                self.assertEqual(remaining['message'], 'second')
                for count in (-1, '1', True):
                    response = await client.post('/v1/tasks/task/preempt_consumed',
                                                 json={'consumed_message': 'second', 'consumed_attachment_count': count})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(state.is_task_preempted('task'), remaining)


if __name__ == '__main__':
    unittest.main()
