"""Final Goal failure/replay edges. No live services or external model calls."""
import copy
import json
import os
import stat
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from plugins.ide.goal import GoalService
from test_goal_plugin import env, command, launch, candidate, answer


def test_runtime_transaction_failure_latches_without_killing_thread(env, monkeypatch):
    launch(env)
    service = env.service
    attempts = []
    settled = threading.Event()

    @contextmanager
    def unavailable():
        attempts.append(1)
        raise ValueError('obligations unavailable')
        yield

    original_tick = service.tick

    def tick():
        original_tick()
        settled.set()

    with monkeypatch.context() as patch:
        patch.setattr(env.rt, 'transaction', unavailable)
        patch.setattr(service, 'tick', tick)
        service.thread = threading.Thread(target=service._run, daemon=True)
        service.thread.start()
        service.wake.set()
        assert settled.wait(2)
        assert service.thread.is_alive()
        assert service.documents['main']['status'] == 'blocked'
        for _ in range(10):
            service.tick()
        assert len(attempts) == 1
        with pytest.raises(HTTPException) as exc:
            service.read('main')
        assert exc.value.status_code == 503
        service.stopped.set()
        service.wake.set()
        service.thread.join(2)
        assert not service.thread.is_alive()
    # Allow the fixture's close() to dispose subscriptions normally.
    service.stopped.clear()
    service.tick()
    assert len(env.rt.inputs) == 1  # Repair alone does not reauthorize execution.


def test_advance_and_pause_failure_still_blocks_without_repeated_work(env, monkeypatch):
    launch(env)
    calls = []

    def fail(data):
        calls.append(data)
        raise OSError('disk unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(env.service, '_advance', fail)
        patch.setattr(env.rt, 'set_paused', lambda *args: fail(args))
        env.service.tick()
        count = len(calls)
        env.service.tick()
        assert len(calls) == count
        assert env.service.read('main')['status'] == 'blocked'
    assert len(env.rt.inputs) == 1


@pytest.mark.parametrize('phase', ['replace', 'directory_fsync'])
def test_save_fault_blocks_without_mutating_attempt_revision(env, monkeypatch, phase):
    command(env, 'set', goal='Keep goal')
    original = copy.deepcopy(env.service.documents['main'])
    attempted = copy.deepcopy(original)
    attempted['status'] = 'running'
    fsync = os.fsync

    def fail_replace(*args):
        raise OSError('replace failed')

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory fsync failed')
        return fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr('plugins.ide.goal.os.' + ('replace' if phase == 'replace' else 'fsync'),
                      fail_replace if phase == 'replace' else fail_directory)
        with pytest.raises(OSError):
            env.service._save(attempted)
        assert attempted['revision'] == original['revision']
        assert env.service.read('main')['status'] == 'blocked'
        env.service.tick()
        assert not env.rt.inputs
    assert not list((env.root / 'data/temp').glob('goal-*.tmp'))


def test_stale_candidate_verdict_does_not_starve_new_review(env):
    launch(env)
    first = candidate(env)
    command(env, 'pause')
    answer(env, first, passed=True)
    data = env.service.documents['main']
    old = copy.deepcopy(data['reviews'][-1])
    data['_candidate']['candidate_id'] = 'new-final-candidate'
    # Same execution snapshot, different final response identity.
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.reviews) == 2
    assert env.service.read('main')['status'] == 'reviewing'
    assert env.service.documents['main']['reviews'][-1] == old
    answer(env, env.rt.reviews[-1][0], passed=True)
    assert env.service.read('main')['status'] == 'completed'


def test_submit_replay_preserves_original_request_without_reauthorizing(env):
    body = {'action': 'submit', 'goal': 'original goal', 'expected_session_id': 'main',
            'expected_revision': 0, 'request_id': 'submit-id'}
    first = env.service.operate('main', body)
    assert first['status'] == 'running'
    assert env.service.operate('main', body) == first
    with pytest.raises(HTTPException) as exc:
        env.service.operate('main', {**body, 'goal': 'other goal'})
    assert exc.value.status_code == 409
    env.service.tick()
    command(env, 'pause')
    before = env.service.read('main')
    assert env.service.operate('main', body) == before
    env.service.tick()
    assert env.service.read('main')['status'] == 'paused'
    assert len(env.rt.inputs) == 1


