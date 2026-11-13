"""Discord optional !goal extension. Offline HTTP/Discord, no gateway or disk writes.

Run: python3.11 -B -m pytest tests/test_discord_goal.py -q
"""
from __future__ import annotations

import asyncio
from collections import deque
import datetime
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from adapters.discord import app, context
from adapters.discord import extensions
from adapters.discord.extensions import ide_goal
from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import Event


class DM:
    id = 11
    guild = None


class DiscordGoalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.posts = []
        self.plugins = [{"name": "ide"}]
        self.sid = "s1"
        self.current_sid = self.sid
        self.goal_status = "empty"
        self.goal_revision = 2
        self.capabilities = {"create": True}
        self.create_status = "running"
        self.node = "main"
        self.override = False
        self.events = [{"type": "session_created"}, {"type": "inbound_message"}]
        self.error_path = None
        self.error_status = 503
        self.transport_error = False
        self.created_response = None
        self.views = []
        self.client = ClonothClient("https://offline.invalid", admin_token="offline-test-token")
        self.client._last_token = "offline-test-token"
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.http),
                                               headers={"Authorization": "Bearer offline-test-token"})
        bot = NS(id=99, mentioned_in=MagicMock(return_value=False))
        dc = NS(user=bot)
        dc.event = lambda fn: setattr(dc, fn.__name__, fn) or fn
        self.rt = NS(clonoth_client=self.client, session_state=SessionState(), entry_node_id="main",
                     channel_history={}, transient_msg_ids=deque(maxlen=1000), key_roles=set(),
                     history_max_len=15, history_seq_counter=0, superusers=set(), dc_client=dc)
        app.register_events(self.rt)
        self.msg = self.message()

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        await self.client.close()

    def message(self, text="!goal Make checker pass", mid=30, channel=None):
        channel = channel or NS(id=10, guild=NS())
        card = NS(id=mid + 100, edit=AsyncMock())
        msg = NS(id=mid, channel=channel, guild=channel.guild, author=NS(id=1, bot=False,
                 name="alice", display_name="Alice", roles=[]), content=text, attachments=[],
                 reference=None, message_snapshots=[], mention_everyone=False,
                 created_at=datetime.datetime.now(datetime.timezone.utc), stickers=[], embeds=[])
        msg.reply = AsyncMock(return_value=card)
        return msg

    async def http(self, request):
        self.calls.append((request.method, request.url.path, dict(request.url.params),
                           json.loads(request.content) if request.content else None))
        assert request.headers.get("authorization") == "Bearer offline-test-token"
        path, method = request.url.path, request.method
        if self.error_path == path:
            return httpx.Response(self.error_status, json={"detail": "private backend failure"})
        if path == "/v1/plugins":
            data = {"plugins": self.plugins}
        elif path == "/v1/sessions/get_or_create":
            data = {"session_id": self.current_sid, "conversation_key": json.loads(request.content)["conversation_key"]}
        elif path.endswith("/active_node"):
            data = {"session_id": self.sid, "node_id": self.node, "default_node_id": "main", "is_override": self.override}
        elif path.endswith("/events"):
            data = self.events
        elif path.endswith("/switch_node"):
            self.node = json.loads(request.content)["target_node_id"]
            data = {"ok": True, "node_id": self.node, "session_id": self.sid}
        elif path == ide_goal.GOAL_PATH and method == "GET":
            data = {"session_id": self.sid, "revision": self.goal_revision, "status": self.goal_status,
                    "is_child": False, "capabilities": self.capabilities}
        elif path == ide_goal.GOAL_PATH and method == "POST":
            self.posts.append(json.loads(request.content))
            if self.transport_error:
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            data = self.created_response or {"session_id": self.sid, "revision": 3, "status": self.create_status}
        else:
            raise AssertionError(f"unexpected API: {method} {path}")
        return httpx.Response(200, json=data)

    async def command(self, msg=None):
        msg = msg or self.msg
        with patch.object(app, "handle_agent", AsyncMock()) as agent, \
             patch.object(app, "handle_model_command", AsyncMock(return_value=False)) as model:
            await self.rt.dc_client.on_message(msg)
            agent.assert_not_awaited()
            model.assert_not_awaited()
        kwargs = msg.reply.call_args.kwargs if msg.reply.call_args else {}
        view = kwargs.get("view")
        if view:
            self.views.append(view)
        return view

    def interaction(self, uid=1):
        return NS(user=NS(id=uid), response=NS(defer=AsyncMock(), send_message=AsyncMock(),
                   edit_message=AsyncMock()), edit_original_response=AsyncMock())

    async def click(self, view, *, uid=1, cancel=False):
        interaction = self.interaction(uid)
        button = view.cancel if cancel else view.confirm
        await button.callback(interaction)
        return interaction

    def result_text(self, interaction):
        return interaction.edit_original_response.call_args.kwargs["content"]

    async def test_real_app_entry_consumes_before_history_and_creates_only_after_confirmation(self):
        view = await self.command()
        self.assertIsInstance(view, ide_goal.GoalConfirmView)
        self.assertFalse(self.rt.channel_history)
        self.assertIn(self.msg.id, self.rt.transient_msg_ids)
        self.assertIn(view.card.id, self.rt.transient_msg_ids)
        self.assertFalse(self.posts)
        self.assertEqual([p for m, p, *_ in self.calls if m != "GET"], ["/v1/sessions/get_or_create"])
        embed = self.msg.reply.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "IDE 扩展 · 创建 Goal")
        self.assertIn("当前频道", embed.fields[0].value)
        self.assertIn("120 秒", embed.footer.text)
        i = await self.click(view)
        self.assertIn("已开始推进", self.result_text(i))
        self.assertEqual(self.posts, [{"action": "create", "goal": "Make checker pass", "expected_session_id": "s1",
                                      "expected_revision": 2, "request_id": "discord-goal:10:30"}])
        self.assertTrue(all(b.disabled for b in view.children))
        self.assertEqual(self.rt.session_state.get_session_id("discord:10"), "s1")

    async def test_only_creator_can_confirm_or_cancel_nonowner_does_not_extend_deadline(self):
        view = await self.command()
        deadline, expiry = view.deadline, view.expiry.when()
        for cancel in (False, True):
            i = await self.click(view, uid=2, cancel=cancel)
            i.response.send_message.assert_awaited_once()
            self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])
            self.assertFalse(view.decided)
        self.assertEqual((view.deadline, view.expiry.when()), (deadline, expiry))
        self.assertFalse(self.posts)
        i = await self.click(view)
        self.assertIn("已开始推进", self.result_text(i))

    async def test_cancel_and_timeout_do_not_write_goal(self):
        view = await self.command()
        i = await self.click(view, cancel=True)
        self.assertIn("已取消", i.response.edit_message.call_args.kwargs["content"])
        await view.on_timeout()
        self.assertFalse(self.posts)
        view = await self.command()
        await view.on_timeout()
        self.assertIn("超时", view.card.edit.call_args.kwargs["content"])
        self.assertTrue(view.expiry.cancelled())
        self.assertFalse(self.posts)
        late = await self.click(view)
        late.response.send_message.assert_awaited_once()

    async def test_fixed_monotonic_deadline_rejects_late_click_even_before_timer_runs(self):
        view = await self.command()
        with patch.object(ide_goal, "time", NS(monotonic=lambda: view.deadline + 1)):
            await self.click(view)
        self.assertTrue(view.decided)
        self.assertFalse(self.posts)

    async def test_double_click_and_original_message_edit_do_not_change_frozen_body(self):
        view = await self.command()
        self.msg.content = "!goal changed later"
        await asyncio.gather(self.click(view), self.click(view))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["goal"], "Make checker pass")

    async def test_parser_exact_command_empty_body_and_attachment_rejection(self):
        self.assertFalse(await extensions.dispatch_command(self.rt, self.msg, "!goalkeeper abc"))
        self.assertFalse(await extensions.dispatch_command(self.rt, self.msg, "hello !goal abc"))
        self.msg.content = "!goal"
        await self.command()
        self.assertIn("用法", self.msg.reply.call_args.args[0])
        self.msg.content = "!goal body"
        self.msg.attachments = [NS(filename="a.txt")]
        await self.command()
        self.assertIn("不支持附件", self.msg.reply.call_args.args[0])
        self.assertFalse(self.calls)

    async def test_bots_are_ignored_at_app_entry(self):
        self.msg.author.bot = True
        await self.command()
        self.msg.reply.assert_not_awaited()
        self.assertFalse(self.calls)

    async def test_long_goal_has_complete_utf8_attachment_and_mentions_are_suppressed(self):
        goal = "**目标** @everyone\n" * 230
        self.msg.content = "!goal " + goal
        captured = {}
        async def reply(**kwargs):
            captured.update(kwargs)
            captured["raw"] = kwargs["files"][0].fp.read()
            return NS(id=130, edit=AsyncMock())
        self.msg.reply.side_effect = reply
        await self.command()
        self.assertEqual(captured["raw"].decode(), goal.strip())
        self.assertLessEqual(len(captured["embed"].description), 4096)
        self.assertIn("goal.txt", captured["embed"].description)
        self.assertFalse(captured["allowed_mentions"].everyone)
        self.assertFalse(captured["allowed_mentions"].users)
        self.assertFalse(captured["mention_author"])

    async def test_unconfirmed_command_and_card_cannot_be_quoted_as_reply_context(self):
        view = await self.command()
        for mid in (self.msg.id, view.card.id):
            next_message = NS(reference=NS(message_id=mid))
            self.assertEqual(await context._build_reply_context(self.rt, next_message), "")

    async def test_missing_disabled_old_plugin_and_missing_sdk_fail_closed(self):
        self.plugins = []
        await self.command()
        self.assertIn("未安装或未启用", self.msg.reply.call_args.args[0])
        self.assertEqual(len(self.calls), 1)
        self.plugins = [{"name": "ide"}]
        self.capabilities = {}
        await self.command()
        self.assertIn("更新插件", self.msg.reply.call_args.args[0])
        self.rt.clonoth_client = None
        await self.command()
        self.assertIn("SDK", self.msg.reply.call_args.args[0])
        self.assertFalse(self.posts)
        self.assertFalse(self.rt.channel_history)

    async def test_dynamic_import_failure_is_consumed_and_reported(self):
        with patch.object(extensions, "import_module", side_effect=ImportError("optional extension missing")):
            await self.command()
        self.assertIn("命令扩展当前不可用", self.msg.reply.call_args.args[0])
        self.assertFalse(self.calls)
        self.assertFalse(self.rt.channel_history)

    async def test_http_missing_or_not_ready_before_and_after_confirm(self):
        for status in (404, 503):
            self.error_path, self.error_status = ide_goal.GOAL_PATH, status
            await self.command()
            self.assertIn("未安装、未启用", self.msg.reply.call_args.args[0])
            self.error_path = None
            view = await self.command()
            self.error_path = ide_goal.GOAL_PATH
            i = await self.click(view)
            self.assertIn("未安装、未启用", self.result_text(i))
            self.assertFalse(self.posts)
        self.assertFalse(self.rt.channel_history)

    async def test_all_unfinished_statuses_rejected_completed_replacement_is_explicit(self):
        for status in ("running", "waiting", "reviewing", "paused", "blocked", "unknown"):
            self.goal_status = status
            self.assertIsNone(await self.command())
            self.assertIn("未完成", self.msg.reply.call_args.args[0])
        self.goal_status = "completed"
        view = await self.command()
        warning = self.msg.reply.call_args.kwargs["embed"].fields[1].value
        self.assertIn("重置执行预算", warning)
        self.assertIn("聊天历史和 Todo 保留", warning)
        await self.click(view)
        self.assertEqual(self.posts[0]["action"], "create")
        self.assertFalse(any(body and body.get("action") in {"clear", "submit"} for _, _, _, body in self.calls))

    async def test_revision_conflict_new_active_goal_and_reset_never_redirect_confirmation(self):
        view = await self.command()
        self.goal_revision = 3
        i = await self.click(view)
        self.assertIn("目标已变化", self.result_text(i))
        view = await self.command()
        self.goal_status = "running"
        i = await self.click(view)
        self.assertIn("未完成", self.result_text(i))
        self.goal_status = "empty"
        view = await self.command()
        self.current_sid = "reset-session"
        i = await self.click(view)
        self.assertIn("会话已重置或替换", self.result_text(i))
        self.assertFalse(self.posts)

    async def test_post_conflict_and_network_timeout_are_not_retried(self):
        view = await self.command()
        original = self.http
        async def conflict(request):
            if request.url.path == ide_goal.GOAL_PATH and request.method == "POST":
                return httpx.Response(409, json={"detail": "changed"})
            return await original(request)
        self.client._client._transport = httpx.MockTransport(conflict)
        i = await self.click(view)
        self.assertIn("不再有效", self.result_text(i))
        self.client._client._transport = httpx.MockTransport(original)
        view = await self.command()
        self.transport_error = True
        i = await self.click(view)
        self.assertIn("创建结果未确认", self.result_text(i))
        self.assertEqual(len(self.posts), 1)
        await self.click(view)
        self.assertEqual(len(self.posts), 1)

    async def test_result_reports_actual_status_never_claims_blocked_is_started(self):
        for status, expected in (("paused", "已暂停"), ("blocked", "受阻"), ("empty", "未设置"), ("completed", "已完成")):
            self.create_status = status
            view = await self.command()
            i = await self.click(view)
            self.assertIn(expected, self.result_text(i))
            self.assertNotIn("已开始推进", self.result_text(i))
        self.created_response = {"session_id": "wrong", "revision": 3, "status": "running"}
        view = await self.command()
        self.assertIn("创建结果未确认", self.result_text(await self.click(view)))

    async def test_initial_entry_binding_only_after_confirm_existing_binding_untouched(self):
        self.events = [{"type": "session_created"}]
        self.rt.entry_node_id = "other"
        view = await self.command()
        self.assertNotIn("/v1/sessions/s1/switch_node", [p for _, p, *_ in self.calls])
        await self.click(view)
        self.assertEqual(self.node, "other")
        self.assertEqual(len(self.posts), 1)
        self.calls.clear()
        self.node = "user-chosen"
        self.events.append({"type": "node_switch"})
        view = await self.command()
        await self.click(view)
        self.assertEqual(self.node, "user-chosen")
        self.assertFalse(any(p.endswith("/switch_node") for _, p, *_ in self.calls))

    async def test_new_session_concurrent_binding_or_input_is_not_overwritten(self):
        self.events = [{"type": "session_created"}]
        self.rt.entry_node_id = "other"
        view = await self.command()
        self.events.append({"type": "inbound_message"})
        i = await self.click(view)
        self.assertIn("其他操作", self.result_text(i))
        self.assertFalse(self.posts)
        self.assertFalse(any(p.endswith("/switch_node") for _, p, *_ in self.calls))
        self.events = [{"type": "session_created"}]
        view = await self.command()
        self.node = "changed"
        self.assertIn("入口节点已变化", self.result_text(await self.click(view)))
        self.assertFalse(self.posts)

    async def test_existing_explicit_default_binding_is_preserved(self):
        self.events = [{"type": "session_created"}, {"type": "node_switch"}]
        self.rt.entry_node_id = "other"
        view = await self.command()
        self.assertFalse(view.new_session)
        await self.click(view)
        self.assertFalse(any(p.endswith("/switch_node") for _, p, *_ in self.calls))

    async def test_cancelled_unbound_session_still_initializes_custom_entry_next_time(self):
        self.events = [{"type": "session_created"}]
        self.rt.entry_node_id = "custom"
        view = await self.command()
        self.assertTrue(view.new_session)
        await self.click(view, cancel=True)
        self.assertFalse(self.posts)
        self.assertEqual(self.node, "main")
        # get_or_create now returns the same pre-existing, still unbound session.
        self.msg = self.message(mid=31)
        view = await self.command()
        self.assertTrue(view.new_session)
        await self.click(view)
        self.assertEqual(self.node, "custom")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["request_id"], "discord-goal:10:31")
        self.assertFalse(any(p == "/v1/sessions" for _, p, *_ in self.calls))

    async def test_server_error_after_create_may_have_saved_goal(self):
        original = self.http
        for status in (500, 503):
            async def fail_post(request):
                if request.url.path == ide_goal.GOAL_PATH and request.method == "POST":
                    self.posts.append(json.loads(request.content))
                    return httpx.Response(status, json={"detail": "cleanup failed after save"})
                return await original(request)
            self.client._client._transport = httpx.MockTransport(fail_post)
            view = await self.command()
            i = await self.click(view)
            text = self.result_text(i)
            self.assertIn("创建结果未确认", text)
            self.assertIn("目标可能已保存", text)
            self.assertIn("Web", text)
            self.assertIn("不要盲目重发", text)
            self.assertNotIn("未安装", text)
        self.assertEqual(len(self.posts), 2)

    async def test_dm_and_thread_routing_reuses_sdk_background_channel_send(self):
        for channel in (DM(), NS(id=44, guild=NS())):
            self.msg = self.message(channel=channel)
            with patch.object(ide_goal.discord, "DMChannel", DM):
                view = await self.command()
                await self.click(view)
            ck = f"discord:{channel.id}"
            session_call = [c for c in self.calls if c[1] == "/v1/sessions/get_or_create"][-1]
            self.assertEqual(session_call[3]["channel"], "discord_dm" if isinstance(channel, DM) else "discord_guild")
            self.assertEqual(self.rt.session_state.get_session_id(ck), self.sid)
            if isinstance(channel, DM):
                self.assertEqual(self.rt.session_state.get_dm_channel(self.sid), channel.id)
            cb = NS(**{name: AsyncMock() for name in ("update_progress", "send_to_channel", "send_reply", "finalize_main_progress")})
            router = EventRouter(None, self.rt.session_state, cb, BotConfig(
                base_url="https://offline.invalid", entry_node_id="main", conversation_key_prefix="discord"))
            for kind, more in (("node_started", {"node_name": "Main"}), ("outbound_message", {"text": "result"})):
                await router._dispatch(Event.from_dict({"seq": 1, "type": kind, "session_id": self.sid,
                    "payload": {"node_id": "main", "task_id": "root", "source_inbound_seq": channel.id, **more}}))
            cb.send_to_channel.assert_awaited_once_with(ck, "result", [], node_id="main")
            cb.send_reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


