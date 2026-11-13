"""Read-only Goal card lifecycle and coexistence with ordinary Discord requests.

Run with TMPDIR/--basetemp under data/temp/goal-discord-status.
"""
from __future__ import annotations

import asyncio
from collections import deque
import copy
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx

from adapters.discord import callbacks as callback_module
from adapters.discord.agent import CancelView
from adapters.discord.callbacks import DiscordCallbacks
from adapters.discord.extensions import ide_goal, ide_goal_status as status_module
from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState, TriggerInfo
from clonoth_sdk.types import Event


def record(**extra):
    return {"session_id": "session", "goal_id": "goal-one", "revision": 3, "goal_version": 1,
            "goal": "Run the checker", "status": "running", "rounds": 0, "max_rounds": 8,
            "pause_reason": "", "capabilities": {"create": True, "progress": True},
            "execution": {"task_id": "root", "source_inbound_seq": 10, "started_at": 1000.0}, **extra}


def fields(embed):
    return {field.name: field.value for field in embed.fields}


def display(embed):
    return "\n".join([embed.title or "", embed.description or "", *(field.value for field in embed.fields)])


def trigger(seq=10, *, background=True, ck="discord:42", task_id="root"):
    return TriggerInfo(seq, ck, "session", False, task_id=task_id,
                       platform_data={"background": True} if background else {})


class GoalCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = SessionState()
        self.state.register_session("discord:42", "session")
        self.channel = NS(id=42, guild=None, send=AsyncMock(return_value=NS(id=999)))
        self.message = NS(id=900, channel=self.channel, edit=AsyncMock(), delete=AsyncMock())
        self.log_message = NS(id=901, edit=AsyncMock())
        self.log_channel = NS(id=99, send=AsyncMock(return_value=self.log_message))
        self.current = record()
        self.requests = []
        self.http_error = None
        self.client = ClonothClient("https://offline.invalid")
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        self.rt = NS(session_state=self.state, agent_log_channel_id=99, entry_node_id="main",
                     clonoth_client=self.client, superusers={1}, workspace=Path("."),
                     dc_client=NS(get_channel=lambda cid: self.channel if cid == 42 else self.log_channel,
                                  fetch_channel=AsyncMock()),
                     channel_history={}, history_seq_counter=0, history_max_len=15,
                     transient_msg_ids=deque(maxlen=1000), split_signal="[SPLIT]", restart_signal="[BOT_RESTART]",
                     react_pattern=re.compile(r"\[REACT:(.*?)\]"), child_node_display_names={})
        self.rt.callbacks = DiscordCallbacks(self.rt)
        self.allow_edit = patch.object(self.rt.callbacks, "_should_edit", return_value=True)
        self.allow_edit.start()
        self.monitors = []

    async def asyncTearDown(self):
        for card in self.monitors:
            card.close()
            if card.task:
                await asyncio.gather(card.task, return_exceptions=True)
        self.allow_edit.stop()
        await self.client.close()

    async def http(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET", "status card must never mutate the backend")
        self.assertEqual(request.url.path, "/v1/plugins/ide/goal")
        self.assertEqual(request.url.params["session_id"], "session")
        if isinstance(self.http_error, int):
            return httpx.Response(self.http_error, json={"detail": "offline"})
        if self.http_error:
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(200, json=self.current)

    def monitor(self, *, bound=True):
        card = status_module.prepare(self.rt, self.message, "session", record())
        self.assertIsInstance(card, status_module.GoalStatusCard)
        self.monitors.append(card)
        if bound:
            card.data = record()
            card.goal_id = "goal-one"
        return card

    async def test_prepare_is_inert_until_ack_and_first_progress_can_arrive_before_post_reply(self):
        card = self.monitor(bound=False)
        card.observe(trigger(), "early tool ✓", False)
        self.assertIsNone(card.data)
        self.assertIsNone(card.task)
        self.message.edit.assert_not_awaited()
        self.assertFalse(self.requests)
        with patch.object(card, "run", AsyncMock()):
            self.assertTrue(card.start(record()))
            await card.task
        await card.render()
        self.assertIn("early tool", display(self.message.edit.call_args.kwargs["embed"]))
        self.assertIsNone(self.message.edit.call_args.kwargs["view"])
        self.assertIsNone(self.message.edit.call_args.kwargs["content"])
        self.channel.send.assert_not_awaited()

    async def test_capability_and_missing_callbacks_leave_original_confirmation_compatible(self):
        self.assertIsNone(status_module.prepare(self.rt, self.message, "session", {"capabilities": {"create": True}}))
        self.assertIsNone(status_module.prepare(NS(), self.message, "session", record()))
        card = self.monitor(bound=False)
        self.assertFalse(card.start({"goal_id": ""}))
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_exact_sequence_and_normal_trigger_isolation_without_trigger_mutation(self):
        card = self.monitor()
        normal = trigger(20, background=False)
        normal.platform_data.update(status_msg=object(), cancel_view=object())
        before = dict(normal.platform_data)
        card.observe(normal, "ordinary request must not appear", False)
        card.observe(trigger(30, ck="discord:other"), "other room", False)
        card.observe(trigger(40, task_id="other-goal"), "other goal", False)
        card.observe(trigger(), "owned execution", False)
        text = display(card.embed())
        self.assertIn("owned execution", text)
        self.assertNotIn("ordinary request", text)
        self.assertNotIn("other room", text)
        self.assertNotIn("other goal", text)
        self.assertEqual(normal.platform_data, before)
        self.assertNotIn(20, card.progress)
        for seq in range(100, 140):
            card.observe(trigger(seq), str(seq), False)
        self.assertLessEqual(len(card.progress), 32)

    async def test_round_terminal_is_not_goal_completion_and_late_sweep_cannot_restart_timer(self):
        card = self.monitor()
        card.data["status"] = "waiting"
        card.observe(trigger(), "tool finished ✓", True)
        before = card.embed().to_dict()
        card.observe(trigger(), "late live sweep", False)
        with patch.object(status_module.time, "time", return_value=9000):
            after = card.embed().to_dict()
        self.assertEqual(before, after)
        self.assertEqual(fields(card.embed())["目标状态"], "等待结果")
        self.assertIn("本轮已结束", fields(card.embed()))
        self.assertNotIn("本轮用时", fields(card.embed()))
        self.assertNotIn("late live", display(card.embed()))

    async def test_two_rounds_select_early_next_round_cache_and_reset_tool_and_timer(self):
        card = self.monitor()
        card.observe(trigger(), "old round tool", True)
        self.current = record(revision=4, status="reviewing")
        await card.refresh()
        frozen = card.embed().to_dict()
        card.observe(trigger(11, task_id="next-root"), "new round tool", False)
        self.assertEqual(card.embed().to_dict(), frozen)
        self.current = record(revision=5, rounds=1, execution={"task_id": "next-root", "source_inbound_seq": 11, "started_at": 2000})
        await card.refresh()
        with patch.object(status_module.time, "time", return_value=2012):
            next_fields = fields(card.embed())
        self.assertIn("new round tool", display(card.embed()))
        self.assertNotIn("old round tool", display(card.embed()))
        self.assertEqual(next_fields["本轮用时"], "⏱ 12s")
        self.assertEqual(next_fields["自动续轮"], "1/8")
        card.observe(trigger(), "old late tool", False)
        self.assertNotIn("old late tool", display(card.embed()))

    async def test_pause_block_review_freeze_display_but_allow_later_round_events(self):
        card = self.monitor()
        for index, status in enumerate(("paused", "blocked", "reviewing"), 4):
            self.current = record(revision=index, status=status, pause_reason="需要核对")
            card.observe(trigger(), f"snapshot {status}", False)
            await card.refresh()
            before = card.embed().to_dict()
            card.observe(trigger(), "late progress must not move frozen card", False)
            with patch.object(status_module.time, "time", return_value=9999):
                self.assertEqual(card.embed().to_dict(), before)
            self.assertNotIn("本轮用时", fields(card.embed()))
            self.assertEqual(fields(card.embed())["原因"], "需要核对")
        self.current = record(revision=9, status="running")
        await card.refresh()
        self.assertIn("late progress", display(card.embed()))

    async def test_embed_fields_stay_within_discord_limits(self):
        card = self.monitor()
        card.data.update(goal="*" * 16000, pause_reason="x" * 1500)
        card.observe(trigger(), "p" * 1800, False)
        embed = card.embed()
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(embed), 6000)
        self.assertEqual(len(fields(embed)["当前轮进度"]) + len(fields(embed)["进度（续）"]), 1800)

    async def test_late_revision_and_replacement_do_not_adopt_another_goal(self):
        card = self.monitor()
        self.current = record(revision=2, goal="stale", status="completed")
        self.assertFalse(await card.refresh())
        self.assertEqual(card.data["revision"], 3)
        self.assertEqual(card.data["goal"], "Run the checker")
        for current in (record(revision=4, goal_id="goal-two", goal="other goal"), record(revision=4, status="empty")):
            self.current = current
            self.assertTrue(await card.refresh())
            self.assertEqual(card.data["goal_id"], "goal-one")
            self.assertIn("停止更新", fields(card.embed())["目标状态"])
            self.assertNotIn("other goal", display(card.embed()))

    async def test_completed_run_updates_same_card_and_releases_subscription(self):
        card = self.monitor()
        self.current = record(revision=8, status="completed")
        await card.run()
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)
        self.assertEqual(fields(self.message.edit.call_args.kwargs["embed"])["目标状态"], "已完成")
        self.message.delete.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_plugin_absent_or_disabled_stops_without_recreating_message(self):
        for code in (404, 503):
            card = self.monitor()
            self.http_error = code
            await card.run()
            self.assertTrue(card.closed)
            self.assertIn("不可用", fields(self.message.edit.call_args.kwargs["embed"])["目标状态"])
        self.channel.send.assert_not_awaited()
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_three_network_failures_stop_and_only_read_requests_are_used(self):
        card = self.monitor()
        self.http_error = "timeout"
        with patch.object(status_module.asyncio, "sleep", AsyncMock()) as sleep:
            await card.run()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3, 3])
        self.assertIn("停止更新", fields(self.message.edit.call_args.kwargs["embed"])["目标状态"])
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_rate_limited_final_state_is_eventually_rendered_then_detached(self):
        card = self.monitor()
        self.current = record(revision=4, status="completed")
        with patch.object(self.rt.callbacks, "_should_edit", return_value=False), \
             patch.object(status_module.asyncio, "sleep", AsyncMock()) as sleep:
            await card.run()
        sleep.assert_awaited_once_with(5)
        self.message.edit.assert_awaited_once()
        self.assertEqual(fields(self.message.edit.call_args.kwargs["embed"])["目标状态"], "已完成")
        self.assertTrue(card.closed)

    async def test_deleted_card_and_cancelled_task_release_subscription(self):
        card = self.monitor()
        self.message.edit.side_effect = discord.NotFound(NS(status=404, reason="Not Found"), "deleted")
        await card.run()
        self.assertTrue(card.closed)
        self.channel.send.assert_not_awaited()
        self.message.edit.side_effect = None
        card = self.monitor()
        entered = asyncio.Event()
        async def wait_read():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(card, "refresh", side_effect=wait_read):
            task = asyncio.create_task(card.run())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_exact_task_fallback_does_not_borrow_other_task_in_same_session(self):
        card = self.monitor()
        own = self.state.get_or_create_child_state('root', prefix='custom-node')[0]
        other = self.state.get_or_create_child_state('other-root', prefix='custom-node')[0]
        own.lines.append('selected exact task')
        other.lines.append('unrelated same-session task')
        await card.refresh()
        self.assertIn('selected exact task', display(card.embed()))
        self.assertNotIn('unrelated same-session task', display(card.embed()))

    async def test_temporary_discord_edit_failure_recovers_without_losing_completed_state(self):
        card = self.monitor()
        self.current = record(revision=4, status="completed")
        self.message.edit.side_effect = [discord.HTTPException(NS(status=500, reason="Server Error"), "temporary"), None]
        with patch.object(status_module.asyncio, "sleep", AsyncMock()):
            await card.run()
        self.assertEqual(self.message.edit.await_count, 2)
        self.assertEqual(fields(self.message.edit.call_args.kwargs["embed"])["目标状态"], "已完成")
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_repeated_discord_edit_failure_is_bounded(self):
        card = self.monitor()
        self.message.edit.side_effect = discord.HTTPException(NS(status=500, reason="Server Error"), "temporary")
        with patch.object(status_module.asyncio, "sleep", AsyncMock()):
            await card.run()
        self.assertEqual(self.message.edit.await_count, 3)
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)
        self.channel.send.assert_not_awaited()

    async def test_unchanged_paused_deleted_card_is_detected_by_presence_check(self):
        card = self.monitor()
        self.current = record(revision=4, status="paused")
        self.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(NS(status=404, reason="Not Found"), "deleted"))
        async def after_pause(delay):
            self.assertEqual(delay, 10)
            card.last_presence_check -= 61
        with patch.object(status_module.asyncio, "sleep", side_effect=after_pause):
            await card.run()
        self.message.edit.assert_awaited_once()
        self.channel.fetch_message.assert_awaited_once_with(self.message.id)
        self.assertTrue(card.closed)
        self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_switched_root_end_is_not_output_chain_or_round_completion(self):
        card = self.monitor()
        self.rt.callbacks.get_task_progress = Mock(return_value=("custom tool done", True))
        await card.refresh()
        self.rt.callbacks.get_task_progress.assert_called_once_with("root")
        self.assertIn("根任务已结束", card.progress[10][0])
        self.assertFalse(card.progress[10][1])
        self.assertIn("本轮用时", fields(card.embed()))
        self.assertNotIn("本轮已结束", fields(card.embed()))
        self.assertEqual(fields(card.embed())["目标状态"], "执行中")

    async def test_pause_poll_interval_and_active_interval_are_separate(self):
        card = self.monitor()
        self.current = record(revision=4, status="paused")
        async def next_state(delay):
            self.assertEqual(delay, 10)
            self.current = record(revision=5, status="completed")
        with patch.object(status_module.asyncio, "sleep", side_effect=next_state):
            await card.run()
        self.assertTrue(card.closed)

    async def test_confirmation_buffers_progress_before_post_reply_and_failure_unsubscribes(self):
        origin = NS(id=100, author=NS(id=1), channel=self.channel)
        pre = record(status="empty", revision=2, execution=None)
        view = ide_goal.GoalConfirmView(self.rt, origin, "Run the checker", pre, "discord_guild",
                                       new_session=False, node={"node_id": "main"})
        view.card = self.message
        interaction = NS(user=NS(id=1), response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                         edit_original_response=AsyncMock())
        built = []
        real_prepare = status_module.prepare
        def prepare(*args):
            card = real_prepare(*args)
            built.append(card)
            self.monitors.append(card)
            card.run = AsyncMock()
            return card
        async def request(rt, method, path, **kwargs):
            if method == "POST":
                self.assertEqual(len(self.rt.callbacks._progress_listeners), 1)
                built[0].observe(trigger(), "tool completed before HTTP response", True)
                return record()
            return pre if path == ide_goal.GOAL_PATH else {"node_id": "main"}
        with patch.object(ide_goal, "_available", AsyncMock()), \
             patch.object(ide_goal, "_session", AsyncMock(return_value="session")), \
             patch.object(ide_goal, "_request", side_effect=request), \
             patch.object(status_module, "prepare", side_effect=prepare):
            await view.confirm.callback(interaction)
        await built[0].task
        interaction.edit_original_response.assert_not_awaited()
        await built[0].render()
        self.assertIsNone(self.message.edit.call_args.kwargs["view"])
        self.assertIn("before HTTP", display(built[0].embed()))
        built[0].close()
        for failure in (httpx.ReadTimeout("lost"), asyncio.CancelledError()):
            view = ide_goal.GoalConfirmView(self.rt, origin, "Run the checker", pre, "discord_guild",
                                           new_session=False, node={"node_id": "main"})
            view.card = self.message
            async def failed_request(rt, method, path, **kwargs):
                if method == "POST":
                    raise failure
                return pre if path == ide_goal.GOAL_PATH else {"node_id": "main"}
            with patch.object(ide_goal, "_available", AsyncMock()), \
                 patch.object(ide_goal, "_session", AsyncMock(return_value="session")), \
                 patch.object(ide_goal, "_request", side_effect=failed_request):
                if isinstance(failure, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await view.confirm.callback(interaction)
                else:
                    await view.confirm.callback(interaction)
            self.assertFalse(self.rt.callbacks._progress_listeners)

    async def test_same_channel_real_sdk_keeps_normal_reply_timer_tools_and_cancel_view(self):
        card = self.monitor()
        normal_status = NS(id=700, channel=self.channel, edit=AsyncMock(), delete=AsyncMock())
        origin = NS(id=701, channel=self.channel, reply=AsyncMock(return_value=NS(id=702)), add_reaction=AsyncMock())
        cancel = CancelView(self.rt, "session")
        self.addCleanup(cancel.stop)
        normal = trigger(20, background=False, task_id=None)
        normal.platform_data.update(message=origin, channel_id=42, status_msg=normal_status,
                                    cancel_view=cancel, context_watermark=5)
        self.state.register_trigger(normal)
        router = EventRouter(self.client, self.state, self.rt.callbacks,
                             BotConfig(base_url=self.client.base_url, entry_node_id="main", conversation_key_prefix="discord"))
        router.set_raw_event_hook(self.rt.callbacks.raw_event_hook)
        async def event(kind, seq, task_id, **extra):
            await router._dispatch(Event.from_dict({"seq": 1, "type": kind, "session_id": "session",
                "payload": {"node_id": "main", "source_inbound_seq": seq, "task_id": task_id, **extra}}))
        await event("task_created", 20, "normal-root")
        self.assertEqual(cancel.task_id, "normal-root")
        await event("node_started", 20, "normal-root", node_name="Main")
        self.assertIn("⏱", normal_status.edit.call_args.kwargs["content"])
        await event("node_started", 10, "root", node_name="Main")
        await event("stream_delta", 20, "normal-root", content="normal prose", type="text", llm_request_id="n1")
        await event("assistant_text", 20, "normal-root", text="ordinary explanation")
        await event("tool_call_start", 20, "normal-root", tool_name="read_file", arguments={"path": "ordinary.txt"}, tool_call_id="n-tool")
        await event("tool_call_end", 20, "normal-root", tool_call_id="n-tool", status="success")
        await event("tool_call_start", 10, "root", tool_name="execute_command", arguments={"command": "goal-check"}, tool_call_id="g-tool")
        await router._refresh_active_states()
        self.assertIn("ordinary.txt", normal_status.edit.call_args.kwargs["content"])
        self.assertIn("ordinary explanation", normal_status.edit.call_args.kwargs["content"])
        self.assertNotIn("view", normal_status.edit.call_args.kwargs, "ordinary progress must preserve its cancel view")
        self.assertIs(normal.platform_data["cancel_view"], cancel)
        self.assertNotIn(20, card.progress)
        self.assertIn("goal-check", display(card.embed()))
        self.assertNotIn("ordinary.txt", display(card.embed()))
        with patch.object(self.client, "cancel_task", AsyncMock(return_value=True)) as cancel_request:
            interaction = NS(response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))
            await cancel.cancel_task.callback(interaction)
            cancel_request.assert_awaited_once_with("normal-root")
        await event("outbound_message", 20, "normal-root", text="ordinary answer")
        origin.reply.assert_awaited_once()
        self.assertEqual(origin.reply.call_args.kwargs["content"], "ordinary answer")
        normal_status.delete.assert_awaited_once()
        self.message.delete.assert_not_awaited()
        await event("outbound_message", 10, "root", text="goal round answer")
        self.assertEqual(self.channel.send.call_args.kwargs["content"], "goal round answer")
        self.assertEqual(fields(card.embed())["目标状态"], "执行中")
        self.assertTrue(card.progress[10][1])
        self.assertIsNotNone(self.state.get_session_id("discord:42"))


