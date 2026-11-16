"""Long-running Goal policy, persistence and editable active execution checks."""
import copy
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from plugins.ide.goal import GoalService
from test_goal_plugin import env, command, launch, candidate, answer


def activity(task, **values):
    return {'task_id': task, 'execution_id': 'test', 'last_activity_at': 1000,
            'busy_tool': False, 'busy_child': False, 'busy_async': False,
            'waiting': False, 'awaiting_approval': False, 'has_active_task': True, **values}


def tick_at(e, now):
    with patch('plugins.ide.goal.time.time', return_value=now):
        e.service.tick()


def test_check_at_30_minutes_only_and_one_reminder_each_interval(env):
    with patch('plugins.ide.goal.time.time', return_value=1000):
        task = launch(env)
    env.rt.execution_activity = lambda roots: activity(task)
    tick_at(env, 2799)
    assert not any(':check:' in key for key in env.rt.requests)
    tick_at(env, 2800)
    reminders = [key for key in env.rt.requests if ':check:' in key]
    assert len(reminders) == 1
    tick_at(env, 2801)
    assert len([key for key in env.rt.requests if ':check:' in key]) == 1
    tick_at(env, 4600)
    assert len([key for key in env.rt.requests if ':check:' in key]) == 2
    assert len(env.rt.inputs) == 1
    assert env.service.read('main')['status'] in {'running', 'waiting'}
    assert env.service.documents['main']['_wait_started'] == 4600


@pytest.mark.parametrize('extra', [
    {'last_activity_at': 2400}, {'last_activity_at': None}, {'busy_tool': True},
    {'busy_child': True}, {'busy_async': True}, {'awaiting_approval': True},
    {'waiting': True}, {'task_id': ''}, {'execution_id': ''},
])
def test_normal_work_and_unknown_activity_never_wake_or_pause(env, extra):
    with patch('plugins.ide.goal.time.time', return_value=1000):
        task = launch(env)
    env.rt.execution_activity = lambda roots: activity(task, **extra)
    tick_at(env, 2800)
    assert not any(':check:' in key for key in env.rt.requests)
    assert env.service.read('main')['status'] in {'running', 'waiting'}
    assert env.service.documents['main']['_wait_started'] == 2800


def test_reminder_durable_before_dispatch_and_failed_ack_never_duplicates(env):
    with patch('plugins.ide.goal.time.time', return_value=1000):
        task = launch(env)
    env.rt.execution_activity = lambda roots: activity(task)
    original = env.rt.preempt_execution
    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('reply lost')
    env.rt.preempt_execution = uncertain
    tick_at(env, 2800)
    saved = env.service.documents['main']['_pending_preempt'].copy()
    assert env.service.read('main')['status'] == 'blocked'
    assert saved['request_id'] in env.rt.requests
    env.rt.preempt_execution = original
    command(env, 'resume')
    tick_at(env, 2801)
    assert '_pending_preempt' not in env.service.documents['main']
    assert len([key for key in env.rt.requests if ':check:' in key]) == 1


@pytest.mark.parametrize('action', ['set', 'submit'])
def test_active_edit_keeps_task_clock_counters_and_preempts_once(env, action):
    task = launch(env)
    before = copy.deepcopy(env.service.documents['main'])
    env.service.documents['main'].update(rounds=4, stagnation=8)
    result = command(env, action, goal='Edited objective', max_rounds=512)
    env.service.tick()
    assert result['status'] == 'running'
    assert result['rounds'] == 4 and result['stagnation'] == 8
    assert result['max_rounds'] == 512
    assert env.service.documents['main']['_roots'] == before['_roots']
    assert result['execution'] == before['_current_execution']
    assert not env.rt.paused
    messages = [r for key, r in env.rt.requests.items() if ':edit:' in key]
    assert len(messages) == 1 and 'Edited objective' in messages[0]['text']
    assert len(env.rt.inputs) == 1


def test_paused_edit_and_user_input_never_resume(env):
    task = launch(env)
    command(env, 'pause')
    result = command(env, 'set', goal='Keep paused')
    assert result['status'] == 'paused'
    env.service.on_event({'kind': 'input_routed', 'session_id': 'main', 'task_id': task, 'user_input': True})
    env.service.tick()
    assert env.service.read('main')['status'] == 'paused'
    assert task in env.rt.paused


def test_pre_route_input_is_noop_and_accepted_preempt_keeps_current_clock(env):
    task = launch(env)
    before = env.service.read('main')
    env.service.on_event({'kind': 'input', 'session_id': 'main'})
    assert env.service.read('main') == before
    env.service.on_event({'kind': 'input_routed', 'session_id': 'main', 'task_id': task, 'user_input': True})
    after = env.service.read('main')
    assert after['goal_version'] == before['goal_version'] + 1
    assert after['execution'] == before['execution']
    assert after['status'] == before['status']
    assert len(env.rt.inputs) == 1


def test_round_defaults_override_and_existing_document_is_unchanged(env):
    assert env.service.read('main')['max_rounds'] == 256
    command(env, 'set', goal='Old goal', max_rounds=8)
    durable = env.service._path('main').read_bytes()
    with patch('plugins.ide.goal.load_runtime_config', return_value={'plugins': {'ide': {'goal': {'default_max_rounds': 400}}}}):
        assert env.service.read('main')['max_rounds'] == 8
        assert env.service.read('new-session')['max_rounds'] == 400
    assert env.service._path('main').read_bytes() == durable


@pytest.mark.parametrize('value', [0, -1, True, 1.2, '12', 1000001])
def test_invalid_round_override_rejected_without_write(env, value):
    before = env.service.read('main')
    with pytest.raises(HTTPException) as exc:
        command(env, 'create', goal='New', max_rounds=value)
    assert exc.value.status_code == 422
    assert env.service.read('main') == before
    assert not env.service._path('main').exists()
