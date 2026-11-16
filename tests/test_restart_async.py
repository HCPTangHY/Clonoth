"""Restart cancellation uses ordinary receipt ownership, admission, and processing."""
import pytest

from engine.conversation_store import ConversationStore, Message
from test_restart_supervisor import make_state, task, complete
from supervisor.types import TaskStatus


@pytest.mark.parametrize('owner_status', ['running', 'completed'])
@pytest.mark.parametrize('paused', [False, True])
def test_async_restart_result_is_retained_once_and_settled_through_receipt(tmp_path, owner_status, paused):
    state = make_state(tmp_path, 'before')
    sid = state.get_or_create_session(channel='web', conversation_key='web:async')
    owner = task(state, sid)
    state.assign_next_task(worker_id='worker')
    state.execution_runtime.watch([owner.task_id])
    state._async_tools['async'] = {
        'async_id': 'async', 'task_id': owner.task_id, 'session_id': sid,
        'execution_id': owner.execution_id, 'boot_id': 'before', 'target_generation': 1,
        'tool_name': 'remote_exec', 'status': 'running', 'worker_id': 'worker',
    }
    if owner_status == 'completed':
        complete(state, owner)
    if paused:
        state.execution_runtime.set_paused([owner.task_id], True)
        # An already held obligation must not be bypassed by restart.
        state.execution_runtime.queue_result(delivery_id='existing', work_id='async',
            origin_task_id=owner.task_id, session_id=sid, text='Cancellation already returned')
    state.restart_recovery.begin()
    if owner_status == 'running':
        complete(state, owner, 'cancelled')
    state.restart_recovery.finalize()
    restored = make_state(tmp_path, 'after')
    rt = restored.execution_runtime
    assert len(rt._receipts) == 1
    receipt = next(iter(rt._receipts.values()))
    if paused:
        assert receipt['status'] == 'held'
        assert not restored.tasks[owner.task_id].preempt_message
        rt.set_paused([owner.task_id], False)
    assert receipt['status'] == 'queued'
    carrier = restored.tasks[receipt['carrier_task_id']]
    if owner_status == 'running':
        assert carrier.task_id == owner.task_id
        assert len(restored.tasks) == 1
        assert not [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
    else:
        assert carrier.task_id != owner.task_id
        assert len(restored.tasks) == 2
        inbound = [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
        assert len(inbound) == 1
        assert inbound[0]['payload']['message_type'] == 'async_tool_result'
    assert 'restart_tool_results' not in restored.tasks[owner.task_id].input
    # Use real durable admission before completing the receiving task.
    restored.assign_next_task(worker_id='worker')
    mid = 'cancel-result'
    did = receipt['delivery_id']
    store = ConversationStore(tmp_path/'data/conversations')
    store.append(carrier.input.get('child_session_id') or carrier.session_id,
                 Message(id=mid, role='user', content=receipt['text'], source_task_id=carrier.task_id,
                         meta={'delivery_ids': [did]}))
    rt.admit(carrier.task_id, [did], mid, carrier.execution_id)
    if carrier.preempt_requested:
        restored.consume_preempt_message(carrier.task_id, consumed_message=carrier.preempt_message,
                                      consumed_attachment_count=len(carrier.preempt_attachments),
                                      delivery_ids=[did], message_id=mid, execution_id=carrier.execution_id)
    complete(restored, carrier)
    assert receipt['status'] == 'processed'
    assert restored._async_tools['async']['restart_result_consumed'] is True
    facts = rt.snapshot([owner.task_id])
    assert not facts['busy'] and not facts['unresolved'], facts


def test_user_cancel_during_preparation_does_not_create_callback_task(tmp_path):
    state = make_state(tmp_path, 'before')
    sid = state.get_or_create_session(channel='web', conversation_key='web:cancel')
    owner = task(state, sid)
    state.assign_next_task(worker_id='worker')
    state._async_tools['async'] = {'async_id': 'async', 'task_id': owner.task_id,
        'session_id': sid, 'target_generation': 1, 'tool_name': 'slow', 'status': 'running'}
    state.restart_recovery.begin()
    state.cancel_single_task(owner.task_id)
    complete(state, owner, 'cancelled')
    state.restart_recovery.finalize()
    restored = make_state(tmp_path, 'after')
    assert len(restored.tasks) == 1
    assert restored.tasks[owner.task_id].status == TaskStatus.cancelled
    assert not restored.execution_runtime._receipts
    assert restored.assign_next_task(worker_id='worker') is None


def test_unconfirmed_stop_keeps_checkpoint_blocked_and_reconcile_failure_keeps_plan(tmp_path):
    import json
    from unittest.mock import patch
    state = make_state(tmp_path, 'before')
    sid = state.get_or_create_session(channel='web', conversation_key='web:checkpoint')
    owner = task(state, sid)
    state.assign_next_task(worker_id='worker')
    state.restart_recovery.begin()
    state.restart_recovery.finalize()
    with patch('supervisor.state.SupervisorState._reconcile_after_restart', side_effect=OSError('cleanup failed')):
        with pytest.raises(OSError):
            make_state(tmp_path, 'failed')
    assert json.loads(state.restart_recovery.path.read_text())['phase'] == 'ready'
    restored = make_state(tmp_path, 'after')
    assert restored.tasks[owner.task_id].status == TaskStatus.suspended
    assert restored.tasks[owner.task_id].cancel_reason == 'restart_checkpoint_failed'
    assert restored.assign_next_task(worker_id='worker') is None


def test_evicted_processed_receipts_never_trigger_false_lost_callbacks_after_restart(tmp_path):
    state = make_state(tmp_path, 'before')
    sid = state.get_or_create_session(channel='discord_guild', conversation_key='discord:room-1')
    rt = state.execution_runtime

    # Produce 205 settled async tool deliveries so >5 processed receipts get evicted by _save().
    for idx in range(205):
        aid = f'done-{idx:03d}'
        carrier = task(state, sid)
        state.assign_next_task(worker_id='worker')
        state._async_tools[aid] = {
            'async_id': aid, 'task_id': carrier.task_id, 'session_id': sid,
            'execution_id': carrier.execution_id, 'boot_id': 'before', 'target_generation': 1,
            'tool_name': 'remote_exec', 'status': 'done', 'worker_id': 'worker',
            'finished_at': f'2026-11-12T12:00:{idx % 60:02d}+00:00',
        }
        did = f'{aid}:result'
        rt._receipts[did] = {
            'delivery_id': did, 'work_id': aid, 'origin_task_id': carrier.task_id,
            'session_id': sid, 'target_generation': 1, 'status': 'admitted',
            'carrier_task_id': carrier.task_id, 'message_id': f'msg-{idx}',
        }
        complete(state, carrier)

    rt._save()
    assert len(rt._receipts) == 200
    assert state._async_tools['done-000'].get('receipt_settled') is True
    assert 'done-000:result' not in rt._receipts

    # Run a planned restart and re-register worker with a new generation.
    live_task = task(state, sid)
    state.assign_next_task(worker_id='worker')
    state.restart_recovery.begin()
    complete(state, live_task, 'cancelled')
    state.restart_recovery.finalize()

    restored = make_state(tmp_path, 'after')
    inbound_before = [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
    restored.register_engine('worker', 'new-gen-1')
    inbound_after = [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
    assert inbound_after == inbound_before
    assert restored._async_tools['done-000']['status'] == 'done'


@pytest.mark.parametrize('restart_kind', ['planned', 'unplanned'])
def test_admitted_and_unwatched_async_deliveries_never_trigger_false_lost_callbacks(tmp_path, restart_kind):
    import json
    state = make_state(tmp_path, 'before')
    sid = state.get_or_create_session(channel='discord_guild', conversation_key='discord:room-2')
    rt = state.execution_runtime

    # Case A: unwatched carrier completes with finish -> persisted immediately to runtime.json as processed.
    c1 = task(state, sid)
    state.assign_next_task(worker_id='worker')
    ack1 = state.register_async_tool({
        'async_id': 'unwatched-finish', 'task_id': c1.task_id, 'execution_id': c1.execution_id,
        'worker_id': 'worker', 'session_id': sid, 'tool_name': 'execute_command',
    })
    state.finish_async_tool('unwatched-finish', {**ack1, 'execution_id': c1.execution_id, 'status': 'done'})
    rt.queue_result(
        delivery_id='unwatched-finish:result', work_id='unwatched-finish',
        origin_task_id=c1.task_id, session_id=sid, text='done 1',
    )
    store = ConversationStore(tmp_path / 'data/conversations')
    store.append(c1.session_id, Message(id='m-1', role='user', content='done 1',
                                        source_task_id=c1.task_id, meta={'delivery_ids': ['unwatched-finish:result']}))
    rt.admit(c1.task_id, ['unwatched-finish:result'], 'm-1', c1.execution_id)
    c1.preempt_message = ''
    c1.preempt_requested = False
    c1.preempt_delivery_ids.clear()
    complete(state, c1)
    persisted = json.loads(rt._path.read_text(encoding='utf-8'))
    assert persisted['receipts']['unwatched-finish:result']['status'] == 'processed'
    assert persisted['work']['unwatched-finish']['receipt_settled'] is True
    assert persisted['work']['unwatched-finish']['result_delivered'] is True

    # Case B: legacy/in-flight admitted receipt (like historical production runtime.json rows without settled flags).
    c2 = task(state, sid)
    state.assign_next_task(worker_id='worker')
    state._async_tools['legacy-admitted'] = {
        'async_id': 'legacy-admitted', 'task_id': c2.task_id, 'session_id': sid,
        'execution_id': c2.execution_id, 'boot_id': 'before', 'target_generation': 1,
        'tool_name': 'remote_exec', 'status': 'done', 'worker_id': 'worker',
    }
    rt._receipts['legacy-admitted:result'] = {
        'delivery_id': 'legacy-admitted:result', 'work_id': 'legacy-admitted',
        'origin_task_id': c2.task_id, 'session_id': sid, 'target_generation': 1,
        'status': 'admitted', 'carrier_task_id': c2.task_id, 'message_id': 'm-2',
    }
    rt._save()

    if restart_kind == 'planned':
        state.restart_recovery.begin()
        complete(state, c2, 'cancelled')
        state.restart_recovery.finalize()

    restored = make_state(tmp_path, 'after')
    inbound_before = [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
    restored.register_engine('worker', 'new-gen-2')
    inbound_after = [e for e in restored.eventlog.events if e['type'] == 'inbound_message']
    assert inbound_after == inbound_before
    assert restored._async_tools['unwatched-finish']['status'] == 'done'
    assert restored._async_tools['legacy-admitted']['status'] == 'done'