from test_goal_integration import case, claim, control, finish


def test_real_asgi_goal_metadata_binds_card_and_normal_input_stops_goal_timer(case):
    from fastapi import APIRouter
    from plugins.ide.goal import _SERVICES, add_routes
    from supervisor import admin_api
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore
    c = case
    async def run():
        with patch.object(admin_api, "_admin_token", "goal-status-test"), \
             patch.object(admin_api, "_web_auth", None), \
             patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}):
            app = create_app(state=c.state, process_manager=None, config_store=ConfigStore(path=c.root / "data/config.yaml"))
            routes = APIRouter()
            add_routes(routes)
            app.include_router(routes, prefix="/v1/plugins/ide")
            client = ClonothClient("http://asgi.test", admin_token="goal-status-test")
            client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer goal-status-test"})
            rt = NS(clonoth_client=client, session_state=SessionState())
            rt.callbacks = DiscordCallbacks(rt)
            msg = NS(id=7, channel=NS(id=42), edit=AsyncMock())
            monitor = status_module.prepare(rt, msg, c.sid, c.service.read(c.sid))
            try:
                created = control(c, "create", goal="Check source files")
                monitor.goal_id = created["goal_id"]
                monitor.data = created
                c.service.tick()
                current = c.service.read(c.sid)
                root = current["execution"]["task_id"]
                seq = c.state.tasks[root].source_inbound_seq
                monitor.observe(trigger(seq, task_id=root), "real root tool", False)
                await monitor.refresh()
                assert monitor.data["execution"]["task_id"] == root
                assert monitor.data["execution"]["source_inbound_seq"] == seq
                assert "real root tool" in display(monitor.embed())
                claim(c, root)
                finish(c, root, "round result")
                monitor.observe(trigger(seq, task_id=root), "round result sent", True)
                await monitor.refresh()
                assert monitor.data["status"] != "completed"
                assert "本轮用时" not in fields(monitor.embed())
                c.service.on_event({"kind": "input", "session_id": c.sid})
                await monitor.refresh()
                assert monitor.data["status"] == "paused"
                assert monitor.data["execution"] is None
                assert "本轮用时" not in fields(monitor.embed())
            finally:
                monitor.close()
                await client.close()
    asyncio.run(run())
