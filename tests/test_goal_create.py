"""Independent Goal creation uses real runtime/storage; no external services.

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest tests/test_goal_create.py -q
"""
import asyncio
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest
from fastapi import APIRouter, HTTPException

from plugins.ide.goal import GoalService
from test_goal_integration import case, claim, control, finish, reviewer, start, verdict


def request(c, request_id='create-1', goal='Independent target'):
    return {'action': 'create', 'goal': goal, 'expected_session_id': c.sid,
            'expected_revision': c.service.read(c.sid)['revision'], 'request_id': request_id}


def completed(c):
    root = start(c)
    claim(c, root)
    finish(c, root)
    review_id = reviewer(c)
    finish(c, review_id, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'
    return root, review_id


def test_create_capability_is_public_only_and_empty_creation_starts_via_tick(case):
    c = case
    assert c.service.read(c.sid)['capabilities']['create'] is True
    before = c.service.read(c.sid)
    body = request(c)
    created = c.service.operate(c.sid, body)
    assert created['status'] == 'running'
    assert created['revision'] == before['revision'] + 1
    assert created['goal_version'] == before['goal_version'] + 1
    assert (created['rounds'], created['stagnation']) == (0, 0)
    assert not c.state.tasks
    assert 'capabilities' not in json.loads(c.service._path(c.sid).read_text())
    c.service.tick()
    root = c.service.documents[c.sid]['_roots'][0]
    assert c.state.tasks[root].node_id == 'main'
    assert 'Independent target' in c.state.tasks[root].input['instruction']
    assert not c.state.tasks[root].input.get('resume_data')
    tasks = set(c.state.tasks)
    c.service.operate(c.sid, body)
    c.service.tick()
    assert set(c.state.tasks) == tasks


def test_completed_replacement_resets_exhausted_budget_and_retires_only_goal_evidence(case):
    c = case
    root, review_id = completed(c)
    old = c.service.documents[c.sid]
    old.update(rounds=8, stagnation=3, criteria='old criteria',
               reviewer_provider='old-provider', reviewer_model='old-model')
    c.service._save(copy.deepcopy(old))
    before = copy.deepcopy(c.service.documents[c.sid])
    conversation = c.store.load(c.sid)
    # Todo is deliberately opaque to this feature; bytes must survive intact.
    todo = c.root / 'data/ide/todos/test-owned-by-todo.json'
    todo.parent.mkdir(parents=True, exist_ok=True)
    todo.write_bytes(b'{"untouched":true}')
    forget = c.state.execution_runtime.forget
    saved_before_forget = []

    def inspect(ids):
        durable = json.loads(c.service._path(c.sid).read_text())
        saved_before_forget.append(durable)
        assert durable['goal'] == 'Independent target'
        assert durable['status'] == 'blocked' and durable['_create_cleanup']
        assert not durable['_roots'] and not durable['reviews']
        forget(ids)

    with patch.object(c.state.execution_runtime, 'forget', side_effect=inspect):
        created = c.service.operate(c.sid, request(c))
    assert saved_before_forget
    assert created['status'] == 'running'
    assert created['revision'] > before['revision']
    assert created['goal_version'] == before['goal_version'] + 1
    assert created['rounds'] == created['stagnation'] == 0
    assert created['criteria'] == created['reviewer_provider'] == created['reviewer_model'] == ''
    assert created['reviews'] == []
    assert c.service.documents[c.sid]['_identity'] != before['_identity']
    assert not c.service.documents[c.sid]['_prior_runs']
    assert not c.service.documents[c.sid]['_review_outputs']
    assert root in c.state.execution_runtime._closed
    assert review_id in c.state.execution_runtime._closed
    assert root not in c.state.execution_runtime._evidence
    assert c.store.load(c.sid) == conversation
    assert todo.read_bytes() == b'{"untouched":true}'
    c.service.tick()
    assert c.service.documents[c.sid]['_roots'][-1] != root


@pytest.mark.parametrize('status', ['running', 'waiting', 'reviewing', 'paused', 'blocked'])
def test_create_rejects_unfinished_target_without_mutation(case, status):
    c = case
    control(c, 'set', goal='Keep current goal')
    c.service.documents[c.sid]['status'] = status
    before = copy.deepcopy(c.service.documents[c.sid])
    with pytest.raises(HTTPException) as error:
        c.service.operate(c.sid, request(c))
    assert error.value.status_code == 409
    assert c.service.documents[c.sid] == before
    assert not c.state.tasks


def test_create_duplicate_and_old_requests_survive_replacement(case):
    c = case
    old = request(c, 'old-create', 'First goal')
    first = c.service.operate(c.sid, old)
    assert c.service.operate(c.sid, old) == first
    with pytest.raises(HTTPException) as error:
        c.service.operate(c.sid, {**old, 'goal': 'Different payload'})
    assert error.value.status_code == 409
    # Completed state is enough for this request-cache test; no fake evidence verdict.
    c.service.documents[c.sid]['status'] = 'completed'
    c.service._save(copy.deepcopy(c.service.documents[c.sid]))
    new = request(c, 'new-create', 'Second goal')
    second = c.service.operate(c.sid, new)
    assert c.service.operate(c.sid, old) == second
    assert c.service.operate(c.sid, new) == second
    assert c.service.documents[c.sid]['goal'] == 'Second goal'
    assert set(c.service.documents[c.sid]['_requests']) == {'old-create', 'new-create'}
    c.service.tick()
    assert len(c.service.documents[c.sid]['_roots']) == 1


def test_create_cas_allows_only_one_of_two_confirmations(case):
    c = case
    body = request(c)
    barrier = Barrier(2)

    def create(index):
        barrier.wait()
        try:
            return c.service.operate(c.sid, {**body, 'request_id': f'confirmation-{index}'})
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(create, [1, 2]))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert results.count(409) == 1
    c.service.tick()
    assert len(c.service.documents[c.sid]['_roots']) == 1


