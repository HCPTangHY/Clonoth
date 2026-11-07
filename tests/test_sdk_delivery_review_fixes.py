"""Actual SDK/Discord callbacks with controlled network failure and await order."""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from adapters.discord import agent, messaging
from adapters.discord.callbacks import DiscordCallbacks
from test_discord_forward_migration import message, runtime, trigger
import test_sdk_discord_progress_integration as progress_fixture


def background_case():
    case = progress_fixture.BackgroundProgressIntegrationTests()
    case.setUp()
    case.rt.restart_signal = "[BOT_RESTART]"
    case.rt.react_pattern = re.compile(r"\[REACT:(.*?)\]")
    case.rt.split_signal = "[SPLIT]"
    case.rt.entry_node_id = "main"
    case.rt.child_node_display_names = {}
    return case


class DeliveryReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_intermediate_limit_applies_per_segment_not_whole_payload(self):
        rt = runtime()
        msg = message()
        trig = trigger(rt, msg)
        with patch.object(messaging.asyncio, "sleep", new_callable=AsyncMock):
            await DiscordCallbacks(rt).send_intermediate_reply(
                trig, "a" * 1000 + "[SPLIT]" + "b" * 1000, [],
            )
        self.assertEqual(msg.reply.call_args.kwargs["content"], "a" * 1000)
        self.assertEqual(msg.channel.send.call_args.kwargs["content"], "b" * 1000)

    async def test_real_channel_send_failure_is_not_settled_as_success(self):
        for missing in (False, True):
            with self.subTest(missing_channel=missing):
                case = background_case()
                destination = NS(id=42, guild=None, send=AsyncMock(side_effect=RuntimeError("offline")))
                case.rt.dc_client.get_channel = Mock(side_effect=lambda cid: (
                    case.log_channel if cid == 99 else None if missing else destination
                ))
                case.rt.dc_client.fetch_channel.side_effect = RuntimeError("missing channel")
                with patch.object(case.cb, "_should_edit", return_value=True):
                    await case.event("node_started")
                    with self.assertLogs("clonoth_sdk.event_router", level="ERROR"):
                        await case.event("outbound_message", text="answer")
                status = case.log_message.edit.call_args.kwargs["content"]
                self.assertIn("回复发送失败", status)
                self.assertNotIn("✓ 完成", status)
                self.assertFalse(case.state.triggers)
                self.assertFalse(case.state.main_task_states)
                self.assertEqual(destination.send.await_count, 0 if missing else 1)
                case.assert_owned_state_released()

    async def test_partial_split_failure_is_reported_without_automatic_retry(self):
        case = background_case()
        destination = NS(id=42, guild=None, send=AsyncMock(side_effect=[NS(id=902), RuntimeError("offline")]))
        case.rt.dc_client.get_channel = Mock(side_effect=lambda cid: case.log_channel if cid == 99 else destination)
        with patch.object(case.cb, "_should_edit", return_value=True), \
             patch.object(messaging.asyncio, "sleep", new_callable=AsyncMock):
            await case.event("node_started")
            with self.assertLogs("clonoth_sdk.event_router", level="ERROR"):
                await case.event("outbound_message", text="first[SPLIT]second")
        self.assertEqual(destination.send.await_count, 2)
        self.assertIn("回复发送失败", case.log_message.edit.call_args.kwargs["content"])
        self.assertFalse(case.state.triggers)

    async def test_successful_delivery_keeps_success_status(self):
        case = background_case()
        destination = NS(id=42, guild=None, send=AsyncMock(return_value=NS(id=902)))
        case.rt.dc_client.get_channel = Mock(side_effect=lambda cid: case.log_channel if cid == 99 else destination)
        with patch.object(case.cb, "_should_edit", return_value=True):
            await case.event("node_started")
            await case.event("outbound_message", text="answer")
        destination.send.assert_awaited_once()
        self.assertIn("✓ 完成", case.log_message.edit.call_args.kwargs["content"])

    async def test_bad_destination_is_reported_not_silently_skipped(self):
        case = background_case()
        with self.assertRaises(ValueError):
            await case.cb.send_to_channel("web:42", "answer", [])

    async def test_preempt_completion_while_deleting_old_card_cleans_new_card(self):
        rt = runtime()
        current = message(mid=22)
        old_message = message(mid=21, channel=current.channel)
        old_trigger = trigger(rt, old_message)
        old_status = NS(id=321, delete=AsyncMock())

        async def delete_old():
            rt.session_state.consume_trigger(7)

        old_status.delete.side_effect = delete_old
        old_trigger.platform_data["status_msg"] = old_status
        rt.clonoth_client.get_running_tasks.return_value = [
            NS(task_id="task", is_user_entry=True, source_inbound_seq=7),
        ]
        with patch.object(agent, "_collect_attachments", AsyncMock(return_value=[])), \
             patch.object(agent, "_derive_initial_workspace", return_value=None):
            await agent._handle_agent_inner(rt, current, "new input", 10, "discord:10")
        current.reply.return_value.delete.assert_awaited_once()
        self.assertIsNone(rt.session_state.get_trigger(7))
        self.assertIs(old_trigger.platform_data["message"], old_message)
        rt.clonoth_client.submit_inbound.assert_not_awaited()

    async def test_preempt_replaced_trigger_is_not_overwritten_after_delete(self):
        rt = runtime()
        current = message(mid=22)
        previous = trigger(rt, message(mid=21, channel=current.channel))
        replacement = None

        async def delete_old():
            nonlocal replacement
            replacement = trigger(rt, message(mid=23, channel=current.channel))
            raise RuntimeError("old card already deleted")

        previous.platform_data["status_msg"] = NS(id=321, delete=AsyncMock(side_effect=delete_old))
        rt.clonoth_client.get_running_tasks.return_value = [
            NS(task_id="task", is_user_entry=True, source_inbound_seq=7),
        ]
        with patch.object(agent, "_collect_attachments", AsyncMock(return_value=[])):
            await agent._handle_agent_inner(rt, current, "new input", 10, "discord:10")
        current.reply.return_value.delete.assert_awaited_once()
        self.assertIs(rt.session_state.get_trigger(7), replacement)
        self.assertEqual(replacement.platform_data["message"].id, 23)
        rt.clonoth_client.submit_inbound.assert_not_awaited()

    async def test_late_log_send_is_settled_after_terminal_cleanup(self):
        for kind, terminal in (("outbound_message", "✓ 完成"),
                               ("task_cancelled", "⚠️ 已取消"),
                               ("task_preempted", "⚡ 被打断"),
                               ("context_reset", "🔄 上下文已重置")):
            with self.subTest(kind=kind):
                case = background_case()
                case.state.register_trigger(case.trigger)
                state = case.state.get_or_create_main_state(10)
                state.progress_records.append("working")
                started, release = asyncio.Event(), asyncio.Event()

                async def slow_send(*args, **kwargs):
                    started.set()
                    await release.wait()
                    return case.log_message

                case.log_channel.send.side_effect = slow_send
                with patch.object(case.cb, "_should_edit", return_value=True), \
                     patch.object(case.cb, "send_to_channel", new_callable=AsyncMock):
                    sweep = asyncio.create_task(case.router._refresh_active_states())
                    try:
                        await asyncio.wait_for(started.wait(), 2)
                        await case.event(kind, text="done", conversation_key="discord:42", reason="clear")
                    finally:
                        release.set()
                        await sweep
                    await case.cb.update_progress(case.trigger, state)
                self.assertFalse(case.state.triggers)
                self.assertFalse(case.state.main_task_states)
                self.assertIs(state.platform_data["log_msg"], case.log_message)
                self.assertIn(terminal, case.log_message.edit.call_args.kwargs["content"])
                case.log_channel.send.assert_awaited_once()
                case.assert_owned_state_released()

    async def test_late_log_edit_cannot_overwrite_terminal_status(self):
        case = background_case()
        case.state.register_trigger(case.trigger)
        state = case.state.get_or_create_main_state(10)
        state.progress_records.append("working")
        state.platform_data["log_msg"] = case.log_message
        started, release = asyncio.Event(), asyncio.Event()
        contents = []

        async def slow_edit(*, content):
            if "✓ 完成" not in content:
                started.set()
                await release.wait()
            contents.append(content)

        case.log_message.edit.side_effect = slow_edit
        with patch.object(case.cb, "_should_edit", return_value=True), \
             patch.object(case.cb, "send_to_channel", new_callable=AsyncMock):
            sweep = asyncio.create_task(case.router._refresh_active_states())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await case.event("outbound_message", text="done")
            finally:
                release.set()
                await sweep
        self.assertIn("✓ 完成", contents[-1])
        case.assert_owned_state_released()


if __name__ == "__main__":
    unittest.main()
