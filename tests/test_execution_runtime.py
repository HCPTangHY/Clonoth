"""Restricted runtime facts and receipt ordering; no network or model calls."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine.conversation_store import ConversationStore, Message
from supervisor.eventlog import EventLog
from supervisor.state import SupervisorState


@pytest.fixture
def runtime(tmp_path):
    p = tmp_path / 'config' / 'nodes'
    p.mkdir(parents=True)
    (p / 'main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\n')
    with patch('supervisor.state.auto_discover_and_register', return_value={}), patch('engine.hooks.loader.load_external_plugins', return_value=0):
        state = SupervisorState(workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / 'data' / 'events.jsonl', run_id='boot'), policy=SimpleNamespace())
    state._default_entry_node = lambda: 'main'
    state._maybe_trigger_turn_summary_locked = lambda task: None
    sid = state.get_or_create_session(channel='web', conversation_key='web:runtime')
    return state.execution_runtime, state, sid


def claimed(runtime):
    rt, state, sid = runtime
    tid = rt.submit_input(sid, 'work', 'first')['task_id']
    item = state.assign_next_task(worker_id='worker')
    assert item['task_id'] == tid
    return tid, item['execution_id']


def finish(state, tid, execution):
    return state.complete_task(task_id=tid, worker_id='worker', execution_id=execution,
        result={'action': 'finish', 'result': {'text': 'done'}})


def test_complete_and_heartbeat_identity(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    assert not state.renew_lease(tid, 'worker', execution_id='old')
    assert state.renew_lease(tid, 'worker', execution_id=execution)
    with pytest.raises(ValueError):
        finish(state, tid, 'old')
    with pytest.raises(ValueError, match='required'):
        finish(state, tid, '')
    finish(state, tid, execution)
    events = len(state.eventlog.events)
    finish(state, tid, execution)
    assert len(state.eventlog.events) == events


def test_snapshot_heartbeat_pause_and_unrelated_task_do_not_change_version(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    before = rt.snapshot([tid])['version']
    state.renew_lease(tid, 'worker', execution_id=execution)
    rt.set_paused([tid], True)
    rt.submit_input(sid, 'unrelated', 'unrelated')
    assert rt.snapshot([tid])['version'] == before
    with pytest.raises(ValueError, match='paused'):
        rt.submit_input(sid, 'continue', 'next', origin_task_id=tid)


def test_async_done_is_not_delivered_and_stale_start_does_not_reset(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    entry = {'async_id': 'one', 'task_id': tid, 'execution_id': execution, 'worker_id': 'worker',
             'session_id': sid, 'tool_name': 'tool'}
    ack = state.register_async_tool(entry)
    assert ack['ok']
    body = {**ack, 'execution_id': execution, 'status': 'done'}
    assert state.finish_async_tool('one', body)['ok']
    assert state.register_async_tool(entry)['ok']
    assert state._async_tools['one']['status'] == 'done'
    finish(state, tid, execution)
    assert rt.snapshot([tid])['busy']
    assert not state.finish_async_tool('one', {**body, 'status': 'invalid'})['ok']
    assert not state.finish_async_tool('one', {**body, 'boot_id': 'old'})['ok']


def test_receipt_requires_owned_persistent_input_and_normal_completion(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    result = rt.queue_result(delivery_id='d', work_id='w', origin_task_id=tid,
        session_id=sid, text='result')
    assert result['strategy'] == 'preempt'
    with pytest.raises(ValueError, match='durably'):
        rt.admit(tid, ['d'], 'missing', execution)
    store = ConversationStore(rt.workspace_root / 'data' / 'conversations')
    store.append(state.tasks[tid].session_id, Message(id='m', role='user', content='result',
        source_task_id=tid, meta={'delivery_ids': ['d']}))
    rt.admit(tid, ['d'], 'm', execution)
    assert rt.snapshot([tid])['busy']
    state.tasks[tid].preempt_message = ''
    state.tasks[tid].preempt_requested = False
    finish(state, tid, execution)
    assert not rt.snapshot([tid])['busy']
    assert rt.snapshot([tid])['receipts'][0]['status'] == 'processed'


def test_inline_delivery_still_waits_for_source_finish(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    ack = state.register_async_tool({'async_id': 'inline', 'task_id': tid, 'execution_id': execution,
        'worker_id': 'worker', 'session_id': sid, 'tool_name': 'tool'})
    assert state.finish_async_tool('inline', {**ack, 'execution_id': execution, 'status': 'done',
        'delivered_inline': True, 'carrier_task_id': tid})['ok']
    assert rt.snapshot([tid])['busy']
    finish(state, tid, execution)
    assert not rt.snapshot([tid])['busy']


def test_route_failure_never_acknowledges_or_repeats_partial_commit(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    with patch.object(state, '_route_completed_task_inner_locked', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            finish(state, tid, execution)
    assert rt.snapshot([tid])['busy']
    with pytest.raises(ValueError, match='unresolved'):
        finish(state, tid, execution)


def test_forget_blocks_late_result_and_input(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    finish(state, tid, execution)
    rt.forget([tid])
    count = len(state.tasks)
    assert rt.queue_result(delivery_id='late', work_id='late-work', origin_task_id=tid,
        session_id=sid, text='late')['strategy'] == 'closed'
    assert len(state.tasks) == count
    with pytest.raises(ValueError, match='closed'):
        rt.submit_input(sid, 'late continuation', 'late-input', origin_task_id=tid)


def test_receipt_save_failure_retry_recovers(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    finish(state, tid, execution)
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            rt.queue_result(delivery_id='retry', work_id='retry-work', origin_task_id=tid, session_id=sid, text='result')
    result = rt.queue_result(delivery_id='retry', work_id='retry-work', origin_task_id=tid, session_id=sid, text='result')
    assert result['strategy'] == 'inbound'
    with pytest.raises(ValueError, match='conflict'):
        rt.queue_result(delivery_id='retry', work_id='retry-work', origin_task_id=tid, session_id=sid, text='result', attachments=[{'path': 'different'}])


def test_direct_command_has_independent_persistent_history_and_outbound(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    original_seq = state.tasks[tid].source_inbound_seq
    rt.register_input_handler(lambda sid, text, request_id: {'text': 'paused'} if text == '/goal pause' else None)
    answer = rt.handle_input(sid, '/goal pause', 'command')
    assert answer['handled'] and answer['inbound_seq'] != original_seq
    rows = ConversationStore(rt.workspace_root / 'data' / 'conversations').load(sid)
    assert [row.content for row in rows][-2:] == ['/goal pause', 'paused']
    event = [e for e in state.eventlog.events if e['type'] == 'outbound_message'][-1]
    assert event['payload']['source_inbound_seq'] == answer['inbound_seq']
    assert 'task_id' not in event['payload']
    assert state.tasks[tid].status == 'running'
    assert rt.handle_input(sid, '/goal pause', 'command') == answer


def test_unwatched_task_completion_ignores_runtime_disk_failure(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    rt.unwatch([tid])
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        finish(state, tid, execution)
    assert state.tasks[tid].status == 'completed'
    assert state.tasks[tid].completed_execution_id == execution


def test_async_registration_failed_save_is_retried(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    entry = {'async_id': 'start', 'task_id': tid, 'execution_id': execution,
             'worker_id': 'worker', 'session_id': sid}
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            state.register_async_tool(entry)
    assert state.register_async_tool(entry)['ok']
    persisted = json.loads(rt._path.read_text())
    assert persisted['work']['start']['execution_id'] == execution


def test_done_can_cancel_before_delivery_and_lost_respects_pause(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    ack = state.register_async_tool({'async_id': 'cancel', 'task_id': tid, 'execution_id': execution,
        'worker_id': 'worker', 'session_id': sid})
    body = {**ack, 'execution_id': execution}
    assert state.finish_async_tool('cancel', {**body, 'status': 'done'})['ok']
    assert state.finish_async_tool('cancel', {**body, 'status': 'cancelled'})['ok']
    state.register_async_tool({'async_id': 'lost', 'task_id': tid, 'execution_id': execution,
        'worker_id': 'worker', 'session_id': sid})
    state.finish_async_tool('lost', {**body, 'status': 'done'})
    rt.set_paused([tid], True)
    count = len(state.tasks)
    state.register_engine('worker', 'new-generation')
    assert state._async_tools['lost']['status'] == 'lost'
    assert len(state.tasks) == count
    assert any(row['status'] == 'held' for row in rt._receipts.values())


def test_workspace_failure_preserves_old_binding(runtime):
    rt, state, sid = runtime
    state.set_session_workspace(sid, {'name': 'before'})
    with patch.object(state._session_store, '_flush', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            state.set_session_workspace(sid, {'name': 'after'})
    assert state.get_session_workspace(sid)['name'] == 'before'
    assert state._session_store._registry[sid]['workspace']['name'] == 'before'
