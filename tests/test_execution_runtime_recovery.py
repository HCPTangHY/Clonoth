"""Fault injection for staged control replies and bounded execution evidence."""
import json
from unittest.mock import patch

import pytest

from engine.conversation_store import ConversationStore, Message
from supervisor.execution_runtime import ExecutionRuntime
from test_execution_runtime import runtime, claimed, finish


def store(rt):
    return ConversationStore(rt.workspace_root / 'data' / 'conversations')


def fail_phase(rt, phase):
    save = rt._save
    def failing():
        if any(row.get('phase') == phase for row in rt._requests.values()):
            raise OSError('injected ' + phase)
        save()
    return patch.object(rt, '_save', side_effect=failing)


@pytest.mark.parametrize('phase', ['prepared', 'history', 'outbound', 'done'])
def test_control_failed_save_retry_does_not_repeat_business_or_history(runtime, phase):
    rt, state, sid = runtime
    calls = []
    rt.register_input_handler(lambda *args: calls.append(args) or {'text': 'paused'})
    with fail_phase(rt, phase), pytest.raises(OSError):
        rt.handle_input(sid, '/goal pause', 'command')
    result = rt.handle_input(sid, '/goal pause', 'command')
    assert len(calls) == 1
    assert [m.content for m in store(rt).load(sid)] == ['/goal pause', 'paused']
    assert len([e for e in state.eventlog.events if e['type'] == 'inbound_message']) == 1
    assert len([e for e in state.eventlog.events if e['type'] == 'outbound_message']) == 1
    assert rt.handle_input(sid, '/goal pause', 'command') == result
    with pytest.raises(ValueError, match='conflict'):
        rt.handle_input(sid, '/goal clear', 'command')
    with pytest.raises(ValueError, match='conflict'):
        rt.submit_input(sid, '/goal pause', 'command')


def test_partial_history_retry_only_adds_missing_ids(runtime):
    rt, state, sid = runtime
    calls = []
    rt.register_input_handler(lambda *args: calls.append(args) or {'text': 'ok'})
    append = ConversationStore.append_batch
    def partial(self, session_id, messages):
        append(self, session_id, messages[:1])
        raise OSError('one row committed')
    with patch.object(ConversationStore, 'append_batch', partial), pytest.raises(OSError):
        rt.handle_input(sid, '/goal status', 'c')
    assert state._inbound_order[0] in state._inbound_processed
    rt.handle_input(sid, '/goal status', 'c')
    assert len(calls) == 1
    assert len(store(rt).load(sid)) == 2


def test_outbound_commit_then_error_reuses_seq(runtime):
    rt, state, sid = runtime
    rt.register_input_handler(lambda *args: {'text': 'ok'})
    append = state.append_outbound_message
    def partial(**kwargs):
        append(**kwargs)
        raise OSError('reply committed')
    with patch.object(state, 'append_outbound_message', side_effect=partial), pytest.raises(OSError):
        rt.handle_input(sid, '/goal status', 'c')
    rt.handle_input(sid, '/goal status', 'c')
    assert len([e for e in state.eventlog.events if e['type'] == 'outbound_message']) == 1


def test_unconfirmed_command_business_is_never_replayed_even_after_restart(runtime):
    rt, state, sid = runtime
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise OSError('business may have committed')
    rt.register_input_handler(uncertain)
    with pytest.raises(OSError):
        rt.handle_input(sid, '/goal start', 'c')
    with pytest.raises(ValueError, match='unconfirmed'):
        rt.handle_input(sid, '/goal start', 'c')
    restored = ExecutionRuntime(state)
    restored.register_input_handler(uncertain)
    with pytest.raises(ValueError, match='unconfirmed'):
        restored.handle_input(sid, '/goal start', 'c')
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['input', 'system'])
def test_task_ack_save_retry_and_identity_conflict(runtime, kind):
    rt, state, sid = runtime
    def call(text='work', **options):
        if kind == 'input':
            return rt.submit_input(sid, text, 'r')
        return rt.create_system_task(sid, 'main', text, 'r', node_overrides=options)
    with patch('engine.faces.nodes.declared_node_owner', return_value='ide'):
        with fail_phase(rt, 'done'), pytest.raises(OSError):
            call()
        count = len(state.tasks)
        result = call()
        assert len(state.tasks) == count == 1
        assert json.loads(rt._path.read_text())['requests'][sid + ':r']['result'] == result
        with pytest.raises(ValueError, match='conflict'):
            call('changed')
        if kind == 'system':
            with pytest.raises(ValueError, match='conflict'):
                call(model='different')


