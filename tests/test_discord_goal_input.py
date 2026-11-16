"""Discord Goal preempt routing with real SDK HTTP and simulated Discord transport."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from adapters.discord import agent
from adapters.discord.extensions.ide_goal_input import preempt_current
from clonoth_sdk.client import ClonothClient
from test_discord_forward_migration import runtime, message, trigger, member


class GoalInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rt = runtime()
        self.message = message(mid=22)
        self.rt.session_state.register_session('discord:10', 's')
        self.calls = []
        self.goal = {'session_id': 's', 'is_child': False, 'status': 'waiting', 'goal_id': 'goal-1',
                     'execution': {'task_id': 'root'}, 'preempt_target': {
                         'task_id': 'output-leaf', 'execution_id': 'exec-1', 'session_id': 's',
                         'conversation_key': 'discord:10', 'source_inbound_seq': 70}}
        self.tasks = [NS(task_id='other', is_user_entry=True, source_inbound_seq=7),
                      NS(task_id='output-leaf', is_user_entry=False, source_inbound_seq=70)]
        self.get_error = None
        self.post_error = None
        self.post_reply = {'ok': True, 'session_id': 's', 'task_id': 'output-leaf', 'source_inbound_seq': 70}
        self.client = ClonothClient('https://offline.invalid')
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        self.rt.clonoth_client = self.client

    async def asyncTearDown(self):
        await self.client.close()

    async def http(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.method == 'GET' and request.url.path == '/v1/plugins/ide/goal':
            if self.get_error:
                return httpx.Response(self.get_error, json={'detail': 'unavailable'})
            return httpx.Response(200, json=self.goal)
        if request.method == 'POST' and request.url.path == '/v1/plugins/ide/goal/preempt':
            if self.post_error == 'network':
                raise httpx.ReadTimeout('lost acknowledgement', request=request)
            if self.post_error:
                return httpx.Response(self.post_error, json={'detail': 'failed'})
            return httpx.Response(200, json=self.post_reply)
        raise AssertionError(str(request.url))

    async def route(self, **kwargs):
        return await preempt_current(self.rt, self.message, 's', 'discord:10', self.tasks,
                                     'new instructions', [{'path': 'data/attachments/a.txt'}], is_dm=kwargs.get('is_dm', False))

    def posts(self):
        return [row for row in self.calls if row[0] == 'POST']

    async def test_exact_output_leaf_without_trigger_not_first_task(self):
        self.assertTrue(await self.route())
        body = self.posts()[0][2]
        self.assertEqual(body['expected_task_id'], 'output-leaf')
        self.assertEqual(body['expected_execution_id'], 'exec-1')
        self.assertEqual(body['expected_conversation_key'], 'discord:10')
        self.assertEqual(body['request_id'], 'discord-input:10:22')
        self.assertEqual(body['attachments'], [{'path': 'data/attachments/a.txt'}])
        self.assertFalse(self.rt.session_state.triggers)
        self.assertIn('追加指令已交给', self.message.reply.call_args.args[0])

    async def test_identity_proof_is_required(self):
        original = copy.deepcopy(self.goal)
        for patch_data in [
            {'session_id': 'foreign'}, {'is_child': True}, {'status': 'reviewing'},
            {'goal_id': ''}, {'execution': None}, {'preempt_target': None},
        ]:
            with self.subTest(patch_data=patch_data):
                self.goal = {**copy.deepcopy(original), **patch_data}
                self.assertFalse(await self.route())
        self.assertFalse(self.posts())

    async def test_foreign_target_missing_identity_or_wrong_sequence_is_rejected(self):
        original = copy.deepcopy(self.goal['preempt_target'])
        for change in [{'session_id': 'elsewhere'}, {'conversation_key': 'discord:11'},
                       {'execution_id': ''}, {'task_id': 'review'}, {'source_inbound_seq': 71},
                       {'source_inbound_seq': True}, {'source_inbound_seq': 0}]:
            with self.subTest(change=change):
                self.goal['preempt_target'] = {**original, **change}
                self.assertFalse(await self.route())
        self.assertFalse(self.posts())

    async def test_duplicate_running_target_is_not_guessed(self):
        self.tasks.append(self.tasks[-1])
        self.assertFalse(await self.route())
        self.assertFalse(self.posts())

    async def test_ordinary_author_rule_is_preserved(self):
        previous = trigger(self.rt, message(author=member(2)), seq=70)
        self.assertFalse(await self.route())
        self.assertFalse(self.posts())
        self.assertEqual(previous.platform_data['message'].author.id, 2)
        self.message.author = member(2)
        self.assertTrue(await self.route())
        self.assertIs(self.rt.session_state.get_trigger(70), previous)
        self.assertEqual(previous.platform_data['message'].id, 21)

    async def test_goal_background_card_is_not_replaced(self):
        previous = trigger(self.rt, message(author=member(2)), seq=70)
        previous.platform_data['background'] = True
        previous.platform_data['status_msg'] = NS(delete=AsyncMock())
        self.assertTrue(await self.route())
        self.assertIs(self.rt.session_state.get_trigger(70), previous)
        previous.platform_data['status_msg'].delete.assert_not_awaited()
        self.assertEqual(previous.platform_data['message'].id, 21)

    async def test_stale_channel_binding_is_rejected(self):
        self.rt.session_state.register_session('discord:10', 'new-session')
        self.assertFalse(await self.route())
        self.assertFalse(self.posts())

    async def test_get_failure_is_safe_to_fall_back_before_any_delivery(self):
        self.get_error = 404
        self.assertFalse(await self.route())
        self.assertFalse(self.posts())

    async def test_uncertain_and_conflicting_post_are_handled_not_fallback(self):
        for error in ['network', 503, 409]:
            with self.subTest(error=error):
                self.post_error = error
                self.assertTrue(await self.route())
        self.assertEqual(len(self.posts()), 3)
        self.assertIn('本次未另建任务', self.message.reply.call_args.args[0])

    async def test_malformed_success_is_not_reported_as_confirmed(self):
        self.post_reply = {'ok': True, 'session_id': 's', 'task_id': 'different', 'source_inbound_seq': 70}
        self.assertTrue(await self.route())
        self.assertIn('未确认', self.message.reply.call_args.args[0])

    async def test_discord_ack_failure_never_requests_redelivery(self):
        self.message.reply.side_effect = RuntimeError('Discord unavailable')
        self.assertTrue(await self.route())
        self.assertEqual(len(self.posts()), 1)

    async def test_real_entry_never_falls_back_after_goal_post(self):
        for error in [None, 'network', 409, 503]:
            with self.subTest(error=error):
                self.post_error = error
                self.calls.clear()
                with patch.object(self.client, 'get_running_tasks', AsyncMock(return_value=self.tasks)), \
                     patch.object(self.client, 'preempt_task', AsyncMock()) as ordinary, \
                     patch.object(self.client, 'submit_inbound', AsyncMock()) as inbound, \
                     patch.object(agent, '_collect_attachments', AsyncMock(return_value=[])):
                    await agent._handle_agent_inner(self.rt, self.message, 'continue', 10, 'discord:10')
                self.assertEqual(len(self.posts()), 1)
                ordinary.assert_not_awaited()
                inbound.assert_not_awaited()

    async def test_ordinary_preempt_completion_during_new_status_send_is_not_replayed(self):
        rt = runtime()
        current = message(mid=22)
        old = trigger(rt, message(channel=current.channel), seq=7)
        rt.clonoth_client.get_running_tasks.return_value = [NS(task_id='ordinary', is_user_entry=True, source_inbound_seq=7)]
        async def acknowledge(*args, **kwargs):
            rt.session_state.consume_trigger(7)
            return NS(id=99, delete=AsyncMock())
        current.reply.side_effect = acknowledge
        with patch.object(agent, '_collect_attachments', AsyncMock(return_value=[])):
            await agent._handle_agent_inner(rt, current, 'continue', 10, 'discord:10')
        rt.clonoth_client.preempt_task.assert_awaited_once()
        rt.clonoth_client.submit_inbound.assert_not_awaited()
        self.assertIsNone(rt.session_state.get_trigger(7))
