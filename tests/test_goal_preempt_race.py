"""Real supervisor output-chain completion/preempt races; no model or network."""
import json

import pytest

from engine.conversation_store import Message
from supervisor.types import TaskStatus
from test_goal_integration import case, claim, control, finish, start


def chain(c, output='__source__'):
    nodes = c.root / 'config/nodes'
    (nodes / 'main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\noutput: qa\n')
    (nodes / 'qa.yaml').write_text('id: qa\ntype: ai\noutput_mode: hybrid\n' + (f'output: {output}\n' if output else ''))
    (nodes / 'next.yaml').write_text('id: next\ntype: ai\noutput_mode: hybrid\noutput: __source__\n')
    root = start(c)
    claim(c, root)
    finish(c, root)
    leaf = c.state.tasks[root].waiting_for_task_id
    claim(c, leaf)
    return root, leaf


def events(c):
    return [json.loads(line) for line in (c.root / 'data/events.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('source', ['user', 'goal'])
@pytest.mark.parametrize('consumed', [False, True])
def test_leaf_preempt_completion_race(case, source, consumed):
    c = case
    root, leaf = chain(c)
    task = c.state.tasks[leaf]
    old_execution = task.execution_id
    original_execution = c.service.read(c.sid)['execution']
    if source == 'user':
        assert c.state.execution_runtime.preempt_execution(c.sid, leaf, old_execution,
            'NEW USER REQUEST', 'race-user', roots=[root], source='user',
            attachments=[{'path': 'new-input.txt'}])
    else:
        control(c, 'set', goal='NEW EDITED GOAL')
        c.service.tick()
        assert task.preempt_requested
    if not consumed:
        queued = c.state.execution_runtime.queue_result(delivery_id='pending-receipt', work_id='work',
            origin_task_id=leaf, session_id=c.sid, text='Background evidence')
        assert queued['strategy'] == 'preempt' and queued['task_id'] == leaf
    pending = task.preempt_message
    attachments = list(task.preempt_attachments)
    if consumed:
        c.state.consume_preempt_message(leaf)
    result = {'action': 'finish', 'context_ref': 'qa-context',
              'result': {'text': 'Old QA result', 'attachments': [{'path': 'qa-report.txt'}]}}
    c.state.complete_task(task_id=leaf, worker_id='test-worker', execution_id=old_execution, result=result)
    before_retry = len(events(c))
    # A repeated ACK for the old claim must neither reroute nor deliver the reply twice.
    c.state.complete_task(task_id=leaf, worker_id='test-worker', execution_id=old_execution, result=result)
    assert len(events(c)) == before_retry
    if consumed:
        assert task.status == TaskStatus.completed
        assert c.state.tasks[root].status == TaskStatus.completed
        assert c.service.documents[c.sid]['_candidate']['entry_task_id'] == root
        return
    assert task.status == TaskStatus.pending
    assert task.preempt_message == pending and task.preempt_attachments == attachments
    assert task.preempt_delivery_ids == ['pending-receipt']
    assert c.state.tasks[root].status == TaskStatus.suspended
    assert c.state.tasks[root].waiting_for_task_id == leaf
    assert c.service.read(c.sid)['execution'] == original_execution
    assert c.service.documents[c.sid]['_candidate'] is None
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    assert task.input['context_ref'] == 'qa-context'
    assert task.input['resume_data']['type'] == 'unconsumed_preempt'
    replies = [e for e in events(c) if e['type'] == 'intermediate_reply' and e['payload'].get('task_id') == leaf]
    assert len(replies) == 1
    assert replies[0]['payload']['text'] == 'Old QA result'
    assert replies[0]['payload']['attachments'] == [{'path': 'qa-report.txt'}]
    assert replies[0]['payload']['execution_id'] == old_execution
    snapshots = [e['payload'] for e in events(c) if e['type'] == 'task_resumed' and e['payload'].get('task_id') == leaf]
    assert snapshots[-1]['status'] == 'pending'
    assert snapshots[-1]['preempt_delivery_ids'] == ['pending-receipt']
    assert snapshots[-1]['preempt_message'] == pending
    assert snapshots[-1]['execution_id'] == old_execution
    claim(c, leaf)
    assert task.execution_id != old_execution
    c.state.complete_task(task_id=leaf, worker_id='test-worker', execution_id=old_execution, result=result)
    assert task.status == TaskStatus.running
    assert len([e for e in events(c) if e['type'] == 'intermediate_reply' and e['payload'].get('task_id') == leaf]) == 1
    assert c.state.execution_runtime._receipts['pending-receipt']['status'] == 'queued'
    c.store.append(task.input.get('child_session_id') or task.session_id,
        Message(id='admitted-update', role='user', content=pending, source_task_id=leaf,
                meta={'delivery_ids': ['pending-receipt']}))
    c.state.consume_preempt_message(leaf, pending, len(attachments), delivery_ids=['pending-receipt'],
        message_id='admitted-update', execution_id=task.execution_id)
    assert not task.preempt_delivery_ids
    finish(c, leaf, 'Updated QA result')
    assert task.status == TaskStatus.completed
    assert c.state.tasks[root].status == TaskStatus.completed
    assert c.state.execution_runtime._receipts['pending-receipt']['status'] == 'processed'
    assert c.service.documents[c.sid]['_candidate']['entry_task_id'] == root
    outbound = [e for e in events(c) if e['type'] == 'outbound_message' and e['payload'].get('task_id') == leaf]
    assert len(outbound) == 1 and outbound[0]['payload']['text'] == 'Updated QA result'


@pytest.mark.parametrize('route', ['no-output', 'next-output', 'reject-limit'])
def test_other_finish_routes_do_not_bypass_leaf_preempt(case, route):
    c = case
    root, leaf = chain(c, '' if route == 'no-output' else 'next' if route == 'next-output' else '__source__')
    task = c.state.tasks[leaf]
    assert c.state.execution_runtime.preempt_execution(c.sid, leaf, task.execution_id,
        'New requirement', 'other-route', roots=[root], source='user')
    if route == 'reject-limit':
        c.state.tasks[root].input['_output_chain_reject_count'] = 100
    before = set(c.state.tasks)
    c.state.complete_task(task_id=leaf, worker_id='test-worker', execution_id=task.execution_id,
        result={'action': 'reject' if route == 'reject-limit' else 'finish', 'result': {'text': 'Old result'}})
    assert task.status == TaskStatus.pending and task.preempt_message == 'New requirement'
    assert c.state.tasks[root].status == TaskStatus.suspended
    assert set(c.state.tasks) == before
    assert c.service.documents[c.sid]['_candidate'] is None
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