def test_evidence_result_flags_change_version_heartbeat_does_not(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    before = rt.snapshot([tid])['version']
    rt._evidence[tid] = [{'content': 'new fact', 'source_task_id': tid}]
    evidence = rt.snapshot([tid])['version']
    assert evidence != before
    state.tasks[tid].result = {'action': 'finish', 'result': {'text': 'candidate'}}
    rt.task_changed(state.tasks[tid])
    candidate = rt.snapshot([tid])['version']
    assert candidate != evidence
    state.renew_lease(tid, 'worker', execution_id=execution)
    assert rt.snapshot([tid])['version'] == candidate
    rt._tasks[tid]['identity_unverified'] = True
    snap = rt.snapshot([tid])
    assert snap['version'] != candidate
    assert any(r['status'] == 'unknown' for r in snap['unresolved'])
    rt.task_changed(state.tasks[tid])
    assert rt._tasks[tid]['identity_unverified']


def test_legacy_work_and_receipt_are_unknown(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    state._async_tools['legacy'] = {'async_id': 'legacy', 'task_id': tid, 'status': 'done', 'session_id': sid}
    rt.queue_result(delivery_id='d', work_id='legacy', origin_task_id=tid, session_id=sid,
                    text='done', metadata={'identity_unverified': True})
    snap = rt.snapshot([tid])
    assert sum(r['status'] == 'unknown' for r in snap['unresolved']) == 2


def test_closed_system_history_cleans_late_terminal_and_boot_only_own_history(runtime):
    rt, state, sid = runtime
    with patch('engine.faces.nodes.declared_node_owner', return_value='ide'):
        result = rt.create_system_task(sid, 'main', 'review', 'review')
    tid, child = result['task_id'], result['session_id']
    task = state.tasks[tid]
    rt.forget([tid])
    store(rt).append(sid, Message(id='main', role='user', content='keep'))
    store(rt).append(child, Message(id='late', role='assistant', content='late'))
    task.status = 'cancelled'
    rt.task_changed(task)
    assert store(rt).load(child) == []
    store(rt).append(child, Message(id='after', role='assistant', content='even later'))
    rt.cleanup_closed_system(task)
    assert store(rt).load(child) == []
    store(rt).append(child, Message(id='boot', role='assistant', content='orphan'))
    restored = ExecutionRuntime(state)
    restored.mark_ready()
    assert store(rt).load(child) == []
    assert [m.content for m in store(rt).load(sid)] == ['keep']


def test_evidence_owner_budget_isolated_and_total_limited(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    other_sid = state.get_or_create_session(channel='web', conversation_key='other')
    other = rt.submit_input(other_sid, 'other', 'other')['task_id']
    # One exhausted session must not consume another session's quota.
    rt._evidence[tid] = [{'content': 'x' * 2_000_000}]
    store(rt).append(state.tasks[other].session_id, Message(id='m', role='tool', content='证据', source_task_id=other))
    rt.capture_evidence(state.tasks[other])
    assert rt._evidence[other][0]['content'] == '证据'
    with patch('supervisor.execution_runtime.TOTAL_EVIDENCE_LIMIT', 2_000_000):
        rt.capture_evidence(state.tasks[other])
    assert rt._evidence[other] == [] and other in rt._truncated


def test_restart_done_without_receipt_is_lost(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    state._async_tools['done'] = {'async_id': 'done', 'task_id': tid, 'execution_id': execution, 'status': 'done'}
    rt._save()
    ExecutionRuntime(state)
    assert state._async_tools['done']['status'] == 'lost'


def test_runtime_failure_allows_plain_input_but_not_command_or_automatic_work(runtime):
    rt, state, sid = runtime
    def handler(sid, text, rid):
        if text.startswith('/goal'):
            rt.snapshot([])
        return None
    rt.register_input_handler(handler)
    rt._load_error = True
    assert rt.handle_input(sid, 'ordinary chat', 'chat') is None
    with pytest.raises(ValueError, match='unavailable'):
        rt.handle_input(sid, '/goal status', 'c')
    with pytest.raises(ValueError, match='unavailable'):
        rt.submit_input(sid, 'auto', 'a')


def test_plain_input_matching_never_writes_execution_storage(runtime):
    rt, state, sid = runtime
    calls = []
    rt.register_input_handler(lambda *args: calls.append(args) or {'text': 'ok'},
                              matches=lambda text: text.startswith('/goal '))
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        assert rt.handle_input(sid, 'normal chat', 'ordinary') is None
    assert calls == []
    assert rt._requests == {}


def test_failed_unpause_revokes_authorization_but_failed_pause_keeps_it_revoked(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    rt.set_paused([tid], True)
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            rt.set_paused([tid], False)
    assert rt._is_paused(tid)
    assert rt.queue_result(delivery_id='held', work_id='held', origin_task_id=tid,
                           session_id=sid, text='done')['strategy'] == 'held'
    rt.set_paused([tid], False)
    with patch.object(rt, '_save', side_effect=OSError('disk')):
        with pytest.raises(OSError):
            rt.set_paused([tid], True)
    assert rt._is_paused(tid)
