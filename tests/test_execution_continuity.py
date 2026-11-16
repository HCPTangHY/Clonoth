"""Execution-only activity, main-chain targeting, and durable preempt semantics."""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from supervisor.execution_runtime import ExecutionRuntime
from supervisor.types import TaskKind, TaskStatus
from test_execution_runtime import runtime, claimed, finish


def event(rt, state, tid, execution, kind, **payload):
    return rt.record_activity({'type': kind, 'session_id': state._route_session_id_for_task_locked(state.tasks[tid]),
        'payload': {'task_id': tid, 'execution_id': execution, **payload}})


def test_activity_excludes_lease_usage_and_stale_claim(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    with patch('supervisor.execution_runtime.time.time', return_value=100):
        assert event(rt, state, tid, execution, 'tool_call_start', tool_call_id='one', tool_name='read_file')
    snapshot = rt.execution_activity([tid])
    assert snapshot['task_id'] == tid and snapshot['execution_id'] == execution
    assert snapshot['session_id'] == sid and snapshot['conversation_key'] == 'web:runtime'
    assert snapshot['busy_tool'] and snapshot['last_tool_activity_at'] == 100
    assert not event(rt, state, tid, 'old', 'tool_call_end', tool_call_id='one')
    assert rt.execution_activity([tid])['busy_tool']
    assert state.renew_lease(tid, 'worker', execution_id=execution)
    assert not event(rt, state, tid, execution, 'context_usage')
    assert rt.execution_activity([tid])['last_activity_at'] == 100
    with patch('supervisor.execution_runtime.time.time', return_value=110):
        assert event(rt, state, tid, execution, 'tool_call_end', tool_call_id='one')
    assert not rt.execution_activity([tid])['busy_tool']
    assert rt.execution_activity([tid])['last_activity_at'] == 110


def test_parallel_tools_finish_independently(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    for call in ['a', 'b']:
        event(rt, state, tid, execution, 'tool_call_start', tool_call_id=call)
    event(rt, state, tid, execution, 'tool_call_end', tool_call_id='a')
    assert rt.execution_activity([tid])['busy_tool']
    event(rt, state, tid, execution, 'tool_call_end', tool_call_id='b')
    assert not rt.execution_activity([tid])['busy_tool']


def test_main_target_excludes_child_review_and_unrelated_entries(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    child = state._create_task_locked(session_id=state.tasks[tid].session_id, session_generation=1, kind=TaskKind.node,
        node_id='worker', input_data={}, continuation={}, caller_task_id=tid)
    review = state._create_task_locked(session_id=sid, session_generation=1, kind=TaskKind.node,
        node_id='review', input_data={'_system_task': True}, continuation={})
    unrelated = rt.submit_input(sid, 'other', 'other')['task_id']
    value = rt.execution_activity([tid])
    assert value['task_id'] == tid and value['busy_child']
    assert child.task_id in value['live_task_ids']
    assert review.task_id not in value['live_task_ids'] and unrelated not in value['live_task_ids']
    assert not rt.execution_activity([child.task_id])['task_id']
    assert rt.execution_activity([tid, unrelated])['ambiguous']
    assert not rt.execution_activity([tid, unrelated])['task_id']


def test_output_chain_leaf_is_target_and_user_input_changes_snapshot(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    root = state.tasks[tid]
    leaf = state._create_task_locked(session_id=root.session_id, session_generation=1, kind=TaskKind.node, node_id='qa',
        input_data={'_output_chain': True, 'parent_session_id': sid}, continuation={},
        caller_task_id=tid, output_chain_entry_task_id=tid)
    root.status = TaskStatus.suspended
    root.waiting_for_task_id = leaf.task_id
    rt.task_changed(root)
    value = rt.execution_activity([tid])
    assert value['task_id'] == leaf.task_id
    before = rt.snapshot([tid])['version']
    events = []
    rt.subscribe(events.append)
    assert rt.preempt_execution(sid, leaf.task_id, '', 'new direction', 'edit', roots=[tid], source='user')
    assert leaf.preempt_message == 'new direction'
    assert rt.snapshot([tid])['version'] != before
    assert len([e for e in events if e['kind'] == 'input_routed']) == 1
    assert rt.preempt_execution(sid, leaf.task_id, '', 'new direction', 'edit', roots=[tid], source='user')
    assert leaf.preempt_message == 'new direction'
    assert len([e for e in events if e['kind'] == 'input_routed']) == 1


def test_preempt_dedupes_across_reload_and_rejects_changed_identity(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    assert not rt.preempt_execution(sid, tid, 'wrong', 'check', 'a', roots=[tid])
    assert rt.preempt_execution(sid, tid, execution, 'check', 'a', roots=[tid])
    assert state.tasks[tid].preempt_message == 'check'
    recreated = ExecutionRuntime(state)
    assert recreated.preempt_execution(sid, tid, execution, 'check', 'a', roots=[tid])
    assert state.tasks[tid].preempt_message == 'check'
    with pytest.raises(ValueError, match='conflict'):
        rt.preempt_execution(sid, tid, execution, 'different', 'a', roots=[tid])
    rt.set_paused([tid], True)
    assert not rt.preempt_execution(sid, tid, execution, 'check', 'b', roots=[tid])


def test_approval_and_async_are_waits_not_activity(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    state.approvals['test'] = type('Approval', (), {'status': 'pending', 'task_id': tid})()
    state._async_tools['async'] = {'task_id': tid, 'status': 'running'}
    value = rt.execution_activity([tid])
    assert value['awaiting_approval'] and value['busy_async'] and value['waiting']
    assert {'approval', 'async_work'}.issubset(value['wait_reasons'])


def test_subscriber_save_failure_cannot_validate_old_evidence(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    old = rt.snapshot([tid])['version']
    def failing(event):
        if event['kind'] == 'input_routed':
            raise OSError('Goal disk failure')
    rt.subscribe(failing)
    assert rt.preempt_execution(sid, tid, execution, 'change goal', 'edit', roots=[tid], source='user')
    assert rt.snapshot([tid])['version'] != old
    saved = json.loads(rt._path.read_text())
    assert saved['tasks'][tid]['input_revision'] == 1
    assert rt.preempt_execution(sid, tid, execution, 'change goal', 'edit', roots=[tid], source='user')
    assert rt._tasks[tid]['input_revision'] == 1


def test_durable_intent_failure_does_not_preempt(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    with patch.object(rt, '_save', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            rt.preempt_execution(sid, tid, execution, 'check', 'fail', roots=[tid])
    assert not state.tasks[tid].preempt_requested