# Reuse the real Supervisor/GoalService fixture; no HTTP endpoint stubs below.
from test_goal_integration import case, claim, finish


def test_real_asgi_discord_cancel_then_confirm_custom_entry_and_sdk_output(case):
    from fastapi import APIRouter
    from plugins.ide.goal import _SERVICES, add_routes
    from supervisor import admin_api
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore

    c = case
    node = 'discord_custom'
    (c.root / 'config/nodes/discord_custom.yaml').write_text(
        'id: discord_custom\ntype: ai\noutput_mode: hybrid\ncontrol_tools: all\nswitchable: true\n', encoding='utf-8')
    assert c.state._is_switchable_entry_node(node)
    c.state.hook_registry.register_plugin_meta({'name': 'ide', 'version': 'test'})

    async def run():
        with patch.object(admin_api, '_admin_token', 'dc-asgi-token'), \
             patch.object(admin_api, '_web_auth', None), \
             patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}):
            api = create_app(state=c.state, process_manager=None,
                             config_store=ConfigStore(path=c.root / 'data/config.yaml'))
            routes = APIRouter()
            add_routes(routes)
            api.include_router(routes, prefix='/v1/plugins/ide')
            client = ClonothClient('http://asgi.test', admin_token='dc-asgi-token')
            client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                               headers={'Authorization': 'Bearer dc-asgi-token'})
            dc = NS(user=NS(id=99))
            dc.event = lambda fn: setattr(dc, fn.__name__, fn) or fn
            rt = NS(clonoth_client=client, session_state=SessionState(), entry_node_id=node,
                    transient_msg_ids=deque(maxlen=1000), channel_history={}, dc_client=dc)
            app.register_events(rt)
            def message(mid):
                return NS(id=mid, content='!goal Run the real checker', author=NS(id=1, bot=False),
                          channel=NS(id=701, guild=NS()), attachments=[],
                          reply=AsyncMock(return_value=NS(id=mid + 100, edit=AsyncMock())))
            def interaction():
                return NS(user=NS(id=1), response=NS(defer=AsyncMock(), send_message=AsyncMock(),
                          edit_message=AsyncMock()), edit_original_response=AsyncMock())
            views = []
            try:
                first = message(1)
                with patch.object(app, 'handle_agent', AsyncMock()) as normal_agent:
                    await dc.on_message(first)
                    normal_agent.assert_not_awaited()
                view = first.reply.call_args.kwargs['view']
                views.append(view)
                sid = view.sid
                assert view.new_session
                assert c.state.sessions[sid].channel == 'discord_guild'
                assert c.state.sessions[sid].entry_node_id == ''
                events_before = list(c.state.eventlog.events)
                active = await ide_goal._request(rt, 'GET', f'/v1/sessions/{sid}/active_node')
                assert active['node_id'] == 'main' and active['default_node_id'] == 'main'
                assert active['is_override'] is False
                assert await ide_goal._pristine(rt, sid, active)
                assert list(c.state.eventlog.events) == events_before  # GETs do not bind a node or write.
                await view.cancel.callback(interaction())
                assert not c.state.tasks and c.service.read(sid)['status'] == 'empty'
                assert c.state.sessions[sid].entry_node_id == ''

                second = message(2)
                await dc.on_message(second)
                view = second.reply.call_args.kwargs['view']
                views.append(view)
                assert view.sid == sid and view.new_session
                assert not rt.channel_history
                assert {first.id, second.id, view.card.id}.issubset(rt.transient_msg_ids)
                accepted = interaction()
                await view.confirm.callback(accepted)
                assert '已开始推进' in accepted.edit_original_response.call_args.kwargs['content']
                assert c.state.sessions[sid].entry_node_id == node
                assert c.service.read(sid)['status'] == 'running'
                assert not c.state.tasks
                active = await ide_goal._request(rt, 'GET', f'/v1/sessions/{sid}/active_node')
                assert active['node_id'] == node and active['is_override']
                assert not await ide_goal._pristine(rt, sid, active)
                c.service.tick()
                root = c.service.documents[sid]['_roots'][-1]
                assert c.state.tasks[root].node_id == node
                assert 'Run the real checker' in c.state.tasks[root].input['instruction']
                claim(c, root)
                finish(c, root, 'Checked through real supervisor')
                # The real completion path produced the outbound. Feed actual /v1/events to SDK.
                cb = NS(**{name: AsyncMock() for name in (
                    'update_progress', 'send_to_channel', 'send_reply', 'finalize_main_progress',
                    'create_child_progress', 'update_child_progress', 'finalize_child_progress')})
                router = EventRouter(client, rt.session_state, cb, BotConfig(
                    base_url=client.base_url, entry_node_id=node, conversation_key_prefix='discord'))
                events = await client.poll_events(after_seq=0)
                outbound = [event for event in events if event.type == 'outbound_message'
                            and event.session_id == sid]
                assert outbound, [(event.type, event.session_id) for event in events]
                for event in events:
                    await router._dispatch(event)
                cb.send_to_channel.assert_awaited_once_with(
                    'discord:701', 'Checked through real supervisor', [], node_id=node)
                cb.send_reply.assert_not_awaited()
                assert not rt.channel_history
            finally:
                for view in views:
                    view.stop()
                await client.close()
    asyncio.run(run())
