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


def test_process_manager_stops_engines_in_parallel_and_kills_only_survivors(tmp_path):
    from subprocess import TimeoutExpired
    from supervisor.process_manager import ManagedProcess, ProcessManager

    events = []

    class FakePopen:
        def __init__(self, pid, *, hangs=False):
            self.pid = pid
            self.hangs = hangs
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            events.append(('terminate', self.pid))
            if not self.hangs:
                self.stopped = True

        def wait(self, timeout=None):
            events.append(('wait', self.pid, round(float(timeout or 0), 2)))
            if not self.stopped:
                raise TimeoutExpired(cmd='engine', timeout=timeout or 0)
            return 0

        def kill(self):
            events.append(('kill', self.pid))
            self.stopped = True

    with patch('supervisor.process_manager.atexit.register'), patch.object(ProcessManager, '_install_signal_handlers'):
        pm = ProcessManager(supervisor_url='http://127.0.0.1:8765', workspace_root=tmp_path, log_dir=tmp_path / 'logs')
    assert pm.stop_wait_timeout_sec == 20.0
    pm.stop_wait_timeout_sec = 0.05
    p1 = FakePopen(101, hangs=False)
    p2 = FakePopen(102, hangs=True)
    pm.engines = [ManagedProcess('engine-1', p1, None), ManagedProcess('engine-2', p2, None)]
    pm.stop_engine()
    # Both processes receive SIGTERM before any blocking wait runs.
    assert events[:2] == [('terminate', 101), ('terminate', 102)]
    assert ('kill', 101) not in events
    assert ('kill', 102) in events
    assert pm.engines == []
