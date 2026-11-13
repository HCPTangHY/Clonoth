"""Read-only Goal identity/current-round metadata, against real runtime storage.

Run with TMPDIR/--basetemp under data/temp/goal-discord-status/backend.
"""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.ide.goal import GoalService
from test_goal_integration import case, claim, control, finish, reviewer, verdict


def create(c, request_id='create-progress'):
    body = {'action': 'create', 'goal': 'Run the checker', 'expected_session_id': c.sid,
            'expected_revision': c.service.read(c.sid)['revision'], 'request_id': request_id}
    return body, c.service.operate(c.sid, body)


def tick_at(c, timestamp):
    # Patch only the Goal module's clock; runtime task timestamps remain real.
    with patch('plugins.ide.goal.time', SimpleNamespace(time=lambda: timestamp)):
        c.service.tick()
    return c.service.read(c.sid)


def test_identity_and_capabilities_are_public_without_read_side_effects(case):
    c = case
    empty = c.service.read(c.sid)
    assert empty['goal_id'] == c.service.documents[c.sid]['_identity']
    assert empty['execution'] is None
    assert empty['capabilities'] == {'create': True, 'progress': True}
    assert not c.service._path(c.sid).exists()
    body, created = create(c)
    assert created['goal_id'] != empty['goal_id']
    assert created['execution'] is None
    assert created['revision'] == empty['revision'] + 1
    assert c.service.operate(c.sid, body) == created
    durable = c.service._path(c.sid).read_bytes()
    events = len(c.state.eventlog.events)
    assert c.service.read(c.sid) == created
    assert c.service._path(c.sid).read_bytes() == durable
    assert len(c.state.eventlog.events) == events
    stored = json.loads(durable)
    assert stored['_current_execution'] is None
    assert not {'goal_id', 'execution', 'capabilities'}.intersection(stored)
    assert not any(key.startswith('_') for key in created)


def test_two_rounds_expose_distinct_execution_and_keep_final_execution(case):
    c = case
    body, created = create(c)
    first = tick_at(c, 1000.0)
    root = first['execution']['task_id']
    assert first['execution'] == {'task_id': root,
        'source_inbound_seq': c.state.tasks[root].source_inbound_seq, 'started_at': 1000.0}
    assert first['goal_id'] == created['goal_id']
    claim(c, root)
    finish(c, root)
    review_id = reviewer(c)
    assert c.service.read(c.sid)['execution'] == first['execution']
    finish(c, review_id, verdict(False), evidence=False)
    second = tick_at(c, 2000.0)
    next_root = second['execution']['task_id']
    assert next_root != root
    assert second['goal_id'] == first['goal_id']
    assert second['rounds'] == 1
    assert second['execution']['started_at'] == 2000.0
    assert second['execution']['source_inbound_seq'] == c.state.tasks[next_root].source_inbound_seq
    assert second['execution']['source_inbound_seq'] != first['execution']['source_inbound_seq']
    assert c.service.operate(c.sid, body)['execution'] == second['execution']
    claim(c, next_root)
    finish(c, next_root)
    review_id = reviewer(c)
    finish(c, review_id, verdict(True), evidence=False)
    c.service.tick()
    completed = c.service.read(c.sid)
    assert completed['status'] == 'completed'
    assert completed['execution'] == second['execution']
    assert completed['goal_id'] == created['goal_id']
    _, replacement = create(c, 'next-progress')
    assert replacement['goal_id'] != completed['goal_id']
    assert replacement['goal_version'] > completed['goal_version']
    assert replacement['execution'] is None


@pytest.mark.parametrize('action', ['set', 'submit'])
def test_target_edit_preserves_identity_and_clears_previous_execution(case, action):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    root = started['execution']['task_id']
    claim(c, root)
    edited = control(c, action, goal='Different work')
    assert edited['goal_id'] == started['goal_id']
    assert edited['execution'] is None
    assert c.service.documents[c.sid]['_current_execution'] is None
    assert edited['goal_version'] == started['goal_version'] + 1
    if action == 'set':
        assert edited['status'] == 'paused'
        control(c, 'start')
    next_round = tick_at(c, 2000.0)
    assert next_round['execution']['task_id'] != root
    assert next_round['execution']['started_at'] == 2000.0
    assert next_round['goal_id'] == started['goal_id']


def test_pause_preserves_execution_but_normal_input_hides_previous_round(case):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    paused = control(c, 'pause')
    assert paused['execution'] == started['execution']
    assert paused['goal_id'] == started['goal_id']
    c.service.on_event({'kind': 'input', 'session_id': c.sid})
    after_input = c.service.read(c.sid)
    assert after_input['execution'] is None
    assert after_input['goal_id'] == started['goal_id']
    assert c.service.documents[c.sid]['_current_execution'] == started['execution']


def test_clear_replaces_identity_and_removes_execution(case):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    cleared = control(c, 'clear')
    assert cleared['goal_id'] != started['goal_id']
    assert cleared['execution'] is None
    assert cleared['status'] == 'empty'
    assert c.service.documents[c.sid]['_current_execution'] is None
    _, created = create(c, 'after-clear')
    assert created['goal_id'] not in {started['goal_id'], cleared['goal_id']}


def test_execution_public_copy_does_not_leak_private_fields_or_stale_root(case):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    public = c.service.read(c.sid)
    assert set(public['execution']) == {'task_id', 'source_inbound_seq', 'started_at'}
    assert not any(key.startswith('_') for key in public)
    public['execution']['task_id'] = 'not-real'
    assert c.service.read(c.sid)['execution'] == started['execution']
    c.service.documents[c.sid]['_roots'].append('different-current-root')
    before = copy.deepcopy(c.service.documents[c.sid])
    assert c.service.read(c.sid)['execution'] is None
    assert c.service.documents[c.sid] == before


def test_duplicate_runtime_result_does_not_restart_current_round_clock(case):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    execution = started['execution']
    data = copy.deepcopy(c.service.documents[c.sid])
    data['_pending_input'] = {'request_id': 'already-accepted', 'text': 'existing input', 'round': 0}
    data['_needs_input'] = True
    # Isolate a replayed acknowledgement using an actually created task identity.
    result = {'task_id': execution['task_id'], 'session_id': c.sid,
              'inbound_seq': execution['source_inbound_seq']}
    with patch.object(c.state.execution_runtime, 'submit_input', return_value=result), \
            patch('plugins.ide.goal.time', SimpleNamespace(time=lambda: 9999.0)):
        c.service._submit(data)
    assert c.service.read(c.sid)['execution'] == execution
    assert len(c.service.documents[c.sid]['_roots']) == 1


def test_reload_preserves_identity_clock_and_supports_legacy_missing_metadata(case):
    c = case
    create(c)
    started = tick_at(c, 1000.0)
    control(c, 'pause')
    c.service.close()
    c.service = GoalService(c.state.execution_runtime, c.root, background=False)
    assert c.service.read(c.sid)['goal_id'] == started['goal_id']
    assert c.service.read(c.sid)['execution'] == started['execution']
    legacy = copy.deepcopy(c.service.documents[c.sid])
    legacy.pop('_current_execution')
    c.service._save(legacy)
    c.service.close()
    c.service = GoalService(c.state.execution_runtime, c.root, background=False)
    public = c.service.read(c.sid)
    assert public['goal_id'] == started['goal_id']
    assert public['execution'] is None
