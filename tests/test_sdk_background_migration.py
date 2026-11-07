"""Background entry routing stays platform-neutral; no network or filesystem IO."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState, TriggerInfo
from clonoth_sdk.types import Event


class Callbacks:
    def __init__(self):
        for name in ("update_progress", "create_child_progress", "update_child_progress",
                     "send_reply", "send_to_channel", "send_intermediate_reply",
                     "send_response_notice", "edit_status_message", "delete_status_message",
                     "on_context_reset", "finalize_child_progress", "finalize_main_progress"):
            setattr(self, name, AsyncMock())


class BackgroundRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = SessionState()
        self.state.register_session("test:room", "session")
        self.cb = Callbacks()
        self.router = EventRouter(None, self.state, self.cb, BotConfig(
            base_url="http://offline.invalid", entry_node_id="main", conversation_key_prefix="test",
        ))

    async def event(self, kind, **payload):
        await self.router._dispatch(Event.from_dict({
            "seq": 1, "type": kind, "session_id": payload.pop("session_id", "session"),
            "payload": {"node_id": "main", "task_id": "root", "source_inbound_seq": 10, **payload},
        }))

    async def test_background_output_chain_uses_one_main_card_and_channel_send(self):
        await self.event("node_started", node_name="Main")
        trigger = self.state.get_trigger(10)
        self.assertTrue(trigger.platform_data["background"])
        self.assertEqual(trigger.task_id, "root")
        await self.event("handoff_progress", message="Working")
        await self.event("node_completed", node_name="Main")
        await self.event("task_completed")
        self.cb.finalize_main_progress.assert_not_called()
        await self.event("task_started", task_id="output", caller_task_id="root", node_id="formatter")
        await self.event("node_started", task_id="output", node_id="formatter")
        await self.event("node_completed", task_id="output", node_id="formatter")
        await self.event("outbound_message", task_id="output", node_id="formatter", reply_node_id="main", text="Answer")
        self.cb.send_to_channel.assert_awaited_once_with("test:room", "Answer", [], node_id="main")
        self.cb.send_reply.assert_not_called()
        self.cb.create_child_progress.assert_not_called()
        self.cb.finalize_main_progress.assert_awaited_once()
        args = self.cb.finalize_main_progress.call_args.args
        self.assertIs(args[0], trigger)
        self.assertEqual(len(args[1].progress_records), 5)
        self.assertEqual(args[2], "✓ 完成")
        self.assertFalse(self.state.triggers)
        self.assertFalse(self.state.main_task_states)

    async def test_intermediate_and_cancel_never_need_platform_message(self):
        await self.event("handoff_progress", message="Started")
        main_state = self.state.get_main_state(10)
        main_state.stream_parts.append("partial")
        await self.event("intermediate_reply", text="Progress")
        self.assertEqual(main_state.stream_parts, [])
        self.cb.send_to_channel.assert_awaited_once_with("test:room", "Progress", [], node_id="main")
        self.cb.send_intermediate_reply.assert_not_called()
        await self.event("task_cancelled")
        await self.event("task_cancelled")
        self.cb.finalize_main_progress.assert_awaited_once()
        self.assertEqual(self.cb.finalize_main_progress.call_args.args[2], "⚠️ 已取消")
        self.cb.edit_status_message.assert_not_called()

    async def test_normal_trigger_keeps_reply_path(self):
        trigger = TriggerInfo(10, "test:room", "session", False, platform_data={"message": object()})
        self.state.register_trigger(trigger)
        await self.event("node_started")
        self.assertIs(self.state.get_trigger(10), trigger)
        await self.event("intermediate_reply", text="Progress")
        await self.event("outbound_message", text="Answer")
        self.cb.send_reply.assert_awaited_once()
        self.cb.send_intermediate_reply.assert_awaited_once()
        self.cb.send_to_channel.assert_not_called()
        self.cb.finalize_main_progress.assert_not_called()

    async def test_foreign_missing_sequence_and_nonentry_do_not_create_background(self):
        self.state.register_session("other:room", "foreign")
        await self.event("node_started", session_id="foreign")
        await self.event("node_started", source_inbound_seq=0)
        await self.event("node_started", node_id="system.worker")
        await self.event("node_started", node_id="child")
        self.assertFalse(self.state.triggers)
        self.assertFalse(self.state.main_task_states)

    async def test_concurrent_sources_are_independent_and_completion_does_not_create(self):
        await self.event("node_started")
        await self.event("node_started", source_inbound_seq=20, task_id="other")
        await self.event("task_cancelled")
        self.assertEqual(set(self.state.triggers), {20})
        await self.event("node_completed")
        self.assertEqual(set(self.state.triggers), {20})
        self.assertEqual(set(self.state.main_task_states), {20})

    async def test_parent_route_and_dm_are_used_without_session_map(self):
        self.state.register_dm_channel("session", 42)
        await self.event("node_started", session_id="branch", parent_conversation_key="test:room")
        self.assertTrue(self.state.get_trigger(10).is_dm)
        info = {"notices": [{"text": "Notice"}]}
        await self.event("outbound_message", session_id="branch", text="Answer", response_info=info)
        self.cb.send_to_channel.assert_awaited_once_with("test:room", "Answer", [], node_id="main")
        self.cb.send_response_notice.assert_awaited_once()
        self.assertEqual(self.cb.send_response_notice.call_args.args[1], "test:room")

    async def test_send_failure_still_settles_but_does_not_report_success(self):
        await self.event("node_started")
        self.cb.send_to_channel.side_effect = RuntimeError("offline failure")
        with self.assertLogs("clonoth_sdk.event_router", level="ERROR"):
            await self.event("outbound_message", text="Answer", response_info={"notices": ["n"]})
        self.assertFalse(self.state.triggers)
        self.assertEqual(self.cb.finalize_main_progress.call_args.args[2], "⚠️ 回复发送失败")
        self.cb.send_response_notice.assert_not_called()

    async def test_optional_callback_and_empty_output(self):
        del self.cb.finalize_main_progress
        await self.event("node_started")
        await self.event("outbound_message", text="")
        self.assertFalse(self.state.triggers)
        self.cb.send_to_channel.assert_not_called()

    async def test_expiry_preempt_reset_and_cancel_requested_settle_once(self):
        for kind in ("expiry", "task_preempted", "context_reset", "cancel_requested"):
            with self.subTest(kind=kind):
                self.cb.finalize_main_progress.reset_mock()
                await self.event("node_started")
                if kind == "expiry":
                    self.state.get_trigger(10).created_at = 0
                    await self.router._cleanup_stale_triggers()
                elif kind == "context_reset":
                    await self.event(kind, conversation_key="test:room", reason="clear")
                    self.state.register_session("test:room", "session")
                else:
                    await self.event(kind)
                self.cb.finalize_main_progress.assert_awaited_once()
                self.assertIsNotNone(self.cb.finalize_main_progress.call_args.args[1])
                self.assertFalse(self.state.triggers)
                self.assertFalse(self.state.main_task_states)
        self.cb.edit_status_message.assert_not_called()
        self.cb.delete_status_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