def test_create_failed_initial_save_never_retires_old_goal_or_starts_new_task(case):
    c = case
    root, _ = completed(c)
    old_bytes = c.service._path(c.sid).read_bytes()
    tasks = set(c.state.tasks)
    replace = os.replace

    def fail(source, target):
        if target == c.service._path(c.sid):
            raise OSError('initial Goal write failed')
        return replace(source, target)

    with patch('plugins.ide.goal.os.replace', side_effect=fail), \
            patch.object(c.state.execution_runtime, 'forget', wraps=c.state.execution_runtime.forget) as forget:
        with pytest.raises(OSError):
            c.service.operate(c.sid, request(c))
        forget.assert_not_called()
    assert c.service._path(c.sid).read_bytes() == old_bytes
    assert root not in c.state.execution_runtime._closed
    c.service.tick()
    assert set(c.state.tasks) == tasks
    assert c.service.read(c.sid)['status'] == 'blocked'


def test_cleanup_failure_returns_503_keeps_durable_intent_and_retry_does_not_recreate(case):
    c = case
    completed(c)
    body = request(c)
    tasks = set(c.state.tasks)
    with patch.object(c.state.execution_runtime, 'forget', side_effect=OSError('retirement failed')) as forget:
        with pytest.raises(HTTPException) as error:
            c.service.operate(c.sid, body)
        assert error.value.status_code == 503
        durable = json.loads(c.service._path(c.sid).read_text())
        assert durable['goal'] == 'Independent target'
        assert durable['status'] == 'blocked' and durable['_create_cleanup']
        identity = durable['_identity']
        with pytest.raises(HTTPException) as replay_error:
            c.service.operate(c.sid, body)
        assert replay_error.value.status_code == 503
        assert c.service.documents[c.sid]['_identity'] == identity
        assert forget.call_count == 1
        c.service.tick()
        assert set(c.state.tasks) == tasks
    # Explicit resume performs pending cleanup and authorizes the already-created Goal.
    resumed = control(c, 'resume')
    assert resumed['status'] == 'running'
    assert c.service.documents[c.sid]['_identity'] == identity
    assert '_create_cleanup' not in c.service.documents[c.sid]
    c.service.tick()
    assert len(set(c.state.tasks) - tasks) == 1


def test_reload_recovers_cleanup_but_does_not_reauthorize_start(case):
    c = case
    completed(c)
    body = request(c)
    with patch.object(c.state.execution_runtime, 'forget', side_effect=OSError('retry after reload')):
        with pytest.raises(HTTPException):
            c.service.operate(c.sid, body)
    old_identity = c.service.documents[c.sid]['_identity']
    c.service.close()
    replacement = GoalService(c.state.execution_runtime, c.root, background=False)
    c.service = replacement
    assert replacement.read(c.sid)['status'] == 'paused'
    assert '_create_cleanup' not in replacement.documents[c.sid]
    assert replacement.documents[c.sid]['_identity'] == old_identity
    tasks = set(c.state.tasks)
    replacement.tick()
    assert set(c.state.tasks) == tasks
    replay = replacement.operate(c.sid, body)
    assert replay['status'] == 'paused'
    control(c, 'resume')
    replacement.tick()
    assert len(set(c.state.tasks) - tasks) == 1


