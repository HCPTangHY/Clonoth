"""Goal submission is an authenticated API operation, not backend text parsing."""
import asyncio
from unittest.mock import patch

import httpx

from test_goal_integration import case, claim, finish, reviewer, verdict
from supervisor import admin_api
from supervisor.api import create_app
from supervisor.config_store import ConfigStore


def test_plain_inbound_and_preempt_do_not_interpret_goal_text(case):
    c = case
    async def run():
        with patch.object(admin_api, '_admin_token', 'goal-test-token'), patch.object(admin_api, '_web_auth', None):
            app = create_app(state=c.state, process_manager=None, config_store=ConfigStore(path=c.root / 'data/config.yaml'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                body = {'channel': 'web', 'conversation_key': 'web:test', 'text': '/goal build the feature',
                        'user_input': True, 'message_id': 'message-1', 'request_id': 'message-1'}
                assert (await client.post('/v1/inbound', json=body)).status_code == 401
                client.headers['Authorization'] = 'Bearer goal-test-token'
                response = await client.post('/v1/inbound', json=body)
                assert response.status_code == 200, response.text
                assert response.json()['handled'] is False
                assert c.service.read(c.sid)['status'] == 'empty'
                assert not c.state.execution_runtime._handlers
                item = c.state.assign_next_task(worker_id='worker')
                task = c.state.tasks[item['task_id']]
                old_seq = task.source_inbound_seq
                before = set(c.state.tasks)
                reply = await client.post(f'/v1/tasks/{task.task_id}/preempt', json={
                    'message': '/goal pause', 'user_input': True, 'request_id': 'ordinary-preempt'})
                assert reply.status_code == 200, reply.text
                assert not reply.json().get('handled')
                assert task.preempt_requested and '/goal pause' in task.preempt_message
                assert set(c.state.tasks) == before and task.source_inbound_seq == old_seq
                assert c.service.read(c.sid)['status'] == 'empty'
                assert not [e for e in c.state.eventlog.events if e['type'] == 'outbound_message']
    asyncio.run(run())


def test_replayed_control_input_without_outbound_is_never_model_work(case):
    # Other plugins may still use the generic durable direct-reply mechanism.
    c = case
    payload = {'channel': 'web', 'conversation_key': 'web:test', 'message_id': 'interrupted-control',
               'message_type': 'control_input', 'text': 'other plugin control'}
    event = c.state.eventlog.append(session_id=c.sid, component='supervisor', type_='inbound_message', payload=payload)
    c.state._apply_inbound_message(seq=event['seq'], session_id=c.sid, payload=payload)
    assert event['seq'] in c.state._inbound_processed
    assert c.state.assign_next_inbound(worker_id='restarted') is None
    assert not c.state.tasks


def test_goal_submit_api_starts_main_before_review_and_reuses_session_across_workspace(case):
    from fastapi import APIRouter
    from plugins.ide.goal import add_routes, _SERVICES
    c = case
    async def run():
        with patch.object(admin_api, '_admin_token', 'goal-api-token'), \
             patch.object(admin_api, '_web_auth', None), \
             patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}):
            app = create_app(state=c.state, process_manager=None, config_store=ConfigStore(path=c.root / 'data/config.yaml'))
            router = APIRouter()
            add_routes(router)
            app.include_router(router, prefix='/v1/plugins/ide')
            url = '/v1/plugins/ide/goal?session_id=' + c.sid
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                initial = c.service.read(c.sid)
                body = {'action': 'submit', 'goal': 'Check the work', 'criteria': 'PASS evidence',
                        'expected_session_id': c.sid, 'expected_revision': initial['revision'], 'request_id': 'api-submit'}
                assert (await client.post(url, json=body)).status_code == 401
                client.headers['Authorization'] = 'Bearer goal-api-token'
                for goal in ('', '   ', 'x' * 16001):
                    invalid = await client.post(url, json={**body, 'goal': goal})
                    assert invalid.status_code == 422, invalid.text
                assert c.service.read(c.sid) == initial and not c.state.tasks
                saved = await client.post(url, json=body)
                assert saved.status_code == 200, saved.text
                assert saved.json()['status'] == 'running' and not c.state.tasks
                assert (await client.post(url, json=body)).json() == saved.json()
                conflict = await client.post(url, json={**body, 'request_id': 'stale-submit', 'goal': 'different'})
                assert conflict.status_code == 409
                conflict = await client.post(url, json={**body, 'goal': 'different'})
                assert conflict.status_code == 409
                c.service.tick()
                tid = c.service.documents[c.sid]['_roots'][-1]
                assert c.state.tasks[tid].node_id == 'main'
                assert not any(t.input.get('_system_task') for t in c.state.tasks.values())
                claim(c, tid)
                finish(c, tid)
                rid = reviewer(c)
                before_version = c.service.read(c.sid)['goal_version']
                workspace = await client.put(f'/v1/sessions/{c.sid}/workspace', json={'name': 'other'})
                assert workspace.status_code == 200, workspace.text
                finish(c, rid, verdict(True), evidence=False)
                c.service.tick()
                paused = (await client.get(url)).json()
                assert paused['status'] == 'paused' and paused['goal_version'] == before_version
                assert paused['reviews'][-1]['verdict']['passed'] is True
                assert not paused['reviews'][-1]['applied']
                count = len(c.state.tasks)
                resumed = await client.post(url, json={'action': 'resume', 'expected_session_id': c.sid,
                    'expected_revision': paused['revision'], 'request_id': 'api-resume'})
                assert resumed.status_code == 200, resumed.text
                c.service.tick()
                done = (await client.get(url)).json()
                assert done['status'] == 'completed'
                assert done['rounds'] == paused['rounds'] and len(c.state.tasks) == count
    asyncio.run(run())
