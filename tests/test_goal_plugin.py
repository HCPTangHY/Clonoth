"""Goal policy unit tests; host lifecycle is covered by test_goal_integration.py."""
import copy
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from plugins.ide.goal import GoalService, parse_verdict


class Runtime:
    def __init__(self, root):
        self.workspace_root = root
        self.ready = True
        self.lock = threading.RLock()
        self.callbacks = []
        self.handlers = []
        self.tasks = {}
        self.facts = {}
        self.inputs = []
        self.reviews = []
        self.paused = set()
        self.forgotten = []
        self.events = []
        self.requests = {}

    @contextmanager
    def transaction(self):
        with self.lock:
            yield self

    def subscribe(self, cb):
        self.callbacks.append(cb)
        return lambda: self.callbacks.remove(cb)

    def register_input_handler(self, cb, *, matches=None):
        self.handlers.append(cb)
        return lambda: self.handlers.remove(cb)

    def emit_plugin_event(self, kind, payload):
        self.events.append((kind, payload))

    def session_info(self, sid):
        return {"session_id": "main" if sid == "branch" else sid, "is_child": sid == "child", "generation": 1, "workspace": {}}

    def watch(self, roots):
        pass

    def forget(self, roots):
        self.forgotten.extend(roots)

    def set_paused(self, roots, paused):
        self.paused.update(roots) if paused else self.paused.difference_update(roots)

    def snapshot(self, roots):
        tasks = [copy.deepcopy(self.tasks[r]) for r in roots if r in self.tasks]
        facts = [self.facts.get(r, {}) for r in roots]
        return {"tasks": tasks, "version": str([(r, self.tasks.get(r, {}).get('status'), self.facts.get(r, {}).get('version', 1)) for r in roots]),
                "busy": any(t['status'] in ('pending', 'running') for t in tasks),
                "unresolved": [t for t in tasks if t['status'] in ('failed', 'lost', 'cancelled')],
                "evidence": [e for f in facts for e in f.get('evidence', [])], "evidence_truncated": False,
                "truncated_task_ids": [], "outputs": [e for f in facts for e in f.get('outputs', [])]}

    def submit_input(self, sid, text, request_id, origin_task_id=""):
        if request_id in self.requests:
            return self.requests[request_id]
        tid = 'input-' + str(len(self.inputs))
        self.tasks[tid] = {'task_id': tid, 'status': 'running', 'result': {}}
        self.inputs.append((tid, text, origin_task_id))
        result = {'task_id': tid, 'session_id': sid, 'inbound_seq': len(self.inputs)}
        self.requests[request_id] = result
        return result

    def create_system_task(self, sid, node_id, instruction, request_id, node_overrides=None):
        if request_id in self.requests:
            return self.requests[request_id]
        tid = 'review-' + str(len(self.reviews))
        self.tasks[tid] = {'task_id': tid, 'status': 'running', 'result': {}}
        self.reviews.append((tid, json.loads(instruction), node_overrides))
        result = {'task_id': tid, 'session_id': 'review-session-' + tid}
        self.requests[request_id] = result
        return result

    def cancel_system_task(self, tid):
        self.tasks[tid]['status'] = 'cancelled'


@pytest.fixture
def env(tmp_path):
    rt = Runtime(tmp_path)
    service = GoalService(rt, tmp_path, background=False)
    yield SimpleNamespace(rt=rt, service=service, root=tmp_path)
    service.close()


def command(e, action, **fields):
    current = e.service.read('main')
    return e.service.operate('main', {'action': action, 'expected_session_id': 'main',
        'expected_revision': current['revision'], 'request_id': f'{action}-{current["revision"]}', **fields})


def launch(e):
    command(e, 'set', goal='Make checker pass', criteria='Return proof')
    command(e, 'start')
    e.service.tick()
    return e.rt.inputs[-1][0]


def candidate(e):
    tid = e.rt.inputs[-1][0]
    e.rt.tasks[tid].update(status='completed', result={'action': 'finish', 'result': {'text': 'done'}})
    e.rt.facts[tid] = {'evidence': [{'role': 'tool', 'content': 'PASS', 'source_task_id': tid}],
                      'outputs': [{'id': tid + ':finish', 'task_id': tid, 'kind': 'finish', 'text': 'done', 'sequence': len(e.rt.inputs)}]}
    e.service.on_event({'kind': 'candidate', 'session_id': 'main', 'candidate': {'entry_task_id': tid,
        'session_id': 'main', 'producer_task_id': tid, 'result': {'text': 'done'}}})
    e.service.tick()
    return e.rt.reviews[-1][0]


def answer(e, tid, *, passed=False, progress=True, invalid=False):
    text = 'broken' if invalid else json.dumps({'passed': passed, 'progress': progress,
        'feedback': 'proof verified' if passed else 'provide evidence', 'missing_evidence': [] if passed else ['test output'], 'needs_user': False})
    e.rt.tasks[tid].update(status='completed', result={'action': 'finish', 'result': {'text': text}})
    e.service.tick()


def test_eight_continuations_not_nine(env):
    e = env
    launch(e)
    for _ in range(9):
        tid = candidate(e)
        answer(e, tid)
    data = e.service.read('main')
    assert data['rounds'] == 8 and len(e.rt.inputs) == 9
    assert data['status'] == 'paused' and data['reviews'][-1]['verdict']['passed'] is False
    with pytest.raises(HTTPException) as err:
        command(e, 'resume')
    assert err.value.status_code == 409


