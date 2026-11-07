"""Offline SDK-to-Discord background progress lifecycle checks."""
from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from adapters.discord.callbacks import DiscordCallbacks
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import MainTaskState, SessionState, TriggerInfo
from clonoth_sdk.types import Event


class BackgroundProgressIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = SessionState()
        self.state.register_session("discord:42", "session")
        self.state.register_dm_channel("session", 42)
        self.log_message = SimpleNamespace(id=900, edit=AsyncMock())
        self.log_channel = SimpleNamespace(id=99, send=AsyncMock(return_value=self.log_message))
        self.rt = SimpleNamespace(
            session_state=self.state, agent_log_channel_id=99,
            dc_client=SimpleNamespace(get_channel=Mock(return_value=self.log_channel),
                                      fetch_channel=AsyncMock()),
            transient_msg_ids=deque(maxlen=1000), channel_history={},
        )
        self.cb = DiscordCallbacks(self.rt)
        self.router = EventRouter(None, self.state, self.cb, BotConfig(
            base_url="http://offline.invalid", entry_node_id="main", conversation_key_prefix="discord",
        ))
        self.trigger = TriggerInfo(10, "discord:42", "session", True,
                                   platform_data={"background": True})

    async def event(self, kind, **payload):
        await self.router._dispatch(Event.from_dict({
            "seq": 1, "type": kind, "session_id": "session",
            "payload": {"node_id": "main", "task_id": "root", "source_inbound_seq": 10, **payload},
        }))

    def seed_owned_state(self):
        self.cb._dot_states.update({10: {}, 20: {"text_preview": "other"}})
        self.cb._last_edit_time.update({10: 1, "log:10": 2, 20: 3, "log:20": 4})

    def assert_owned_state_released(self):
        self.assertNotIn(10, self.cb._dot_states)
        self.assertNotIn(10, self.cb._last_edit_time)
        self.assertNotIn("log:10", self.cb._last_edit_time)

    async def test_finalize_settles_card_without_removing_transient_filter(self):
        state = MainTaskState(progress_records=["Working"], platform_data={"log_msg": self.log_message})
        self.rt.transient_msg_ids.append(900)
        self.seed_owned_state()
        await self.cb.finalize_main_progress(self.trigger, state, "done")
        self.assertIn("done", self.log_message.edit.call_args.kwargs["content"])
        self.assert_owned_state_released()
        self.assertEqual(self.cb._dot_states[20], {"text_preview": "other"})
        self.assertEqual(self.cb._last_edit_time, {20: 3, "log:20": 4})
        self.assertIn(900, self.rt.transient_msg_ids)

    async def test_missing_state_cleanup_is_repeatable(self):
        self.seed_owned_state()
        await self.cb.finalize_main_progress(self.trigger, None, "done")
        await self.cb.finalize_main_progress(self.trigger, None, "done")
        self.assert_owned_state_released()
        self.log_message.edit.assert_not_awaited()

    async def test_failed_settlement_and_cancellation_still_release_resources(self):
        for error in (RuntimeError("offline edit failure"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.seed_owned_state()
                with patch.object(self.cb, "_settle_log_msg", AsyncMock(side_effect=error)):
                    with self.assertRaises(type(error)):
                        await self.cb.finalize_main_progress(self.trigger, MainTaskState(), "done")
                self.assert_owned_state_released()

    async def test_background_dm_creates_then_updates_monitor_log(self):
        state = MainTaskState(progress_records=["Working"])
        with patch.object(self.cb, "_should_edit", return_value=True):
            await self.cb.update_progress(self.trigger, state)
            await self.cb.update_progress(self.trigger, state)
        self.log_channel.send.assert_awaited_once()
        self.log_message.edit.assert_awaited_once()
        self.assertIs(state.platform_data["log_msg"], self.log_message)
        self.assertIn(900, self.rt.transient_msg_ids)
        self.rt.dc_client.fetch_channel.assert_not_awaited()

    async def test_user_dm_keeps_existing_status_message_path(self):
        status = SimpleNamespace(channel=SimpleNamespace(id=42), edit=AsyncMock())
        self.trigger.platform_data = {"status_msg": status}
        with patch.object(self.cb, "_should_edit", return_value=True):
            await self.cb.update_progress(self.trigger, MainTaskState(progress_records=["Working"]))
        status.edit.assert_awaited_once()
        self.log_channel.send.assert_not_awaited()

    async def test_sdk_terminal_events_settle_real_adapter_state(self):
        scenarios = (("outbound_message", "✓ 完成"), ("task_cancelled", "⚠️ 已取消"),
                     ("task_preempted", "⚡ 被打断"), ("cancel_requested", "⚠️ 已取消"))
        for kind, status in scenarios:
            with self.subTest(event=kind):
                self.log_message.edit.reset_mock()
                with patch.object(self.cb, "_should_edit", return_value=True), \
                     patch.object(self.cb, "send_to_channel", new_callable=AsyncMock):
                    await self.event("node_started", node_name="Main")
                    self.assertIs(self.state.get_main_state(10).platform_data["log_msg"], self.log_message)
                    await self.event(kind, text="Answer")
                self.assertIn(status, self.log_message.edit.call_args.kwargs["content"])
                self.assert_owned_state_released()
                self.assertFalse(self.state.triggers)
                self.assertFalse(self.state.main_task_states)

    async def test_sdk_expiry_and_reset_settle_once(self):
        for kind, status in (("expiry", "⚠️ 请求超时"), ("context_reset", "🔄 上下文已重置")):
            with self.subTest(event=kind):
                with patch.object(self.cb, "_should_edit", return_value=True):
                    await self.event("node_started", node_name="Main")
                self.log_message.edit.reset_mock()
                if kind == "expiry":
                    self.state.get_trigger(10).created_at = 0
                    await self.router._cleanup_stale_triggers()
                else:
                    await self.event(kind, conversation_key="discord:42", reason="clear")
                self.log_message.edit.assert_awaited_once()
                self.assertIn(status, self.log_message.edit.call_args.kwargs["content"])
                self.assert_owned_state_released()
                self.assertFalse(self.state.triggers)
                self.assertFalse(self.state.main_task_states)

    async def test_sdk_send_failure_settles_failure_not_success(self):
        with patch.object(self.cb, "send_to_channel", AsyncMock(side_effect=RuntimeError("offline failure"))):
            await self.event("node_started", node_name="Main")
            with self.assertLogs("clonoth_sdk.event_router", level="ERROR"):
                await self.event("outbound_message", text="Answer")
        self.assertIn("回复发送失败", self.log_message.edit.call_args.kwargs["content"])
        self.assert_owned_state_released()
        self.assertFalse(self.state.triggers)


if __name__ == "__main__":
    unittest.main()
