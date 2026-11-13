"""Immutable review outputs are separate from raw evidence and retain ownership."""
import asyncio
import copy
import json
from unittest.mock import patch

import httpx
import pytest

from engine.conversation_store import ConversationStore, Message
from supervisor import admin_api
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.execution_runtime import ExecutionRuntime
from test_execution_runtime import runtime, claimed, finish


def reply(state, task_id, text='reply', *, call='call', event_type='intermediate_reply', attachments=None, session_id=None):
    task = state.tasks[task_id]
    return state.eventlog.append(session_id=session_id or state._route_session_id_for_task_locked(task),
        component='engine', type_=event_type, payload={'task_id': task_id, 'text': text,
        'llm_request_id': call, 'execution_id': task.execution_id, 'attachments': attachments or []})


def test_reply_finish_attachments_order_and_identity(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    before = rt.snapshot([tid])['version']
    attachment = {'path': 'proof.txt', 'type': 'file'}
    event = reply(state, tid, attachments=[attachment])
    rt.record_reply(event)
    rt.record_reply(event)
    rt.record_reply(reply(state, tid, call='another'))
    rt.record_reply(reply(state, tid, call='another', event_type='assistant_text'))
    state.complete_task(task_id=tid, worker_id='worker', execution_id=execution,
        result={'action': 'finish', 'result': {'text': 'done', 'attachments': [attachment]}})
    snapshot = rt.snapshot([tid])
    outputs = snapshot['outputs']
    assert [row['kind'] for row in outputs] == ['reply', 'reply', 'reply', 'finish']
    assert [row['sequence'] for row in outputs] == [1, 2, 3, 4]
    assert outputs[0]['attachments'] == outputs[-1]['attachments'] == [attachment]
    assert outputs[0]['text'] == outputs[1]['text'] == 'reply'  # Same text, distinct calls.
    assert snapshot['version'] != before
    assert snapshot['output_truncated_task_ids'] == []
    copy_before = copy.deepcopy(outputs)
    rt.capture_evidence(state.tasks[tid])
    assert rt.snapshot([tid])['outputs'] == copy_before


def test_finish_survives_raw_trace_budget_and_precise_flags(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    store = ConversationStore(rt.workspace_root/'data/conversations')
    store.append(state.tasks[tid].session_id, Message(id='too-long', role='tool', content='x'*300_000, source_task_id=tid))
    finish(state, tid, execution)
    snapshot = rt.snapshot([tid])
    assert snapshot['evidence'] == []
    assert snapshot['truncated_task_ids'] == [tid]
    assert snapshot['outputs'][-1]['text'] == 'done'
    assert snapshot['output_truncated_task_ids'] == []


def test_output_budget_never_slices_and_changes_snapshot_version(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    before = rt.snapshot([tid])['version']
    with patch('supervisor.execution_runtime.OWNER_EVIDENCE_LIMIT', 200):
        rt.record_reply(reply(state, tid, text='large'*100))
    snapshot = rt.snapshot([tid])
    assert snapshot['outputs'] == []
    assert snapshot['output_truncated_task_ids'] == [tid]
    assert snapshot['version'] != before
    assert snapshot['truncated_task_ids'] == []


def test_unwatched_system_cross_session_and_stale_execution_do_not_record(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    other_sid = state.get_or_create_session(channel='web', conversation_key='other')
    rt.record_reply(reply(state, tid, session_id=other_sid))
    event = reply(state, tid)
    event['payload']['execution_id'] = 'old'
    rt.record_reply(event)
    state.tasks[tid].input['_system_task'] = True
    rt.record_reply(reply(state, tid))
    state.tasks[tid].input['_system_task'] = False
    rt.unwatch([tid])
    rt.record_reply(reply(state, tid))
    assert rt.snapshot([tid])['outputs'] == []
    assert rt.snapshot([tid])['output_truncated_task_ids'] == [tid]


def test_restart_keeps_order_and_forget_clears_without_reusing_sequence(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    rt.record_reply(reply(state, tid))
    finish(state, tid, execution)
    expected = rt.snapshot([tid])['outputs']
    restored = ExecutionRuntime(state)
    assert restored.snapshot([tid])['outputs'] == expected
    state.execution_runtime = restored
    restored.forget([tid])
    assert restored.snapshot([tid])['outputs'] == []
    assert restored.snapshot([tid])['output_truncated_task_ids'] == []
    next_id = restored.submit_input(sid, 'new', 'new')['task_id']
    state.assign_next_task(worker_id='worker')
    restored.record_reply(reply(state, next_id))
    assert restored.snapshot([next_id])['outputs'][0]['sequence'] == 3


def test_legacy_runtime_missing_outputs_is_explicitly_incomplete(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    finish(state, tid, execution)
    data = json.loads(rt._path.read_text())
    del data['outputs']
    data.pop('output_sequence')
    rt._path.write_text(json.dumps(data))
    restored = ExecutionRuntime(state)
    assert restored.snapshot([tid])['output_truncated_task_ids'] == [tid]


def test_output_chain_keeps_main_draft_and_later_producer(runtime):
    rt, state, sid = runtime
    nodes = rt.workspace_root/'config/nodes'
    (nodes/'main.yaml').write_text('id: main\ntype: ai\noutput: qa\n')
    (nodes/'qa.yaml').write_text('id: qa\ntype: ai\noutput: __source__\n')
    tid, execution = claimed(runtime)
    finish(state, tid, execution)
    qa = state.tasks[tid].waiting_for_task_id
    assert qa
    item = state.assign_next_task(worker_id='worker')
    state.complete_task(task_id=qa, worker_id='worker', execution_id=item['execution_id'],
        result={'action': 'finish', 'result': {'text': 'verified final'}})
    outputs = rt.snapshot([tid])['outputs']
    assert [(r['task_id'], r['text']) for r in outputs] == [(tid, 'done'), (qa, 'verified final')]


def test_conflicting_output_identity_never_rewrites_existing_prefix(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    event = reply(state, tid, text='original')
    rt.record_reply(event)
    before = rt.snapshot([tid])
    event['payload']['text'] = 'changed'
    rt.record_reply(event)
    after = rt.snapshot([tid])
    assert after['outputs'] == before['outputs']
    assert after['output_truncated_task_ids'] == [tid]
    assert after['version'] != before['version']


def test_output_write_failure_disables_automatic_facts(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    with patch.object(rt, '_save', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            rt.record_reply(reply(state, tid))
    with pytest.raises(ValueError, match='unavailable'):
        rt.snapshot([tid])


def test_output_budget_is_per_owner_and_has_global_ceiling(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    rt.record_reply(reply(state, tid, text='a'*1500))
    other_sid = state.get_or_create_session(channel='web', conversation_key='isolated')
    other = rt.submit_input(other_sid, 'work', 'other')['task_id']
    state.assign_next_task(worker_id='worker')
    with patch('supervisor.execution_runtime.OWNER_EVIDENCE_LIMIT', 1000):
        rt.record_reply(reply(state, other, text='independent'))
    assert rt.snapshot([other])['outputs'][0]['text'] == 'independent'
    with patch('supervisor.execution_runtime.TOTAL_EVIDENCE_LIMIT', 1000):
        rt.record_reply(reply(state, other, call='second'))
    assert rt.snapshot([other])['output_truncated_task_ids'] == [other]


def test_event_api_records_reply_without_parsing_tool_history(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    async def run():
        with patch.object(admin_api, '_admin_token', 'review-test'), patch.object(admin_api, '_web_auth', None):
            app = create_app(state=state, process_manager=None,
                config_store=ConfigStore(path=rt.workspace_root/'data/config.yaml'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                         headers={'Authorization':'Bearer review-test'}) as http:
                event = {'type':'intermediate_reply','payload':{'task_id':tid,'execution_id':execution,'llm_request_id':'one','text':'exact reply'}}
                response = await http.post(f'/v1/sessions/{sid}/events', json=event)
                assert response.status_code == 200
                assert rt.snapshot([tid])['outputs'][0]['text'] == 'exact reply'
    asyncio.run(run())


def test_recapture_preserves_artifacts_after_cleanup_and_merges_new_files(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    directory = rt.workspace_root/'data/artifacts/tool_results'/tid
    directory.mkdir(parents=True)
    (directory/'old.txt').write_text('original full result')
    finish(state, tid, execution)  # Actual cleanup removes the file after capture.
    assert not (directory/'old.txt').exists()
    before = rt.snapshot([tid])['evidence']
    assert before[-1]['content'] == 'original full result'
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'new.txt').write_text('new full result')
    rt.capture_evidence(state.tasks[tid])
    after = rt.snapshot([tid])['evidence']
    assert [(row['artifact'], row['content']) for row in after] == [
        ('new.txt', 'new full result'), ('old.txt', 'original full result')]
    rt.capture_evidence(state.tasks[tid])
    assert rt.snapshot([tid])['evidence'] == after
    assert rt.snapshot([tid])['truncated_task_ids'] == []


def test_recapture_reserves_old_artifact_budget_and_marks_new_gap(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    old = {'source_task_id': tid, 'artifact': 'old.txt', 'content': '完整返回', 'truncated': False}
    rt._evidence[tid] = [old]
    store = ConversationStore(rt.workspace_root/'data/conversations')
    store.append(state.tasks[tid].session_id, Message(id='new', role='tool', content='x'*1000, source_task_id=tid))
    size = len(json.dumps(old, ensure_ascii=False).encode()) + 4
    with patch('supervisor.execution_runtime.EVIDENCE_LIMIT', size):
        rt.capture_evidence(state.tasks[tid])
    snap = rt.snapshot([tid])
    assert snap['evidence'] == [old]
    assert snap['truncated_task_ids'] == [tid]
    assert len(json.dumps(snap['evidence'], ensure_ascii=False).encode()) <= size


def test_missing_checkpoint_boundary_marks_trace_incomplete(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    state.tasks[tid].result = {'_runtime_state': {'first_shadow_message_id': 'lost-start'}}
    rt.capture_evidence(state.tasks[tid])
    assert rt.snapshot([tid])['truncated_task_ids'] == [tid]


def test_recapture_keeps_messages_removed_by_compaction(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    store = ConversationStore(rt.workspace_root/'data/conversations')
    branch = state.tasks[tid].session_id
    store.append(branch, Message(id='old', role='assistant', content='old step', source_task_id=tid))
    rt.capture_evidence(state.tasks[tid])
    store.replace_all(branch, [Message(id='new', role='assistant', content='new step', source_task_id=tid)])
    rt.capture_evidence(state.tasks[tid])
    assert [row['content'] for row in rt.snapshot([tid])['evidence']] == ['old step', 'new step']
    assert rt.snapshot([tid])['truncated_task_ids'] == []


def test_recapture_same_artifact_name_never_overwrites_prior_content(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    old = {'source_task_id': tid, 'artifact': 'one.txt', 'content': 'original', 'truncated': False}
    rt._evidence[tid] = [old]
    directory = rt.workspace_root/'data/artifacts/tool_results'/tid
    directory.mkdir(parents=True)
    (directory/'one.txt').write_text('changed')
    rt.capture_evidence(state.tasks[tid])
    snap = rt.snapshot([tid])
    assert snap['evidence'] == [old]
    assert snap['truncated_task_ids'] == [tid]


def test_terminal_same_execution_late_reply_appends_and_changes_version(runtime):
    rt, state, sid = runtime
    tid, execution = claimed(runtime)
    event = reply(state, tid, text='late accepted')
    finish(state, tid, execution)
    before = rt.snapshot([tid])
    rt.record_reply(event)
    after = rt.snapshot([tid])
    assert [row['kind'] for row in after['outputs']] == ['finish', 'reply']
    assert after['outputs'][-1]['text'] == 'late accepted'
    assert after['version'] != before['version']
    assert after['output_truncated_task_ids'] == []
    rt.record_reply(event)
    assert rt.snapshot([tid])['outputs'] == after['outputs']


@pytest.mark.parametrize('mode', ['missing_execution', 'old_execution', 'cancelled', 'pending', 'missing_task'])
def test_unconfirmable_reply_marks_output_incomplete_without_attribution(runtime, mode):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    event = reply(state, tid)
    before = rt.snapshot([tid])['version']
    if mode == 'missing_execution':
        event['payload'].pop('execution_id')
    elif mode == 'old_execution':
        state.tasks[tid].execution_id = 'new-execution'
    elif mode == 'missing_task':
        state.tasks.pop(tid)
    else:
        state.tasks[tid].status = mode
    rt.record_reply(event)
    snap = rt.snapshot([tid])
    assert snap['outputs'] == []
    assert snap['output_truncated_task_ids'] == [tid]
    assert snap['version'] != before
    saved = json.loads(rt._path.read_text())
    assert saved['output_truncated_task_ids'] == [tid]


def test_missing_execution_marker_write_failure_disables_runtime(runtime):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    event = reply(state, tid)
    event['payload'].pop('execution_id')
    with patch.object(rt, '_save', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            rt.record_reply(event)
    with pytest.raises(ValueError, match='unavailable'):
        rt.snapshot([tid])


@pytest.mark.parametrize('nested', [False, True])
def test_checkpoint_error_marks_both_trace_and_outputs_incomplete(runtime, nested):
    rt, state, sid = runtime
    tid, _ = claimed(runtime)
    error = {'checkpoint_error': 'reply event not acknowledged'}
    state.tasks[tid].result = {'_runtime_state': error} if nested else error
    rt.capture_evidence(state.tasks[tid])
    snap = rt.snapshot([tid])
    assert snap['truncated_task_ids'] == snap['output_truncated_task_ids'] == [tid]
    stored = json.loads(rt._path.read_text())
    assert stored['truncated'] == stored['output_truncated_task_ids'] == [tid]
