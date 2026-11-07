"""Real SDK/Discord callback regressions; only network operations are mocked."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from adapters.discord import agent, messaging
from adapters.discord.callbacks import DiscordCallbacks
from test_discord_forward_migration import runtime, message, trigger
import test_sdk_discord_progress_integration as integration


def background_case():
    case = integration.BackgroundProgressIntegrationTests()
    case.setUp()
    case.rt.restart_signal = '[BOT_RESTART]'
    case.rt.react_pattern = re.compile(r'\[REACT:(.*?)\]')
    case.rt.split_signal = '[SPLIT]'
    case.rt.entry_node_id = 'main'
    case.rt.child_node_display_names = {}
    destination = NS(id=42, guild=None, send=AsyncMock(return_value=NS(id=1001)))
    case.rt.dc_client.get_channel = Mock(
        side_effect=lambda cid: case.log_channel if cid == 99 else destination)
    return case, destination


class AdapterReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_intermediate_limits_each_segment_not_the_whole_payload(self):
        rt = runtime()
        msg = message()
        incoming = trigger(rt, msg)
        raw = 'a' * 1000 + '[SPLIT]' + 'b' * 1000
        with patch.object(messaging.asyncio, 'sleep', new_callable=AsyncMock):
            await DiscordCallbacks(rt).send_intermediate_reply(incoming, raw, [])
        self.assertEqual(msg.reply.call_args.kwargs['content'], 'a' * 1000)
        self.assertEqual(msg.channel.send.call_args.kwargs['content'], 'b' * 1000)

    async def test_real_send_failure_marks_failure_without_requeue(self):
        case, destination = background_case()
        destination.send.side_effect = RuntimeError('network unavailable')
        with patch.object(case.cb, '_should_edit', return_value=True):
            await case.event('node_started')
            with self.assertLogs('clonoth_sdk.event_router', level='ERROR'):
                await case.event('outbound_message', text='answer')
        self.assertIn('回复发送失败', case.log_message.edit.call_args.kwargs['content'])
        destination.send.assert_awaited_once()
        self.assertFalse(case.state.triggers)
        self.assertFalse(case.state.main_task_states)
        case.assert_owned_state_released()

    async def test_missing_channel_marks_failure(self):
        case, destination = background_case()
        case.rt.dc_client.get_channel = Mock(
            side_effect=lambda cid: case.log_channel if cid == 99 else None)
        case.rt.dc_client.fetch_channel = AsyncMock(side_effect=RuntimeError('not found'))
        await case.event('node_started')
        with self.assertLogs('clonoth_sdk.event_router', level='ERROR'):
            await case.event('outbound_message', text='answer')
        self.assertIn('回复发送失败', case.log_message.edit.call_args.kwargs['content'])
        destination.send.assert_not_awaited()
        self.assertFalse(case.state.triggers)

    async def test_successful_channel_delivery_still_completes(self):
        case, destination = background_case()
        await case.event('node_started')
        await case.event('outbound_message', text='answer')
        destination.send.assert_awaited_once()
        self.assertIn('✓ 完成', case.log_message.edit.call_args.kwargs['content'])

    async def test_preempt_completion_during_old_card_delete_removes_new_card(self):
        rt = runtime()
        current = message(mid=22)
        old = message(mid=21, channel=current.channel)
        incoming = trigger(rt, old)
        old_status = NS(id=321, delete=AsyncMock())

        async def delete_old():
            rt.session_state.consume_trigger(7)

        old_status.delete.side_effect = delete_old
        incoming.platform_data['status_msg'] = old_status
        rt.clonoth_client.get_running_tasks.return_value = [
            NS(task_id='task', is_user_entry=True, source_inbound_seq=7)]
        with patch.object(agent, '_collect_attachments', AsyncMock(return_value=[])), \
             patch.object(agent, '_derive_initial_workspace', return_value=None):
            await agent._handle_agent_inner(rt, current, 'new input', 10, 'discord:10')
        self.assertIsNone(rt.session_state.get_trigger(7))
        current.reply.return_value.delete.assert_awaited_once()
        rt.clonoth_client.submit_inbound.assert_not_awaited()
        self.assertIs(incoming.platform_data['message'], old)

    async def test_late_progress_send_is_settled_after_terminal_cleanup(self):
        case, destination = background_case()
        case.state.register_trigger(case.trigger)
        state = case.state.get_or_create_main_state(10)
        state.progress_records.append('working')
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_send(*args, **kwargs):
            started.set()
            await release.wait()
            return case.log_message

        case.log_channel.send.side_effect = slow_send
        with patch.object(case.cb, '_should_edit', return_value=True):
            sweep = asyncio.create_task(case.router._refresh_active_states())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await case.event('outbound_message', text='done')
            finally:
                release.set()
                await asyncio.wait_for(sweep, 2)
        self.assertFalse(case.state.triggers)
        self.assertFalse(case.state.main_task_states)
        self.assertIs(state.platform_data['log_msg'], case.log_message)
        self.assertIn('✓ 完成', case.log_message.edit.call_args.kwargs['content'])
        case.assert_owned_state_released()
        before = case.log_message.edit.await_count
        await case.cb.update_progress(case.trigger, state)
        self.assertEqual(case.log_message.edit.await_count, before)
        case.assert_owned_state_released()

    async def test_late_progress_edit_cannot_restore_running_status(self):
        case, destination = background_case()
        await case.event('node_started')
        started, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def slow_edit(*args, **kwargs):
            content = kwargs['content']
            if '✓ 完成' not in content:
                started.set()
                await release.wait()
            delivered.append(content)

        case.log_message.edit.side_effect = slow_edit
        with patch.object(case.cb, '_should_edit', return_value=True):
            sweep = asyncio.create_task(case.router._refresh_active_states())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await case.event('outbound_message', text='done')
            finally:
                release.set()
                await asyncio.wait_for(sweep, 2)
        self.assertIn('✓ 完成', delivered[-1])
        self.assertFalse(case.state.main_task_states)
        case.assert_owned_state_released()
