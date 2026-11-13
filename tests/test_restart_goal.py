"""Goal inherits only an explicit planned-restart authorization; no new input."""
import copy
from unittest.mock import patch

import pytest

from plugins.ide.goal import GoalService
from test_goal_plugin import Runtime, answer, candidate, command, env as base_env, launch


@pytest.fixture
def env(base_env):
    yield base_env
    # Reboot replaces the fixture service; close that replacement as well.
    base_env.service.close()


def reboot(e, task_ids, *, restart_id='planned', planned=True):
    # Process death does not invoke the plugin-unload operation.
    e.service.enabled = False
    e.service.stopped.set()
    e.service.unsubscribe()
    runtime = Runtime(e.root)
    runtime.tasks = copy.deepcopy(e.rt.tasks)
    runtime.facts = copy.deepcopy(e.rt.facts)
    runtime.inputs = copy.deepcopy(e.rt.inputs)
    runtime.reviews = copy.deepcopy(e.rt.reviews)
    runtime.planned_restart = {'planned_restart': planned, 'restart_id': restart_id,
                               'resumed_task_ids': list(task_ids), 'preserved_task_ids': list(task_ids)}
    e.rt = runtime
    e.service = GoalService(runtime, e.root, background=False)
    return e.service.read('main')


def test_planned_restart_keeps_goal_and_budget_without_new_first_input(env):
    tid = launch(env)
    before = env.service.read('main')
    env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    env.service.tick()
    assert len(env.rt.inputs) == 1
    result = reboot(env, [tid])
    assert result['status'] == before['status']
    assert result['goal_version'] == before['goal_version']
    assert result['rounds'] == before['rounds']
    assert tid not in env.rt.paused
    env.service.tick()
    assert len(env.rt.inputs) == 1


def test_goal_user_pause_after_restart_intent_is_not_reauthorized(env):
    tid = launch(env)
    env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    command(env, 'pause')
    result = reboot(env, [tid])
    assert result['status'] == 'paused'
    assert tid in env.rt.paused
    env.service.tick()
    assert len(env.rt.inputs) == 1


def test_unplanned_or_mismatched_restart_does_not_inherit_permission(env):
    tid = launch(env)
    env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    result = reboot(env, [tid], restart_id='other')
    assert result['status'] == 'paused'
    assert tid in env.rt.paused


def test_missing_restored_root_requires_explicit_recovery(env):
    launch(env)
    env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    assert reboot(env, [])['status'] == 'paused'


def test_inflight_review_keeps_task_and_excludes_restart_downtime(env):
    tid = launch(env)
    review = candidate(env)
    old_started = env.service.documents['main']['_review']['started']
    with patch('plugins.ide.goal.time.time', return_value=1000):
        env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    with patch('plugins.ide.goal.time.time', return_value=1120):
        result = reboot(env, [tid, review])
    assert result['status'] == 'reviewing'
    assert env.service.documents['main']['_review']['started'] == old_started + 120
    assert len(env.rt.reviews) == 1
    answer(env, review, passed=True)
    assert env.service.read('main')['status'] == 'completed'
    assert len(env.rt.inputs) == 1


def test_aborted_restart_releases_tick_without_replacing_goal(env):
    tid = launch(env)
    env.service.on_event({'kind': 'restart_preparing', 'restart_id': 'planned'})
    env.service.on_event({'kind': 'restart_aborted', 'restart_id': 'planned'})
    assert not env.service._restart_preparing
    assert '_restart_checkpoint' not in env.service.documents['main']
    env.service.tick()
    assert tid == env.rt.inputs[0][0]
    assert len(env.rt.inputs) == 1