def test_three_stagnant_reviews_pause(env):
    launch(env)
    for _ in range(3):
        answer(env, candidate(env), progress=False)
    data = env.service.read('main')
    assert data['stagnation'] == 3 and data['rounds'] == 2
    assert data['status'] == 'paused'


def test_pause_retains_verdict_and_same_version_resume(env):
    launch(env)
    tid = candidate(env)
    before = env.service.read('main')['goal_version']
    env.service.on_event({'kind': 'workspace', 'session_id': 'main'})
    answer(env, tid, passed=True)
    assert env.service.read('main')['status'] == 'paused'
    assert env.service.read('main')['goal_version'] == before
    command(env, 'resume')
    env.service.tick()
    assert env.service.read('main')['status'] == 'completed'
    assert len(env.rt.reviews) == 1


def test_technical_retries_do_not_reset_after_pause(env):
    launch(env)
    tid = candidate(env)
    command(env, 'pause')
    answer(env, tid, invalid=True)
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.reviews) == 2
    tid = env.rt.reviews[-1][0]
    command(env, 'pause')
    answer(env, tid, invalid=True)
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.reviews) == 2
    assert env.service.read('main')['status'] == 'paused'


def test_previous_verdict_provided_to_next_review(env):
    launch(env)
    answer(env, candidate(env), progress=True)
    candidate(env)
    payload = env.rt.reviews[-1][1]
    assert payload['current']['previous_review']['feedback'] == 'provide evidence'
    assert payload['completed_outputs'][0]['text'] == 'done'
    assert payload['current']['latest_main_task']['trace'][0]['content'] == 'PASS'


def test_unknown_fields_cas_and_request_identity(env):
    current = env.service.read('main')
    patch = {'action': 'set', 'expected_session_id': 'main', 'expected_revision': current['revision'],
             'request_id': 'same', 'goal': 'do work'}
    first = env.service.operate('main', patch)
    assert env.service.operate('main', patch) == first
    for bad in ({**patch, 'goal': 'changed'}, {**patch, 'request_id': 'new', 'workspace': 'other'}):
        with pytest.raises(HTTPException):
            env.service.operate('main', bad)
    with pytest.raises(HTTPException) as err:
        env.service.operate('child', patch)
    assert err.value.status_code == 403
    assert env.service.read('branch')['session_id'] == 'main'


def test_recovery_preserves_failure_and_starts_new_root(env):
    first = launch(env)
    env.rt.tasks[first]['status'] = 'lost'
    env.service.tick()
    assert env.service.read('main')['status'] == 'paused'
    command(env, 'resume')
    env.service.tick()
    second = env.rt.inputs[-1][0]
    data = env.service.documents['main']
    assert second != first and data['_roots'] == [second]
    assert data['_prior_runs'] == [[first]] and data['_interruptions'][0]['status'] == 'lost'
    assert '中断或失败' in env.rt.inputs[-1][1]


def test_restart_pauses_without_resetting_budget(env):
    launch(env)
    answer(env, candidate(env))
    count = env.service.read('main')['rounds']
    env.service.close()
    restored = GoalService(env.rt, env.root, background=False)
    try:
        assert restored.read('main')['status'] == 'paused'
        assert restored.read('main')['rounds'] == count
    finally:
        restored.close()
    env.service = restored


def test_clear_preserves_todo_and_drops_goal_content(env):
    tid = launch(env)
    todo = env.root / 'data/ide/todos/sessions/untouched.json'
    todo.parent.mkdir(parents=True)
    todo.write_text('keep')
    command(env, 'clear')
    data = env.service.read('main')
    assert data['status'] == 'empty' and data['goal'] == '' and data['reviews'] == []
    assert tid in env.rt.forgotten and todo.read_text() == 'keep'


def test_background_close_releases_subscriptions_and_thread(tmp_path):
    runtime = Runtime(tmp_path)
    service = GoalService(runtime, tmp_path, background=True)
    service.close()
    service.thread.join(timeout=2)
    assert not service.thread.is_alive()
    assert not runtime.callbacks and not runtime.handlers
    before = len(runtime.inputs)
    service.tick()
    assert len(runtime.inputs) == before
    replacement = GoalService(runtime, tmp_path, background=True)
    replacement.close()
    replacement.thread.join(timeout=2)
    assert not replacement.thread.is_alive()


def test_pause_write_failure_revokes_local_advance_permission(env, monkeypatch):
    launch(env)
    tid = candidate(env)
    monkeypatch.setattr(env.service, '_save', lambda data: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        env.service.on_event({'kind': 'workspace', 'session_id': 'main'})
    assert env.service.documents['main']['status'] == 'paused'
    assert env.service.documents['main']['_roots'][0] in env.rt.paused
    assert env.rt.tasks[tid]['status'] == 'running'


def test_user_input_invalidates_candidate_without_changing_goal_version(env):
    launch(env)
    tid = candidate(env)
    version = env.service.read('main')['goal_version']
    env.service.on_event({'kind': 'input', 'session_id': 'main'})
    answer(env, tid, passed=True)
    command(env, 'resume')
    env.service.tick()
    data = env.service.read('main')
    assert data['status'] == 'running' and data['goal_version'] == version
    assert len(env.rt.inputs) == 2
    assert data['reviews'][-1]['applied'] is False


def test_goal_registers_no_text_command_and_keeps_strict_verdict(env):
    assert not env.rt.handlers
    assert not hasattr(env.service, 'command')
    with pytest.raises(ValueError):
        parse_verdict('{"passed":true}')
    with pytest.raises(ValueError):
        parse_verdict(json.dumps({'passed': True, 'progress': True, 'feedback': 'x', 'missing_evidence': ['x'], 'needs_user': False}))
