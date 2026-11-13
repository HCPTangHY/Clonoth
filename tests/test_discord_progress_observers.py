"""Progress observers do not take ownership of normal Discord reply/cancel surfaces.

Offline: real handle_agent, ClonothClient, EventRouter and DiscordCallbacks;
HTTP and Discord transport are simulated. No production state or gateway access.
"""
from __future__ import annotations

import copy
import json
import re
from collections import deque
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import unittest

import httpx

from adapters.discord import agent, callbacks
from adapters.discord.callbacks import DiscordCallbacks
from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import MainTaskState, SessionState, TriggerInfo
from clonoth_sdk.types import Event


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Channel:
    def __init__(self, cid=42):
        self.id = cid
        self.guild = NS(id=99, members=[])
        self.send = AsyncMock(side_effect=lambda *a, **k: surface(900, self))

    def typing(self):
        return Typing()


class DM(Channel):
    def __init__(self, cid=42):
        super().__init__(cid)
        self.guild = None


def surface(mid, channel):
    return NS(id=mid, channel=channel, edit=AsyncMock(), delete=AsyncMock())


class ProgressObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http_calls = []
        self.next_seq = 10
        self.channel = Channel()
        self.log_channel = Channel(99)
        self.client = ClonothClient('https://offline.invalid')
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        self.rt = NS(
            clonoth_client=self.client, session_state=SessionState(), channel_history={},
            history_seq_counter=0, history_max_len=30, key_roles=set(), superusers={1},
            transient_msg_ids=deque(maxlen=1000), split_signal='[SPLIT]',
            react_pattern=re.compile(r'\[REACT:(.*?)\]'), restart_signal='[BOT_RESTART]',
            entry_node_id='main', child_node_display_names={}, agent_log_channel_id=99,
            bot_restart_view_factory=None, approval_view_factory=None, workspace=Path.cwd(),
            dc_client=NS(user=None, get_channel=lambda cid: self.log_channel if cid == 99 else self.channel,
                         fetch_channel=AsyncMock()),
        )
        self.cb = DiscordCallbacks(self.rt)
        self.rt.callbacks = self.cb
        self.router = EventRouter(self.client, self.rt.session_state, self.cb, BotConfig(
            base_url=self.client.base_url, entry_node_id='main', conversation_key_prefix='discord'))
        self.router.set_raw_event_hook(self.cb.raw_event_hook)
        self.views = []
        self.observed = []
        self.cb.subscribe_progress(lambda trigger, text, terminal: self.observed.append((trigger, text, terminal)))

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        await self.client.close()

    async def http(self, request):
        payload = json.loads(request.content) if request.content and request.headers.get('content-type') == 'application/json' else None
        self.http_calls.append((request.method, request.url.path, payload))
        path = request.url.path
        if path == '/v1/inbound':
            data = {'session_id': 'session', 'inbound_seq': self.next_seq}
            self.next_seq += 10
        elif path.endswith('/running_tasks'):
            data = {'tasks': []}
        elif path.endswith('/workspace'):
            data = {'name': 'existing-workspace'}
        elif path == '/v1/attachments/upload':
            data = {'path': 'data/attachments/input.txt', 'name': 'input.txt', 'type': 'file'}
        elif path.startswith('/v1/tasks/') and path.endswith('/cancel'):
            data = {'ok': True}
        else:
            raise AssertionError(f'Unexpected HTTP call: {request.method} {path}')
        return httpx.Response(200, json=data)

    async def submit(self, *, channel=None, mid=1, attachment=False):
        channel = channel or self.channel
        status = surface(mid + 100, channel)
        sent = surface(mid + 200, channel)
        message = NS(id=mid, channel=channel, guild=channel.guild,
                     author=NS(id=1, name='alice', display_name='Alice', roles=[]),
                     content='normal request', reference=None, message_snapshots=[], attachments=[],
                     reply=AsyncMock(side_effect=[status, sent, sent]), add_reaction=AsyncMock())
        if attachment:
            message.attachments = [NS(content_type='text/plain', filename='input.txt', size=5,
                                      read=AsyncMock(return_value=b'input'))]
        with patch.object(agent.discord, 'DMChannel', DM):
            await agent.handle_agent(self.rt, message, 'normal request', channel.id)
        seq = self.next_seq - 10
        trigger = self.rt.session_state.get_trigger(seq)
        self.assertIsNotNone(trigger)
        self.assertIs(trigger.platform_data['status_msg'], status)
        view = trigger.platform_data['cancel_view']
        self.views.append(view)
        self.assertIsInstance(view, agent.CancelView)
        self.assertIs(message.reply.call_args.kwargs['view'], view)
        self.assertIs(trigger.platform_data['message'], message)
        self.assertFalse(trigger.platform_data.get('background'))
        return trigger, message, status, view

    async def event(self, kind, seq=10, **fields):
        await self.router._dispatch(Event.from_dict({
            'seq': 100, 'type': kind, 'session_id': 'session',
            'payload': {'node_id': 'main', 'task_id': f'root-{seq}', 'source_inbound_seq': seq, **fields},
        }))

    async def start(self, seq=10):
        await self.event('task_created', seq)
        await self.event('node_started', seq, node_name='Main')

    async def activity(self, seq=10, tool_path='ordinary.txt'):
        await self.event('stream_delta', seq, type='text', content=f'Preview-{seq}', llm_request_id=f'llm-{seq}')
        await self.event('assistant_text', seq, text=f'Explanation-{seq}')
        await self.event('tool_call_start', seq, tool_name='read_file', arguments={'path': tool_path}, tool_call_id=f'tool-{seq}')
        await self.event('tool_call_end', seq, tool_call_id=f'tool-{seq}', status='success')
        await self.event('handoff_progress', seq, message=f'Work-{seq}')
        await self.router._refresh_active_states()

    async def test_normal_guild_reply_timer_tools_cancel_and_attachments_remain_intact(self):
        trigger, message, status, view = await self.submit(attachment=True)
        body = next(body for method, path, body in self.http_calls if path == '/v1/inbound')
        self.assertEqual(body['channel'], 'discord_guild')
        self.assertTrue(body['use_branch'])
        self.assertEqual(body['attachments'][0]['path'], 'data/attachments/input.txt')
        with patch.object(self.cb, '_should_edit', return_value=True):
            await self.start()
            self.assertEqual(view.task_id, 'root-10')
            self.assertIn('⏱', status.edit.call_args.kwargs['content'])
            await self.activity()
        self.assertIn('ordinary.txt ✓', status.edit.call_args.kwargs['content'])
        self.assertIn('Explanation-10', status.edit.call_args.kwargs['content'])
        for call in status.edit.call_args_list:
            self.assertNotIn('view', call.kwargs, 'progress edits must preserve original CancelView')
        self.assertFalse(view.cancel_task.disabled)
        self.assertTrue(any('⏱' in text and 'ordinary.txt ✓' in text for _, text, terminal in self.observed if not terminal))
        marker = object()
        with patch.object(callbacks, '_atts_to_discord_files', return_value=[marker]) as files:
            await self.event('outbound_message', text='Final answer', attachments=[{'path': 'output.txt'}])
        files.assert_called_once_with(self.rt, [{'path': 'output.txt'}])
        self.assertEqual(message.reply.call_args.kwargs['content'], 'Final answer')
        self.assertEqual(message.reply.call_args.kwargs['files'], [marker])
        status.delete.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.rt.session_state.get_trigger(trigger.inbound_seq))
        self.assertIn('Final answer', self.rt.channel_history[42][-1]['text'])

    async def test_normal_dm_keeps_original_progress_and_reply_path(self):
        self.channel = DM()
        trigger, message, status, view = await self.submit(channel=self.channel)
        body = next(body for method, path, body in self.http_calls if path == '/v1/inbound')
        self.assertEqual(body['channel'], 'discord_dm')
        self.assertFalse(body['use_branch'])
        self.assertTrue(trigger.is_dm)
        with patch.object(self.cb, '_should_edit', return_value=True):
            await self.start()
            await self.activity(tool_path='dm-file.txt')
        display = status.edit.call_args.kwargs['content']
        self.assertIn('Agent 执行中', display)
        self.assertIn('⏱', display)
        self.assertIn('Preview-10', display)
        self.assertIn('Work-10', display)
        self.assertIn('dm-file.txt ✓', self.observed[-1][1])
        self.assertEqual(view.task_id, 'root-10')
        self.assertTrue(all('view' not in call.kwargs for call in status.edit.call_args_list))
        self.log_channel.send.assert_not_awaited()
        await self.event('outbound_message', text='DM answer')
        self.assertEqual(message.reply.call_args.kwargs['content'], 'DM answer')
        status.delete.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_normal_cancel_remains_precise_and_does_not_cancel_other_source(self):
        first, _, first_status, first_view = await self.submit(mid=1)
        second, _, second_status, second_view = await self.submit(mid=2)
        with patch.object(self.cb, '_should_edit', return_value=True):
            await self.start(first.inbound_seq)
            await self.start(second.inbound_seq)
            await self.activity(first.inbound_seq, 'first.txt')
            await self.activity(second.inbound_seq, 'second.txt')
        by_seq = {trigger.inbound_seq: text for trigger, text, _ in self.observed}
        self.assertIn('first.txt', by_seq[10])
        self.assertNotIn('second.txt', by_seq[10])
        self.assertIn('second.txt', by_seq[20])
        self.assertNotIn('first.txt', by_seq[20])
        interaction = NS(user=NS(id=1), response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))
        self.assertTrue(await first_view.interaction_check(interaction))
        await first_view.cancel_task.callback(interaction)
        self.assertTrue(first_view.cancel_task.disabled)
        self.assertFalse(second_view.cancel_task.disabled)
        self.assertIs(interaction.response.edit_message.call_args.kwargs['view'], first_view)
        cancels = [path for method, path, _ in self.http_calls if method == 'POST' and path.endswith('/cancel')]
        self.assertEqual(cancels, ['/v1/tasks/root-10/cancel'])
        second_edits = second_status.edit.await_count
        await self.event('task_cancelled', 10)
        self.assertEqual(first_status.edit.call_args.kwargs, {'content': '⚠️ 任务已取消。', 'view': None})
        self.assertEqual(second_status.edit.await_count, second_edits)
        self.assertIs(self.rt.session_state.get_trigger(20), second)

    async def test_observer_exceptions_do_not_interrupt_normal_progress_or_other_observers(self):
        trigger, message, status, view = await self.submit()
        def broken(*args):
            raise RuntimeError('observer failure')
        self.cb.subscribe_progress(broken)
        with self.assertLogs('discord_bot', level='ERROR') as logs, patch.object(self.cb, '_should_edit', return_value=True):
            await self.start()
            await self.activity()
        self.assertTrue(any('Progress observer failed' in line for line in logs.output))
        self.assertIn('ordinary.txt ✓', status.edit.call_args.kwargs['content'])
        self.assertEqual(view.task_id, 'root-10')
        self.assertTrue(self.observed)
        await self.event('outbound_message', text='Still delivered')
        self.assertEqual(message.reply.call_args.kwargs['content'], 'Still delivered')
        status.delete.assert_awaited_once()

    async def test_observer_formats_copies_before_throttling_and_disposes_idempotently(self):
        trigger = TriggerInfo(10, 'discord:42', 'session', False, platform_data={'background': True})
        state = MainTaskState(progress_records=['Work'])
        dot = {'thinking_start_time': 100, 'had_stream_activity': True, 'dot_step': 2,
               'tool_lines': [{'text': 'read', 'done': True, 'ok': True}], 'prose_lines': ['note']}
        self.cb._dot_states[10] = dot
        before = copy.deepcopy(dot)
        listener = Mock()
        dispose = self.cb.subscribe_progress(listener)
        with patch.object(self.cb, '_should_edit', return_value=False):
            await self.cb.update_progress(trigger, state)
        listener.assert_called_once()
        self.assertEqual(dot, before)
        self.assertLessEqual(len(listener.call_args.args[1]), 1800)
        self.assertFalse(listener.call_args.args[2])
        self.assertIs(listener.call_args.args[0], trigger)
        self.log_channel.send.assert_not_awaited()
        dispose()
        dispose()
        with patch.object(self.cb, '_should_edit', return_value=False):
            await self.cb.update_progress(trigger, state)
        listener.assert_called_once()

    async def test_no_listeners_adds_no_observer_formatting_and_terminal_guard_is_respected(self):
        self.cb._progress_listeners.clear()
        trigger = TriggerInfo(10, 'discord:42', 'session', False, platform_data={'background': True})
        with patch.object(self.cb, '_should_edit', return_value=False), \
             patch.object(callbacks, '_format_progress_log') as progress, \
             patch.object(callbacks, '_format_status_summary') as summary:
            await self.cb.update_progress(trigger, MainTaskState())
            progress.assert_not_called()
            summary.assert_not_called()
        listener = Mock()
        self.cb.subscribe_progress(listener)
        await self.cb.update_progress(trigger, MainTaskState(platform_data={'terminal_status': 'done'}))
        listener.assert_not_called()

    async def test_background_final_notification_has_last_tool_before_cleanup(self):
        trigger = TriggerInfo(10, 'discord:42', 'session', False, platform_data={'background': True})
        self.cb._dot_states[10] = {'thinking_start_time': 100, 'prose_lines': ['Last explanation'],
                                   'tool_lines': [{'text': 'final-tool', 'done': True, 'ok': True}]}
        snapshot = copy.deepcopy(self.cb._dot_states[10])
        notified = []
        def observe(t, text, terminal):
            self.assertEqual(self.cb._dot_states[10], snapshot)
            notified.append((t, text, terminal))
        self.cb.subscribe_progress(observe)
        await self.cb.finalize_main_progress(trigger, MainTaskState(progress_records=['Last step']), '⚠️ 失败')
        self.assertEqual(len(notified), 1)
        self.assertIs(notified[0][0], trigger)
        self.assertTrue(notified[0][2])
        self.assertIn('⚠️ 失败', notified[0][1])
        self.assertIn('Last explanation', notified[0][1])
        self.assertIn('final-tool ✓', notified[0][1])
        self.assertNotIn(10, self.cb._dot_states)

    async def test_custom_node_child_progress_getter_reads_tools_and_timer_without_mutation(self):
        self.rt.session_state.register_session('discord:42', 'session')
        with patch.object(self.cb, '_should_edit', return_value=True):
            await self.event('node_started', 40, node_id='custom', task_id='custom-root', node_name='Custom')
            await self.event('stream_delta', 40, node_id='custom', task_id='custom-root',
                             type='text', content='Custom preview', llm_request_id='custom-llm')
            await self.event('tool_call_start', 40, node_id='custom', task_id='custom-root',
                             tool_name='read_file', arguments={'path': 'custom.txt'}, tool_call_id='custom-tool')
            await self.event('tool_call_end', 40, node_id='custom', task_id='custom-root',
                             tool_call_id='custom-tool', status='success')
        self.assertIsNone(self.rt.session_state.get_trigger(40))
        state = self.rt.session_state.get_child_state('custom-root')
        before_dot = copy.deepcopy(self.cb._child_dot_states['custom-root'])
        before_lines = list(state.lines)
        sends = self.log_channel.send.await_count
        text, terminal = self.cb.get_task_progress('custom-root')
        self.assertFalse(terminal)
        self.assertIn('custom.txt ✓', text)
        self.assertIn('Custom preview', text)
        self.assertIn('⏱', text)
        self.assertLessEqual(len(text), 1800)
        self.assertEqual(self.cb._child_dot_states['custom-root'], before_dot)
        self.assertEqual(state.lines, before_lines)
        self.assertEqual(self.log_channel.send.await_count, sends)
        self.assertIsNone(self.cb.get_task_progress('missing-root'))
        self.assertNotIn('missing-root', self.rt.session_state.child_task_states)
        self.assertNotIn('missing-root', self.cb._child_dot_states)
        await self.event('task_completed', 40, node_id='custom', task_id='custom-root')
        ended = self.cb.get_task_progress('custom-root')
        self.assertTrue(ended[1], 'explicit task terminal callback stops the timer')
        self.assertIn('custom.txt ✓', ended[0])

    async def test_normal_and_background_sources_in_same_channel_are_independent(self):
        normal, message, status, view = await self.submit()
        with patch.object(self.cb, '_should_edit', return_value=True):
            await self.start(10)
            await self.event('node_started', 30, node_name='Background')
            background = self.rt.session_state.get_trigger(30)
            self.assertTrue(background.platform_data['background'])
            self.assertNotIn('status_msg', background.platform_data)
            await self.activity(10, 'normal-only.txt')
            await self.activity(30, 'background-only.txt')
        by_seq = {trigger.inbound_seq: text for trigger, text, _ in self.observed}
        self.assertNotIn('background-only', by_seq[10])
        self.assertNotIn('normal-only', by_seq[30])
        self.assertIn('normal-only', status.edit.call_args.kwargs['content'])
        self.assertFalse(view.cancel_task.disabled)
        await self.event('outbound_message', 30, text='Background result')
        self.assertEqual(self.channel.send.call_args.kwargs['content'], 'Background result')
        status.delete.assert_not_awaited()
        self.assertIs(self.rt.session_state.get_trigger(10), normal)
        await self.event('outbound_message', 10, text='Normal result')
        self.assertEqual(message.reply.call_args.kwargs['content'], 'Normal result')
        status.delete.assert_awaited_once()
        self.assertTrue(any(t.inbound_seq == 30 and terminal for t, _, terminal in self.observed))


if __name__ == '__main__':
    unittest.main()