def test_cleanup_acknowledgement_save_failure_keeps_pending_marker(case):
    c = case
    root, _ = completed(c)
    replace = os.replace
    calls = 0

    def fail_second_goal_write(source, target):
        nonlocal calls
        if target == c.service._path(c.sid):
            calls += 1
            if calls == 2:
                raise OSError('cleanup acknowledgement write failed')
        return replace(source, target)

    with patch('plugins.ide.goal.os.replace', side_effect=fail_second_goal_write):
        with pytest.raises(HTTPException) as error:
            c.service.operate(c.sid, request(c))
    assert error.value.status_code == 503
    durable = json.loads(c.service._path(c.sid).read_text())
    assert durable['status'] == 'blocked'
    assert root in durable['_create_cleanup']
    assert root in c.state.execution_runtime._closed
    control(c, 'resume')
    assert '_create_cleanup' not in c.service.documents[c.sid]


def test_late_old_candidate_and_output_cannot_contaminate_created_goal(case):
    c = case
    root, review_id = completed(c)
    c.service.operate(c.sid, request(c))
    c.service.tick()
    new_root = c.service.documents[c.sid]['_roots'][-1]
    before = copy.deepcopy(c.service.documents[c.sid])
    c.service.on_event({'kind': 'candidate', 'session_id': c.sid,
                        'candidate': {'entry_task_id': root, 'session_id': c.sid, 'candidate_id': 'late-old'}})
    assert c.service.documents[c.sid] == before
    delivery = c.state.execution_runtime.queue_result(delivery_id='old-late-result', work_id='old-work',
        origin_task_id=root, session_id=c.sid, text='Do not inject old result')
    assert delivery['strategy'] == 'closed'
    assert new_root not in c.state.execution_runtime._closed
    assert c.service.documents[c.sid]['reviews'] == []
    assert c.service.documents[c.sid]['_review'] is None
    assert review_id not in c.service.documents[c.sid]['_system_tasks']


@pytest.mark.parametrize('inflight', [False, True])
def test_retired_live_review_is_cancelled_and_late_history_never_reaches_new_goal(case, inflight):
    from engine.conversation_store import Message
    c = case
    completed(c)
    # A residual system task from a superseded review can coexist with completion.
    old = c.state.execution_runtime.create_system_task(c.sid, 'ide.goal_reviewer', 'Old review', 'orphan-review')
    tid, review_sid = old['task_id'], old['session_id']
    if inflight:
        claim(c, tid)
    data = c.service.documents[c.sid]
    data['_system_tasks'].append(tid)
    c.service._save(copy.deepcopy(data))
    parent_history = c.store.load(c.sid)
    c.service.operate(c.sid, request(c))
    assert c.state.tasks[tid].cancel_requested
    assert tid in c.state.execution_runtime._closed
    if not inflight:
        assert c.state.tasks[tid].status == 'cancelled'
    c.service.tick()
    new_root = c.service.documents[c.sid]['_roots'][-1]
    claim(c, new_root)
    before = copy.deepcopy(c.service.documents[c.sid])
    if inflight:
        # Worker history can arrive after forget; terminal and duplicate callbacks
        # must both delete it, without touching the owning chat/Todo or new Goal.
        for index in (1, 2):
            c.store.append_batch(review_sid, [Message(id=f'late-review-{index}', role='assistant', content='old verdict')])
            finish(c, tid, verdict(True), evidence=False)
            c.store.invalidate_cache(review_sid)
            assert not c.store.load(review_sid)
            assert c.service.documents[c.sid] == before
        assert c.state.execution_runtime._tasks[tid]['result'] == {}
    delivery = c.state.execution_runtime.queue_result(delivery_id='closed-review-delivery', work_id='old-review-work',
        origin_task_id=tid, session_id=c.sid, text='Old review cannot resume work')
    assert delivery['strategy'] == 'closed'
    assert c.service.documents[c.sid] == before
    c.service.tick()
    current = c.service.documents[c.sid]
    assert current['status'] == 'waiting'
    assert current['rounds'] == current['stagnation'] == 0
    assert current['_roots'] == [new_root] and current['reviews'] == []
    assert c.store.load(c.sid) == parent_history


