"""Actual Goal input route and runtime: identity, dedupe, auth and unchanged task."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import APIRouter, FastAPI
import httpx

from plugins.ide.goal import _SERVICES, add_routes
from test_goal_integration import case, start, claim, control


def test_goal_preempt_route_checks_identity_and_deduplicates(case):
    c = case
    root = start(c)
    claim(c, root)
    app = FastAPI()
    app.state.state = SimpleNamespace(execution_runtime=c.state.execution_runtime)
    router = APIRouter()
    add_routes(router)
    app.include_router(router, prefix='/v1/plugins/ide')

    async def run():
        with patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}), \
                patch('supervisor.admin_api.get_admin_token', return_value='test-admin'), \
                patch('supervisor.admin_api.get_web_auth', return_value=None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                data = (await client.get('/v1/plugins/ide/goal', params={'session_id': c.sid})).json()
                target = data['preempt_target']
                assert target['task_id'] == root
                body = {'goal_id': data['goal_id'], 'expected_task_id': root,
                        'expected_execution_id': target['execution_id'],
                        'expected_conversation_key': target['conversation_key'],
                        'request_id': 'user-input-1', 'message': 'Keep the original API compatible',
                        'attachments': [{'path': 'data/attachments/context.txt'}]}
                url = '/v1/plugins/ide/goal/preempt?session_id=' + c.sid
                assert (await client.post(url, json=body)).status_code == 401
                client.headers['Authorization'] = 'Bearer test-admin'
                for fields in [{'goal_id': 'wrong'}, {'expected_task_id': 'other'},
                               {'expected_execution_id': 'stale'}, {'expected_conversation_key': 'other'}]:
                    assert (await client.post(url, json={**body, **fields})).status_code == 409
                assert not c.state.tasks[root].preempt_requested
                before_tasks = set(c.state.tasks)
                response = await client.post(url, json=body)
                assert response.status_code == 200, response.text
                assert response.json()['task_id'] == root
                assert response.json()['source_inbound_seq'] == target['source_inbound_seq']
                task = c.state.tasks[root]
                assert task.preempt_message == body['message']
                assert task.preempt_attachments == body['attachments']
                assert set(c.state.tasks) == before_tasks
                current = c.service.read(c.sid)
                assert current['status'] in {'running', 'waiting'}
                assert current['goal_version'] == data['goal_version'] + 1
                duplicate = await client.post(url, json=body)
                assert duplicate.status_code == 200, duplicate.text
                assert c.service.read(c.sid)['goal_version'] == current['goal_version']
                assert task.preempt_message == body['message']
                assert task.preempt_attachments == body['attachments']
                assert (await client.post(url, json={**body, 'message': 'different'})).status_code == 409
                assert (await client.post(url, content='{')).status_code == 422
                assert (await client.post(url, json={**body, 'attachments': [1]})).status_code == 422
                control(c, 'pause')
                assert (await client.post(url, json={**body, 'request_id': 'paused-new'})).status_code == 409
    asyncio.run(run())
