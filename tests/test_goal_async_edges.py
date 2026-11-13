"""The supervisor owns a held delivery; producer loss cannot invalidate it."""
import copy

from test_execution_runtime import runtime, claimed


def register(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    entry = {'async_id': 'edge', 'task_id': tid, 'execution_id': execution,
             'worker_id': 'worker', 'session_id': sid, 'tool_name': 'tool'}
    ack = state.register_async_tool(entry)
    assert ack['ok']
    return entry, {**ack, 'execution_id': execution}


def test_registration_rejects_missing_claim_and_changed_identity(runtime):
    rt, state, sid = runtime
    entry, body = register(runtime)
    assert not state.register_async_tool({**entry, 'async_id': 'other', 'execution_id': ''})['ok']
    for field in ('session_id', 'worker_id', 'node_id', 'tool_name', 'args_summary'):
        assert not state.register_async_tool({**entry, field: 'different'})['ok']
    assert state._async_tools['edge']['tool_name'] == 'tool'


def test_bad_inline_identity_does_not_mutate_lifecycle(runtime):
    rt, state, sid = runtime
    entry, body = register(runtime)
    before = copy.deepcopy(state._async_tools['edge'])
    assert not state.finish_async_tool('edge', {**body, 'status': 'done',
        'delivered_inline': True, 'carrier_task_id': 'other'})['ok']
    assert state._async_tools['edge'] == before


def test_legacy_callback_respects_pause_and_cannot_prove_completion(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    rt.set_paused([tid], True)
    count = len(state.tasks)
    result = state.inject_async_result(sid, 'old callback', source_task_id=tid)
    assert result['strategy'] == 'held'
    assert len(state.tasks) == count
    assert rt.snapshot([tid])['unresolved']
    assert not state.inject_async_result(sid, 'unattributed callback')['ok']
    assert rt._tasks[tid]['identity_unverified']


def test_held_delivery_survives_producer_restart_and_rejects_cancel_rewrite(runtime):
    rt, state, sid = runtime
    entry, body = register(runtime)
    state.finish_async_tool('edge', {**body, 'status': 'done'})
    rt.set_paused([entry['task_id']], True)
    result = rt.queue_result(delivery_id='held', work_id='edge', origin_task_id=entry['task_id'],
        session_id=sid, text='already delivered to supervisor')
    assert result['strategy'] == 'held'
    assert not state.finish_async_tool('edge', {**body, 'status': 'cancelled'})['ok']
    with state._lock:
        assert state._mark_worker_async_tools_lost_locked('worker') == []
    assert state._async_tools['edge']['status'] == 'done'
    assert rt._receipts['held']['status'] == 'held'
