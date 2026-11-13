"""Submit combines target replacement and explicit start without resetting budgets."""
import copy
import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from test_goal_integration import case, claim, control, finish, reviewer, start, verdict


def test_submit_replaces_running_goal_and_preserves_settings_and_old_root_pause(case):
    c = case
    control(c, 'submit', goal='First target', criteria='Keep criteria',
            reviewer_provider='openai', reviewer_model='review-model')
    c.service.tick()
    old_root = c.service.documents[c.sid]['_roots'][-1]
    claim(c, old_root)
    c.service.documents[c.sid].update(rounds=2, stagnation=1)
    before = c.service.read(c.sid)
    edited = control(c, 'submit', goal='New target\n参考附件：data/attachments/reference.png')
    assert edited['status'] == 'running'
    assert edited['goal_version'] == before['goal_version'] + 1
    for field in ('criteria', 'reviewer_provider', 'reviewer_model', 'rounds', 'stagnation'):
        assert edited[field] == before[field]
    assert c.state.execution_runtime._is_paused(old_root)
    c.service.tick()
    new_root = c.service.documents[c.sid]['_roots'][-1]
    assert new_root != old_root
    assert c.state.tasks[old_root].status == 'running'
    assert c.state.tasks[new_root].node_id == 'main'
    assert 'data/attachments/reference.png' in c.state.tasks[new_root].input['instruction']
    assert not c.state.tasks[new_root].input.get('resume_data')
    finish(c, old_root, 'old target finished')
    assert c.service.documents[c.sid]['_candidate'] is None
    callback = c.state.execution_runtime.queue_result(delivery_id='old-late', work_id='old-work',
        origin_task_id=old_root, session_id=c.sid, text='old result')
    assert callback['strategy'] == 'held'
    claim(c, new_root)
    finish(c, new_root)
    tid = reviewer(c)
    assert c.state.tasks[tid].input['_execution_options']['model'] == 'review-model'
    finish(c, tid, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'


@pytest.mark.parametrize('old_result', ['passed', 'malformed'])
def test_old_review_settles_without_applying_or_pausing_submitted_replacement(case, old_result):
    c = case
    old = start(c)
    claim(c, old)
    finish(c, old)
    tid = reviewer(c)
    version = c.service.read(c.sid)['goal_version']
    control(c, 'submit', goal='Replacement target')
    c.service.tick()
    new = c.service.documents[c.sid]['_roots'][-1]
    assert new != old
    finish(c, tid, verdict(True) if old_result == 'passed' else 'not json', evidence=False)
    c.service.tick()
    data = c.service.read(c.sid)
    assert data['status'] in {'running', 'waiting'}
    assert data['goal_version'] == version + 1
    assert data['reviews'][-1]['goal_version'] == version
    assert not data['reviews'][-1]['applied']
    assert c.service.documents[c.sid]['_review'] is None


@pytest.mark.parametrize('counters', [{'rounds': 8}, {'stagnation': 3}])
def test_submit_cannot_bypass_budget_and_rejection_changes_nothing(case, counters):
    c = case
    control(c, 'set', goal='Existing target')
    c.service.documents[c.sid].update(counters)
    before = copy.deepcopy(c.service.documents[c.sid])
    with pytest.raises(HTTPException) as error:
        control(c, 'submit', goal='Another target')
    assert error.value.status_code == 409
    assert c.service.documents[c.sid] == before
    assert not c.state.tasks


def test_failed_submit_save_does_not_start_a_task(case):
    c = case
    replace = os.replace
    def fail_goal_write(source, target):
        if target == c.service._path(c.sid):
            raise OSError('goal save failed')
        return replace(source, target)
    with patch('plugins.ide.goal.os.replace', side_effect=fail_goal_write):
        with pytest.raises(OSError):
            control(c, 'submit', goal='Not confirmed')
    c.service.tick()
    assert not c.state.tasks
    assert c.service.read(c.sid)['status'] == 'blocked'
