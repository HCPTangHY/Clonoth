"""Offline regression checks for forward-migrated Discord behavior."""
from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from adapters.discord import agent, context, messaging
from adapters.discord.callbacks import DiscordCallbacks
from clonoth_sdk.state import SessionState, TriggerInfo


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Channel:
    def __init__(self, cid=10, members=()):
        self.id = cid
        self.guild = NS(members=members)
        self.send = AsyncMock(return_value=NS(id=901))
        self.fetch_message = AsyncMock()

    def typing(self):
        return Typing()


class DM(Channel):
    pass


def member(uid=1, name="alice", display="Alice"):
    return NS(id=uid, name=name, display_name=display, roles=[])


def message(mid=21, channel=None, author=None):
    channel = channel or Channel()
    return NS(id=mid, channel=channel, guild=channel.guild, author=author or member(),
              reference=None, reply=AsyncMock(return_value=NS(id=mid + 100, delete=AsyncMock())),
              add_reaction=AsyncMock(), attachments=[])


def runtime():
    state = SessionState()
    return NS(session_state=state, channel_history={}, history_seq_counter=0,
              history_max_len=30, key_roles=set(), superusers=set(),
              transient_msg_ids=deque(maxlen=1000), split_signal="[SPLIT]",
              react_pattern=re.compile(r"\[REACT:(.*?)\]"), restart_signal="[BOT_RESTART]",
              entry_node_id="main", child_node_display_names={}, agent_log_channel_id=0,
              bot_restart_view_factory=None, approval_view_factory=None,
              dc_client=NS(user=None), workspace=Path.cwd(),
              clonoth_client=NS(get_running_tasks=AsyncMock(), preempt_task=AsyncMock(return_value=True),
                                submit_inbound=AsyncMock(return_value=NS(session_id="s", inbound_seq=88))))


def trigger(rt, msg, seq=7, watermark=0):
    t = TriggerInfo(inbound_seq=seq, conversation_key=f"discord:{msg.channel.id}",
                    session_id="s", is_dm=isinstance(msg.channel, DM),
                    platform_data={"message": msg, "channel_id": msg.channel.id,
                                   "context_watermark": watermark, "status_msg": None})
    rt.session_state.register_session(t.conversation_key, "s")
    rt.session_state.register_trigger(t)
    return t


class DiscordForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_branch_watermark_overrides_shared_even_at_zero(self):
        rt = runtime()
        rt.channel_history[10] = [{"seq": n, "text": f"entry-{n}"} for n in range(4)]
        rt.session_state.last_ctx_seq[10] = 99
        text = context._build_context_text(rt, 10, member(), "now", exclude_last=True, min_seq_override=0)
        self.assertNotIn("entry-0", text)
        self.assertIn("entry-1", text)
        self.assertIn("entry-2", text)
        self.assertNotIn("entry-3", text)
        self.assertEqual(rt.session_state.last_ctx_seq[10], 99)

    async def test_dm_preempt_uses_branch_watermark_and_task_bound_cancel(self):
        rt = runtime()
        ch = DM()
        first, current = message(channel=ch), message(mid=22, channel=ch)
        t = trigger(rt, first)
        rt.channel_history[10] = [{"seq": n, "text": f"entry-{n}"} for n in range(3)]
        rt.session_state.last_ctx_seq[10] = 99
        rt.clonoth_client.get_running_tasks.return_value = [
            NS(task_id="task-one", is_user_entry=True, source_inbound_seq=7),
            NS(task_id="task-two", is_user_entry=True, source_inbound_seq=7),
        ]
        with patch.object(agent.discord, "DMChannel", DM), \
             patch.object(agent, "_collect_attachments", AsyncMock(return_value=[])):
            await agent._handle_agent_inner(rt, current, "now", 10, "discord:10")
        self.assertEqual(rt.clonoth_client.preempt_task.await_count, 1)
        self.assertIn("entry-1", rt.clonoth_client.preempt_task.call_args.kwargs["message"])
        self.assertNotIn("entry-0", rt.clonoth_client.preempt_task.call_args.kwargs["message"])
        self.assertEqual(t.platform_data["cancel_view"].task_id, "task-one")
        self.assertIs(t.platform_data["origin_message"], first)
        self.assertIs(t.platform_data["message"], current)
        self.assertEqual(t.platform_data["context_watermark"], 2)
        self.assertIn(122, rt.transient_msg_ids)
        rt.clonoth_client.submit_inbound.assert_not_awaited()

    async def test_guild_checks_original_author_and_skips_other_branch(self):
        rt = runtime()
        first = message(author=member(1))
        later = message(author=member(2))
        t = trigger(rt, later)
        t.platform_data["origin_message"] = first
        rt.clonoth_client.get_running_tasks.return_value = [NS(task_id="t", is_user_entry=True, source_inbound_seq=7)]
        current = message(author=member(2))
        with patch.object(agent, "_collect_attachments", AsyncMock(return_value=[])), \
             patch.object(agent, "_derive_initial_workspace", return_value=None):
            await agent._handle_agent_inner(rt, current, "now", 10, "discord:10")
        rt.clonoth_client.preempt_task.assert_not_awaited()
        rt.clonoth_client.submit_inbound.assert_awaited_once()

    async def test_consumed_trigger_during_status_send_does_not_leave_orphan(self):
        rt = runtime()
        current = message()
        t = trigger(rt, current)
        rt.clonoth_client.get_running_tasks.return_value = [NS(task_id="t", is_user_entry=True, source_inbound_seq=7)]
        orphan = NS(id=300, delete=AsyncMock())

        async def reply(*args, **kwargs):
            rt.session_state.consume_trigger(7)
            return orphan

        current.reply.side_effect = reply
        with patch.object(agent, "_collect_attachments", AsyncMock(return_value=[])), \
             patch.object(agent, "_derive_initial_workspace", return_value=None):
            await agent._handle_agent_inner(rt, current, "now", 10, "discord:10")
        orphan.delete.assert_awaited_once()
        rt.clonoth_client.submit_inbound.assert_awaited_once()
        self.assertIsNone(rt.session_state.get_trigger(t.inbound_seq))

    async def test_precise_transient_id_filter_and_promotion(self):
        rt = runtime()
        msg = message()
        msg.reference = NS(message_id=501, resolved=None)
        context.register_transient_msg(rt, NS(id=501))
        self.assertEqual(await context._build_reply_context(rt, msg), "")
        msg.channel.fetch_message.assert_not_awaited()
        context.unregister_transient_msg(rt, NS(id=501))
        msg.channel.fetch_message.return_value = NS(id=501, author=member(), created_at=None,
                                                  content="⏳ actual answer", embeds=[], stickers=[], attachments=[])
        self.assertIn("actual answer", await context._build_reply_context(rt, msg))

    async def test_partial_reply_only_promoted_after_successful_edit(self):
        rt = runtime()
        msg = message()
        t = trigger(rt, msg)
        status = NS(id=99, edit=AsyncMock(side_effect=RuntimeError("network unavailable")))
        t.platform_data["status_msg"] = status
        context.register_transient_msg(rt, status)
        cb = DiscordCallbacks(rt)
        cb._dot_states[7] = {"text_preview": "partial answer"}
        await cb.delete_status_message(t)
        self.assertIn(99, rt.transient_msg_ids)
        status.edit.side_effect = None
        cb._dot_states[7] = {"text_preview": "partial answer"}
        await cb.delete_status_message(t)
        self.assertNotIn(99, rt.transient_msg_ids)

    async def test_reaction_success_commits_without_synthetic_history(self):
        for method in ("send_reply", "send_intermediate_reply"):
            rt = runtime()
            msg = message()
            t = trigger(rt, msg, watermark=5)
            await getattr(DiscordCallbacks(rt), method)(t, "[REACT:✅]", [])
            self.assertEqual(rt.session_state.last_ctx_seq[10], 5)
            self.assertEqual(rt.channel_history, {})
            msg.reply.assert_not_awaited()

    async def test_failed_reaction_and_failed_body_do_not_commit(self):
        for method in ("send_reply", "send_intermediate_reply"):
            rt = runtime()
            msg = message()
            t = trigger(rt, msg, watermark=5)
            msg.add_reaction.side_effect = RuntimeError("no reaction")
            await getattr(DiscordCallbacks(rt), method)(t, "[REACT:✅]", [])
            self.assertNotIn(10, rt.session_state.last_ctx_seq)
            msg.add_reaction.side_effect = None
            msg.reply.side_effect = RuntimeError("no send")
            await getattr(DiscordCallbacks(rt), method)(t, "answer [REACT:✅]", [])
            self.assertNotIn(10, rt.session_state.last_ctx_seq)

    async def test_older_reaction_does_not_regress_watermark(self):
        rt = runtime()
        t = trigger(rt, message(), watermark=5)
        rt.session_state.last_ctx_seq[10] = 10
        await DiscordCallbacks(rt).send_reply(t, "[REACT:✅]", [])
        self.assertEqual(rt.session_state.last_ctx_seq[10], 10)

    async def test_intermediate_local_limit_and_full_reply_limit(self):
        rt = runtime()
        msg = message()
        t = trigger(rt, msg)
        cb = DiscordCallbacks(rt)
        await cb.send_intermediate_reply(t, "x" * 1800)
        self.assertLessEqual(len(msg.reply.call_args.kwargs["content"]), 1500)
        await cb.send_reply(t, "x" * 1800, [])
        self.assertEqual(len(msg.reply.call_args.kwargs["content"]), 1800)

    async def test_outbound_mentions_leave_model_history_unchanged(self):
        rt = runtime()
        ch = Channel(members=[member(123)])
        raw = "Hello <@Alice>, `<@Alice>` ```<@Alice>``` <@456>"
        await messaging._send_split_text(rt, ch, raw)
        delivered = ch.send.call_args.kwargs["content"]
        self.assertIn("Hello <@123>", delivered)
        self.assertIn("`<@Alice>`", delivered)
        self.assertIn("```<@Alice>```", delivered)
        self.assertIn("<@456>", delivered)
        messaging._record_bot_reply(rt, 10, raw)
        self.assertIn("Hello <@Alice>", rt.channel_history[10][-1]["text"])

    async def test_mention_accounts_ambiguity_and_unclosed_code(self):
        rt = runtime()
        ch = Channel(members=[member(1, "alice", "same"), member(2, "bob", "same")])
        self.assertEqual(messaging._resolve_outbound_mentions(rt, ch, "<@same>"), "<@same>")
        self.assertEqual(messaging._resolve_outbound_mentions(rt, ch, "<@same[bob]>"), "<@2>")
        for raw in ("<@everyone>", "<@here>", "<@&123>", "```<@alice>", "`<@alice>"):
            self.assertEqual(messaging._resolve_outbound_mentions(rt, ch, raw), raw)

    async def test_transient_registry_is_bounded_and_unique(self):
        rt = runtime()
        for i in range(1100):
            context.register_transient_msg(rt, NS(id=i))
        context.register_transient_msg(rt, NS(id=1099))
        self.assertEqual(len(rt.transient_msg_ids), 1000)
        self.assertNotIn(0, rt.transient_msg_ids)


if __name__ == "__main__":
    unittest.main()