def test_http_operation_still_enforces_original_cas_request_body(env):
    body = {'action': 'set', 'goal': 'unchanged', 'expected_session_id': 'main',
            'expected_revision': 0, 'request_id': 'api-id'}
    result = env.service.operate('main', body)
    assert env.service.operate('main', body) == result
    with pytest.raises(HTTPException) as exc:
        env.service.operate('main', {**body, 'expected_revision': result['revision']})
    assert exc.value.status_code == 409


@pytest.mark.parametrize('content', ['引号"反斜杠\\控制\u0000\n' * 6000, 'plain text' * 8000])
def test_actual_serialized_instruction_obeys_context_budget(env, monkeypatch, content):
    monkeypatch.setattr('engine.node.load_node', lambda *args: SimpleNamespace(extra={'context_limit': 3072}))
    root = launch(env)
    candidate(env)
    command(env, 'pause')
    answer(env, env.rt.reviews[-1][0], invalid=True)
    env.rt.facts[root] = {'version': 2, 'evidence': [{'source_task_id': root, 'content': content}]}
    command(env, 'resume')
    env.service.tick()
    data = env.service.read('main')
    assert data['status'] == 'paused'
    assert 'context_limit' in data['pause_reason']
    assert len(env.rt.reviews) == 1
    assert len(env.rt.inputs) == 1
    env.service.tick()
    assert len(env.rt.reviews) == 1


def test_core_missing_evidence_flag_prevents_pass(env, monkeypatch):
    launch(env)
    snapshot = env.rt.snapshot
    monkeypatch.setattr(env.rt, 'snapshot', lambda roots: {**snapshot(roots), 'evidence_missing': True})
    tid = env.rt.inputs[-1][0]
    env.rt.tasks[tid]['status'] = 'completed'
    env.service.on_event({'kind': 'candidate', 'session_id': 'main', 'candidate': {
        'entry_task_id': tid, 'session_id': 'main', 'result': {'text': 'done'}}})
    env.service.tick()
    data = env.service.read('main')
    assert data['status'] == 'paused'
    assert '完整过程' in data['pause_reason']
    assert not env.rt.reviews
    assert len(env.rt.inputs) == 1


def test_user_input_cannot_use_old_verdict_to_bypass_exhausted_budget(env):
    launch(env)
    tid = candidate(env)
    command(env, 'pause')
    answer(env, tid, passed=True)
    env.service.documents['main']['rounds'] = 8
    env.service.on_event({'kind': 'input', 'session_id': 'main'})
    with pytest.raises(HTTPException) as exc:
        command(env, 'resume')
    assert exc.value.status_code == 409
    assert env.service.read('main')['rounds'] == 8
    assert len(env.rt.inputs) == 1


@pytest.mark.parametrize('created_before_failure', [False, True])
def test_continuation_failure_keeps_verdict_feedback_and_stable_request(env, monkeypatch, created_before_failure):
    launch(env)
    first = candidate(env)
    submit = env.rt.submit_input

    def fail(*args, **kwargs):
        if created_before_failure:
            submit(*args, **kwargs)
        raise OSError('input acknowledgement uncertain')

    with monkeypatch.context() as patch:
        patch.setattr(env.rt, 'submit_input', fail)
        answer(env, first)
        data = env.service.documents['main']
        assert data['status'] == 'blocked'
        assert data['reviews'][-1]['applied'] is True
        pending = copy.deepcopy(data['_pending_input'])
        assert 'provide evidence' in pending['text'] and 'test output' in pending['text']
        assert pending['round'] == 1
        count = len(env.rt.inputs)
        env.service.tick()
        assert len(env.rt.inputs) == count
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.inputs) == 2  # Core replay is keyed by the same request, never new work.
    assert env.service.read('main')['rounds'] == 1
    assert env.rt.inputs[-1][1] == pending['text']


def test_lost_review_after_restart_retains_record_and_bounded_retry(env):
    launch(env)
    first = candidate(env)
    env.service.close()
    env.rt.tasks.pop(first)
    env.service = GoalService(env.rt, env.root, background=False)
    env.service.tick()
    record = env.service.read('main')['reviews'][-1]
    assert record['task_id'] == first and record['error']
    assert env.service.read('main')['status'] == 'paused'
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.reviews) == 2
    env.rt.tasks.pop(env.rt.reviews[-1][0])
    env.service.tick()
    for _ in range(3):
        command(env, 'resume')
        env.service.tick()
    assert len(env.rt.reviews) == 2
    assert len(env.service.read('main')['reviews']) == 2
    assert env.service.read('main')['status'] == 'paused'
