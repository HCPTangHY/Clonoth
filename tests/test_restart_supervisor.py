"""Planned restart handoff against real Supervisor task/session storage; no real restart."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from supervisor.eventlog import EventLog
from supervisor.state import SupervisorState
from supervisor.types import TaskKind, TaskStatus
from engine.conversation_store import ConversationStore, Message


def make_state(root, boot):
    nodes = root / 'config/nodes'
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / 'main.yaml').write_text('id: main\ntype: ai\n')
    with patch('supervisor.state.auto_discover_and_register', return_value={}), patch('engine.hooks.loader.load_external_plugins', return_value=0):
        state = SupervisorState(workspace_root=root, eventlog=EventLog(root / 'data/events.jsonl', run_id=boot), policy=SimpleNamespace())
    state._default_entry_node = lambda: 'main'
    state._maybe_trigger_turn_summary_locked = lambda task: None
    return state


def task(st, sid, **kwargs):
    return st._create_task_locked(session_id=sid, session_generation=1, kind=kwargs.pop('kind', TaskKind.node),
                                 node_id='main', input_data=kwargs.pop('input_data', {'instruction': 'Keep working'}),
                                 continuation={}, **kwargs)


def complete(st, row, action='finish', **extra):
    return st.complete_task(task_id=row.task_id, worker_id='worker', execution_id=row.execution_id,
                           result={'action': action, 'result': {'text': 'done'},
                                   **({'error': 'engine graceful shutdown'} if action == 'cancelled' else {}), **extra})


def test_restart_restores_same_ids_pending_and_running_without_new_inbound(tmp_path):
    st = make_state(tmp_path, 'before')
    sid = st.get_or_create_session(channel='web', conversation_key='web:a')
    first = task(st, sid, source_inbound_seq=77)
    st.assign_next_task(worker_id='worker')
    old_execution = first.execution_id
    queued = task(st, sid)
    plan = st.restart_recovery.begin(task_id=first.task_id, tool_call_id='restart-call')
    assert not first.cancel_requested
    assert st.assign_next_task(worker_id='worker') is None
    complete(st, first, 'cancelled', _runtime_state={'tool_call_log': [{'name': 'check'}]}, error='engine graceful shutdown')
    assert first.status == TaskStatus.suspended
    assert first.cancel_reason == 'service_restart'
    st.restart_recovery.finalize()
    restored = make_state(tmp_path, 'after')
    one = restored.tasks[first.task_id]
    assert one.status == TaskStatus.pending and one.source_inbound_seq == 77
    assert one.input['resume_data']['type'] == 'restart_interrupted'
    assert one.input['resume_data']['restart_tool_call_id'] == 'restart-call'
    assert one.result['_runtime_state']['tool_call_log'] == [{'name': 'check'}]
    assert 'resume_data' not in restored.tasks[queued.task_id].input
    assert restored.execution_runtime.planned_restart['restart_id'] == plan['restart_id']
    with pytest.raises(ValueError, match='stale'):
        restored.complete_task(task_id=one.task_id, worker_id='worker', execution_id=old_execution, result={'action': 'finish'})
    assert restored.assign_next_task(worker_id='new')['task_id'] == first.task_id
    assert not [e for e in restored.eventlog.events if e['type']=='inbound_message']
    third = make_state(tmp_path, 'third')
    assert not third.tasks  # A consumed plan is not crash recovery authorization.


def test_original_inbound_reply_index_and_dedup_survive_restart(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a')
    evt=st.eventlog.append(session_id=sid,component='test',type_='inbound_message',payload={'text':'original','use_branch':False})
    st.record_inbound_message_event(evt)
    original=next(iter(st.tasks.values()))
    st.assign_next_task(worker_id='worker')
    st._inbound_processed.add(evt['seq'])
    st._advance_inbound_cursor()
    st.restart_recovery.begin();complete(st,original,'cancelled');st.restart_recovery.finalize()
    restored=make_state(tmp_path,'after')
    assert restored._inbound_events[evt['seq']]['session_id']==sid
    assert restored._inbound_order==st._inbound_order
    assert restored._inbound_processed==st._inbound_processed
    assert restored._inbound_cursor==st._inbound_cursor
    restored.assign_next_task(worker_id='worker');complete(restored,restored.tasks[original.task_id])
    outputs=[e for e in restored.eventlog.events if e['type']=='outbound_message']
    assert len(outputs)==1 and outputs[0]['payload']['source_inbound_seq']==evt['seq']
    another=task(restored,sid)
    restored.restart_recovery.begin();restored.restart_recovery.finalize()
    again=make_state(tmp_path,'again')
    assert again._inbound_routed[evt['seq']]['action']=='reply'
    assert again.append_outbound_message(session_id=sid,text='duplicate',source_inbound_seq=evt['seq'])['deduped']
    assert another.task_id in again.tasks


def test_user_cancel_before_and_during_restart_never_resumes(tmp_path):
    st = make_state(tmp_path, 'before')
    sid = st.get_or_create_session(channel='web', conversation_key='web:a')
    before = task(st, sid); during = task(st, sid); keep = task(st, sid)
    st.cancel_single_task(before.task_id)
    st.restart_recovery.begin()
    st.cancel_single_task(during.task_id)
    st.restart_recovery.finalize()
    restored = make_state(tmp_path, 'after')
    assert restored.tasks[before.task_id].status == TaskStatus.cancelled
    assert restored.tasks[during.task_id].status == TaskStatus.cancelled
    assert restored.tasks[during.task_id].cancel_reason == 'user_cancel'
    assert restored.assign_next_task(worker_id='worker')['task_id'] == keep.task_id


def test_waiting_batch_and_terminal_sibling_are_preserved(tmp_path):
    st = make_state(tmp_path, 'before')
    sid = st.get_or_create_session(channel='web', conversation_key='web:a')
    parent = task(st, sid)
    parent.status=TaskStatus.suspended
    one=task(st,sid,caller_task_id=parent.task_id,batch_id='batch',batch_index=0)
    two=task(st,sid,caller_task_id=parent.task_id,batch_id='batch',batch_index=1)
    st.assign_next_task(worker_id='worker');complete(st,one)
    st.assign_next_task(worker_id='worker')
    st.restart_recovery.begin();complete(st,two,'cancelled');st.restart_recovery.finalize()
    restored=make_state(tmp_path,'after')
    assert restored.tasks[parent.task_id].status==TaskStatus.suspended
    assert restored.tasks[one.task_id].status==TaskStatus.completed
    assert restored.assign_next_task(worker_id='worker')['task_id']==two.task_id
    complete(restored,restored.tasks[two.task_id])
    assert restored.tasks[parent.task_id].status==TaskStatus.pending
    assert len(restored.tasks[parent.task_id].input['resume_data']['entries'])==2


def test_branch_and_child_history_survive_reconcile(tmp_path):
    st=make_state(tmp_path,'before')
    sid=st.get_or_create_session(channel='discord',conversation_key='dc:a')
    evt=st.eventlog.append(session_id=sid,component='test',type_='inbound_message',payload={'text':'work','use_branch':True})
    st.record_inbound_message_event(evt)
    current=next(iter(st.tasks.values()))
    branch=current.session_id
    assert branch.startswith('branch_')
    store=ConversationStore(tmp_path/'data/conversations')
    store.append(branch,Message(id='saved',role='user',content='history',source_task_id=current.task_id))
    st.assign_next_task(worker_id='worker')
    st.restart_recovery.begin();complete(st,current,'cancelled');st.restart_recovery.finalize()
    restored=make_state(tmp_path,'after')
    assert branch in restored.sessions
    assert restored.entry_branch_parents[branch]==sid
    assert store.load(branch)[0].content=='history'


def test_completed_race_not_reexecuted_and_checkpoint_error_stays_suspended(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a')
    done=task(st,sid);bad=task(st,sid)
    st.assign_next_task(worker_id='worker');st.assign_next_task(worker_id='worker')
    st.restart_recovery.begin();complete(st,done)
    complete(st,bad,'cancelled',checkpoint_error='history write failed')
    st.restart_recovery.finalize();restored=make_state(tmp_path,'after')
    assert restored.tasks[done.task_id].status==TaskStatus.completed
    assert restored.tasks[bad.task_id].status==TaskStatus.suspended
    assert restored.tasks[bad.task_id].cancel_reason=='restart_checkpoint_failed'
    assert restored.assign_next_task(worker_id='worker') is None


def test_standalone_tool_is_returned_cancelled_without_reexecution(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a')
    current=task(st,sid,kind=TaskKind.tool,tool_name='write_file')
    st.assign_next_task(worker_id='worker')
    st.restart_recovery.begin();complete(st,current,'cancelled');st.restart_recovery.finalize()
    restored=make_state(tmp_path,'after')
    assert restored.tasks[current.task_id].input['_restart_cancelled_tool'] is True


def test_unsealed_or_corrupt_plan_does_not_silently_resume(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a');task(st,sid)
    st.restart_recovery.begin()
    with pytest.raises(ValueError,match='sealed'):make_state(tmp_path,'after')
    st.restart_recovery.path.write_text('{bad')
    with pytest.raises(ValueError):make_state(tmp_path,'again')


def test_restore_save_failure_keeps_sealed_plan(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a');task(st,sid)
    st.restart_recovery.begin();st.restart_recovery.finalize()
    with patch('supervisor.execution_runtime.ExecutionRuntime._save',side_effect=OSError('disk full')):
        with pytest.raises(OSError):make_state(tmp_path,'failed')
    assert json.loads(st.restart_recovery.path.read_text())['phase']=='ready'
    assert make_state(tmp_path,'retry').tasks


def test_register_preserves_adapter_triggers_for_all_workers(tmp_path):
    st=make_state(tmp_path,'before');sid=st.get_or_create_session(channel='web',conversation_key='web:a');task(st,sid)
    st.restart_recovery.begin();st.restart_recovery.finalize();restored=make_state(tmp_path,'after')
    restored.register_engine('worker1','new1');restored.register_engine('worker2','new2')
    events=[e for e in restored.eventlog.events if e['type']=='engine_registered']
    assert len(events)==2
    assert all(e['payload']['preserve_tasks'] for e in events)
    assert len({e['payload']['restart_id'] for e in events})==1
    restored.register_engine('worker1','crashed-next-generation')
    assert 'preserve_tasks' not in restored.eventlog.events[-1]['payload']
