"""Planned restart keeps SDK delivery bindings and Discord's existing progress cards."""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from adapters.discord.callbacks import DiscordCallbacks
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import ChildTaskState, MainTaskState, SessionState, TriggerInfo
from clonoth_sdk.types import Event


def message(mid):
    return SimpleNamespace(id=mid, channel=SimpleNamespace(id=42), edit=AsyncMock(), delete=AsyncMock())


class RestartDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = SessionState()
        self.state.register_session('discord:42', 'session')
        self.status, self.log, self.child_log = message(1), message(2), message(3)
        self.original = message(4)
        self.rt = SimpleNamespace(
            session_state=self.state, agent_log_channel_id=99,
            dc_client=SimpleNamespace(get_channel=Mock(), fetch_channel=AsyncMock()),
            transient_msg_ids=deque(maxlen=100), channel_history={}, restart_signal='[BOT_RESTART]',
        )
        self.callbacks = DiscordCallbacks(self.rt)
        self.router = EventRouter(None, self.state, self.callbacks, BotConfig(
            base_url='http://offline.invalid', entry_node_id='main', conversation_key_prefix='discord',
        ))
        self.trigger = TriggerInfo(10, 'discord:42', 'session', False, task_id='root',
            platform_data={'status_msg': self.status, 'message': self.original, 'channel_id': 42,
                           'cancel_view': object(), 'last_typing_time': 1})
        self.state.register_trigger(self.trigger)
        self.main = MainTaskState(progress_records=['Completed first tool'], stream_parts=['unfinished'],
                                  platform_data={'log_msg': self.log})
        self.child = ChildTaskState(lines=['Child working'], prefix='Worker',
                                   platform_data={'msg': self.child_log})
        self.state.main_task_states[10] = self.main
        self.state.child_task_states['child'] = self.child
        self.state.pending_watermarks[10] = (42, 7)
        self.router._task_callers['child'] = 'root'
        self.callbacks._dot_states[10] = {'text_preview': 'partial root'}
        self.callbacks._child_dot_states['child'] = {'text_preview': 'partial child'}
        self.callbacks._last_edit_time.update({10: 1, 'log:10': 1, 'child': 1})

    async def event(self, kind, **payload):
        await self.router._dispatch(Event.from_dict({
            'seq': 100, 'type': kind, 'session_id': 'session',
            'payload': payload,
        }))

    async def stopping(self):
        await self.event('engine_restarting', preserve_tasks=True, restart_id='restart-1')

    async def restored(self, worker='worker-1', generation='new-1'):
        await self.event('engine_registered', worker_id=worker, generation_id=generation,
                         preserve_tasks=True, restart_id='restart-1')

    def assert_bindings_preserved(self):
        self.assertIs(self.state.get_trigger(10), self.trigger)
        self.assertIs(self.state.get_main_state(10), self.main)
        self.assertIs(self.state.get_child_state('child'), self.child)
        self.assertEqual(self.state.pending_watermarks[10], (42, 7))
        self.assertEqual(self.router._task_callers['child'], 'root')
        self.assertNotIn('terminal_status', self.main.platform_data)
        self.assertNotIn('terminal_status', self.child.platform_data)
        self.status.delete.assert_not_awaited()
        self.child_log.delete.assert_not_awaited()

    async def test_planned_stop_preserves_cards_bindings_and_clears_partial_streams(self):
        await self.stopping()
        self.assert_bindings_preserved()
        self.assertEqual(self.main.stream_parts, [])
        self.assertEqual(self.main.progress_records, ['Completed first tool'])
        self.assertFalse(self.callbacks._dot_states)
        self.assertFalse(self.callbacks._child_dot_states)
        self.assertFalse(self.callbacks._last_edit_time)
        self.assertNotIn('last_typing_time', self.trigger.platform_data)
        self.assertIn('暂时中断', self.status.edit.call_args.kwargs['content'])
        self.assertNotIn('view', self.status.edit.call_args.kwargs)
        self.assertIn('暂时中断', self.log.edit.call_args.kwargs['content'])
        self.assertIn('暂时中断', self.child_log.edit.call_args.kwargs['content'])

    async def test_downtime_does_not_refresh_typing_or_expire_trigger_then_restore_refreshes_ttl(self):
        await self.stopping()
        self.trigger.created_at = 0
        with patch.object(self.callbacks, 'refresh_typing', AsyncMock()) as typing:
            await self.router._sweep_once()
            typing.assert_not_awaited()
            self.assertIs(self.state.get_trigger(10), self.trigger)
            await self.restored()
            self.assertGreater(self.trigger.created_at, 0)
            self.assertIn('任务继续执行', self.status.edit.call_args.kwargs['content'])
            await self.router._sweep_once()
            typing.assert_awaited_once_with(self.trigger)
        self.assert_bindings_preserved()

    async def test_multiple_workers_duplicate_and_late_notifications_do_not_clear_or_reset_progress(self):
        await self.event('engine_registered', worker_id='worker-1', generation_id='old-1')
        await self.event('engine_registered', worker_id='worker-2', generation_id='old-2')
        self.status.edit.assert_not_awaited()
        await self.stopping()
        await self.restored()
        self.main.stream_parts.append('new execution text')
        edits = self.status.edit.await_count
        await self.restored('worker-2', 'new-2')
        await self.restored()
        await self.stopping()  # A replayed pre-restart event must not pause restored work.
        self.assertEqual(self.status.edit.await_count, edits)
        self.assertEqual(self.main.stream_parts, ['new execution text'])
        self.assert_bindings_preserved()
        self.assertEqual(self.router._planned_restart_phase, 'restored')

    async def test_original_source_sequence_delivers_once_after_planned_restart(self):
        await self.stopping()
        await self.restored()
        await self.restored('worker-2', 'new-2')
        with patch('adapters.discord.callbacks._send_split_text', AsyncMock(return_value=[])) as send, \
             patch('adapters.discord.callbacks._extract_reactions', return_value=('Finished', [])):
            await self.event('outbound_message', source_inbound_seq=10, task_id='root', node_id='main', text='Finished')
            send.assert_awaited_once()
            self.assertIs(send.call_args.kwargs['reply_to'], self.original)
        self.assertNotIn(10, self.state.triggers)
        self.assertNotIn(10, self.state.main_task_states)
        self.status.delete.assert_awaited_once()
        self.assertEqual(self.main.platform_data['terminal_status'], '✓ 完成')

    async def test_user_cancel_during_restart_stays_terminal_and_is_not_recreated(self):
        await self.stopping()
        await self.event('task_cancelled', source_inbound_seq=10, task_id='root', node_id='main',
                         cancel_reason='user_cancel')
        self.assertNotIn(10, self.state.triggers)
        self.assertNotIn(10, self.state.main_task_states)
        self.assertIn('取消', self.status.edit.call_args.kwargs['content'])
        edits = self.status.edit.await_count
        await self.restored()
        self.assertNotIn(10, self.state.triggers)
        self.assertEqual(self.status.edit.await_count, edits)

    async def test_unplanned_restart_still_cleans_up(self):
        await self.event('engine_registered', worker_id='worker-1', generation_id='old')
        await self.event('engine_registered', worker_id='worker-1', generation_id='new')
        self.assertFalse(self.state.triggers)
        self.assertFalse(self.state.main_task_states)
        self.assertFalse(self.state.child_task_states)
        self.assertIn('后端已重启', self.main.platform_data['terminal_status'])

    async def test_missing_explicit_preservation_or_restart_id_uses_legacy_cleanup(self):
        await self.event('engine_restarting', preserve_tasks=True)
        self.assertFalse(self.state.triggers)
        self.assertIn('后端已重启', self.status.edit.call_args.kwargs['content'])

    async def test_missing_stopping_event_can_recover_from_explicit_registered_event(self):
        self.trigger.created_at = 0
        await self.restored()
        self.assert_bindings_preserved()
        self.assertGreater(self.trigger.created_at, 0)
        self.assertIn('任务继续执行', self.status.edit.call_args.kwargs['content'])


if __name__ == '__main__':
    unittest.main()
