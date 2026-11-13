"""Goal view identity is read-only; entry aliases never grant child sessions parent control."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import APIRouter, FastAPI

from plugins.ide.goal import _SERVICES, add_routes
from test_goal_integration import case, control


def test_goal_api_echoes_requested_view_and_normalizes_only_entry_branches(case):
    c = case
    control(c, 'set', goal='Parent evidence review')
    before = c.service.read(c.sid)
    path = c.service._path(c.sid)
    durable = path.read_bytes()
    registry = c.state._session_store._registry
    registry['entry-view'] = {'session_id': 'entry-view', 'node_id': '__entry_branch__', 'parent_session_id': c.sid}
    registry['real-child'] = {'session_id': 'real-child', 'parent_session_id': c.sid, 'is_child': True}
    child = copy.copy(c.state.sessions[c.sid])
    child.session_id = 'real-child'
    c.state.sessions['real-child'] = child
    app = FastAPI()
    app.state.state = SimpleNamespace(execution_runtime=c.state.execution_runtime)
    router = APIRouter()
    add_routes(router)
    app.include_router(router, prefix='/v1/plugins/ide')

    async def run():
        with patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for viewed in (c.sid, 'entry-view'):
                    response = await client.get('/v1/plugins/ide/goal', params={'session_id': viewed})
                    assert response.status_code == 200, response.text
                    data = response.json()
                    assert data['requested_session_id'] == viewed
                    assert data['session_id'] == c.sid and not data['is_child']
                    assert data['goal'] == before['goal'] and data['revision'] == before['revision']
                response = await client.get('/v1/plugins/ide/goal', params={'session_id': 'real-child'})
                assert response.status_code == 200
                data = response.json()
                assert data['session_id'] == data['requested_session_id'] == 'real-child'
                assert data['is_child'] and not data['goal']
                assert path.read_bytes() == durable
                assert not c.service._path('real-child').exists()
                body = {'action': 'clear', 'expected_session_id': 'real-child',
                        'expected_revision': data['revision'], 'request_id': 'child-must-not-clear-parent'}
                response = await client.post('/v1/plugins/ide/goal', params={'session_id': 'real-child'}, json=body)
                assert response.status_code == 403
                assert c.service.read(c.sid) == before and path.read_bytes() == durable
                body.update(expected_session_id='entry-view', expected_revision=before['revision'], request_id='alias-must-use-owner-cas')
                response = await client.post('/v1/plugins/ide/goal', params={'session_id': 'entry-view'}, json=body)
                assert response.status_code == 409
                assert c.service.read(c.sid) == before
                body.update(expected_session_id=c.sid, request_id='explicit-parent-clear')
                response = await client.post('/v1/plugins/ide/goal', params={'session_id': c.sid}, json=body)
                assert response.status_code == 200 and response.json()['status'] == 'empty'
    asyncio.run(run())
