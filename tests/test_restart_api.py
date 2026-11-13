"""Administrative restart entry tested with fake processes and a captured exit job."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from supervisor import admin_api
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from test_restart_supervisor import make_state, task


@pytest.mark.parametrize('failure', ['', 'storage', 'still_running'])
def test_admin_restart_seals_after_stop_only_and_deduplicates_request(tmp_path, failure):
    async def run():
        state = make_state(tmp_path, 'before')
        sid = state.get_or_create_session(channel='web', conversation_key='web:api')
        row = task(state, sid)
        jobs = []
        calls = []
        live = [True]
        proc = SimpleNamespace(popen=SimpleNamespace(poll=lambda: None if live[0] else 0))
        def stop():
            calls.append('stop')
            assert json.loads(state.restart_recovery.path.read_text())['phase'] == 'preparing'
            live[0] = failure == 'still_running'
        manager = SimpleNamespace(engines=[proc], stop_engine=stop, _restart_pending=False)
        def thread(*, target, **kwargs):
            return SimpleNamespace(start=lambda: jobs.append(target))
        with patch.object(admin_api, '_admin_token', 'restart-api-test'), patch.object(admin_api, '_web_auth', None):
            app = create_app(state=state, process_manager=manager, config_store=ConfigStore(path=tmp_path/'data/config.yaml'))
            app.state._uvicorn_server = SimpleNamespace(should_exit=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                         headers={'Authorization': 'Bearer restart-api-test'}) as http:
                with patch('supervisor.api.threading.Thread', side_effect=thread):
                    for target in ['engine', 'all']:
                        response = await http.post('/v1/admin/restart', json={'target': target})
                        assert response.status_code == 200 and response.json()['scheduled']
                assert len(jobs) == 1
                assert state.assign_next_task(worker_id='w') is None
                with patch('supervisor.api.time.sleep'):
                    if failure == 'storage':
                        with patch.object(state.restart_recovery, 'finalize', side_effect=OSError('disk full')):
                            jobs[0]()
                    else:
                        jobs[0]()
        assert calls == ['stop']
        saved = json.loads(state.restart_recovery.path.read_text())
        assert saved['phase'] == ('preparing' if failure else 'ready')
        assert app.state._uvicorn_server.should_exit is (not failure)
        assert manager._restart_pending is (not failure)
        assert list(state.tasks) == [row.task_id]
        assert not [e for e in state.eventlog.events if e['type'] == 'inbound_message']
        assert bool([e for e in state.eventlog.events if e['type'] == 'restart_failed']) == bool(failure)
    asyncio.run(run())