def test_unconfirmed_old_reviewer_cancellation_blocks_create_before_forget(case):
    c = case
    completed(c)
    old = c.state.execution_runtime.create_system_task(c.sid, 'ide.goal_reviewer', 'Old review', 'orphan-review')
    tid = old['task_id']
    c.service.documents[c.sid]['_system_tasks'].append(tid)
    c.service._save(copy.deepcopy(c.service.documents[c.sid]))
    body = request(c)
    with patch.object(c.state.execution_runtime, 'cancel_system_task', return_value={'ok': False, 'error': 'not confirmed'}), \
            patch.object(c.state.execution_runtime, 'forget', wraps=c.state.execution_runtime.forget) as forget:
        with pytest.raises(HTTPException) as error:
            c.service.operate(c.sid, body)
        assert error.value.status_code == 503
        forget.assert_not_called()
        assert c.service.read(c.sid)['status'] == 'blocked'
        assert tid in c.service.documents[c.sid]['_create_cleanup']
    control(c, 'resume')
    assert c.state.tasks[tid].cancel_requested
    assert tid in c.state.execution_runtime._closed


def test_cleanup_pending_blocks_other_mutations_and_reload_failure_remains_blocked(case):
    c = case
    completed(c)
    with patch.object(c.state.execution_runtime, 'forget', side_effect=OSError('still unavailable')):
        with pytest.raises(HTTPException):
            c.service.operate(c.sid, request(c))
        for action in ('set', 'submit', 'start', 'create'):
            with pytest.raises(HTTPException) as error:
                control(c, action, **({'goal': 'must not replace'} if action in ('set', 'submit', 'create') else {}))
            assert error.value.status_code == 503
        c.service.close()
        c.service = GoalService(c.state.execution_runtime, c.root, background=False)
        assert c.service.read(c.sid)['status'] == 'blocked'
        assert c.service.documents[c.sid]['_create_cleanup']
    tasks = set(c.state.tasks)
    c.service.tick()  # Repair alone does not silently restart or retry a failed cleanup.
    assert set(c.state.tasks) == tasks
    assert c.service.read(c.sid)['status'] == 'blocked'
    control(c, 'clear')
    assert c.service.read(c.sid)['status'] == 'empty'
    assert '_create_cleanup' not in c.service.documents[c.sid]


def test_authenticated_create_api_reports_capability_and_cleanup_failure(case):
    from plugins.ide.goal import _SERVICES, add_routes
    from supervisor import admin_api
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore
    c = case
    completed(c)

    async def run():
        with patch.object(admin_api, '_admin_token', 'create-api-test'), \
                patch.object(admin_api, '_web_auth', None), \
                patch.dict(_SERVICES, {id(c.state.execution_runtime): c.service}):
            app = create_app(state=c.state, process_manager=None,
                             config_store=ConfigStore(path=c.root / 'data/config.yaml'))
            router = APIRouter()
            add_routes(router)
            app.include_router(router, prefix='/v1/plugins/ide')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                url = '/v1/plugins/ide/goal?session_id=' + c.sid
                body = request(c)
                assert (await client.post(url, json=body)).status_code == 401
                client.headers['Authorization'] = 'Bearer create-api-test'
                assert (await client.get(url)).json()['capabilities']['create'] is True
                with patch.object(c.state.execution_runtime, 'forget', side_effect=OSError('disk unavailable')):
                    failed = await client.post(url, json=body)
                    assert failed.status_code == 503
                    assert '新 Goal 已保存' in failed.json()['detail']
                    assert (await client.post(url, json=body)).status_code == 503
                blocked = (await client.get(url)).json()
                assert blocked['status'] == 'blocked' and blocked['goal'] == body['goal']
                assert blocked['revision'] > body['expected_revision']
                resumed = await client.post(url, json={
                    'action': 'resume', 'expected_session_id': c.sid,
                    'expected_revision': blocked['revision'], 'request_id': 'explicit-recovery'})
                assert resumed.status_code == 200 and resumed.json()['status'] == 'running'
                assert (await client.post(url, json=body)).json()['goal_version'] == blocked['goal_version']
    asyncio.run(run())


def test_create_validates_input_before_replacing_completed_goal(case):
    c = case
    completed(c)
    before = copy.deepcopy(c.service.documents[c.sid])
    for extra in [{'goal': ''}, {'goal': 'x' * 16001}, {'criteria': 5}, {'reviewer_model': []}]:
        with pytest.raises(HTTPException) as error:
            c.service.operate(c.sid, {**request(c), **extra})
        assert error.value.status_code == 422
        assert c.service.documents[c.sid] == before
