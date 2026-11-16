"""Goal integration against real supervisor routing, storage, and task claims.

Only model responses are supplied by the test; no network or production state.
Run with TMPDIR=$PWD/data/temp python3.11 -B -m pytest -q this_file.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine.context import EngineContext
from engine.conversation_store import ConversationStore, Message
from engine.faces.nodes import PluginNodesFace
from engine.hooks.registry import HookRegistry
from plugins.ide.goal import GoalService, register_node
from supervisor.eventlog import EventLog
from supervisor.state import SupervisorState


@pytest.fixture
def case(tmp_path):
    nodes = tmp_path / 'config' / 'nodes'
    nodes.mkdir(parents=True)
    (nodes / 'main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\n', encoding='utf-8')
    with patch('supervisor.state.auto_discover_and_register', return_value={}), \
         patch('engine.hooks.loader.load_external_plugins', return_value=0):
        state = SupervisorState(workspace_root=tmp_path,
                                eventlog=EventLog(tmp_path / 'data' / 'events.jsonl', run_id='boot-test'),
                                policy=SimpleNamespace())
    state._default_entry_node = lambda: 'main'
    state._maybe_trigger_turn_summary_locked = lambda task: None
    sid = state.get_or_create_session(channel='web', conversation_key='web:test')
    hooks = HookRegistry()
    ctx = EngineContext(hooks=hooks)
    ctx.contributions.mount('nodes', PluginNodesFace())
    with hooks.collecting('ide-test'):
        register_node(ctx)
    service = GoalService(state.execution_runtime, tmp_path, background=False)
    c = SimpleNamespace(state=state, service=service, sid=sid, root=tmp_path,
                        store=ConversationStore(tmp_path / 'data' / 'conversations'))
    yield c
    service.close()
    hooks.unload_plugin('ide-test')


def control(c, action, **fields):
    data = c.service.read(c.sid)
    return c.service.operate(c.sid, {
        'action': action, 'expected_revision': data['revision'], 'expected_session_id': c.sid,
        'request_id': f"request-{data['revision']}-{action}", **fields,
    })


def start(c):
    control(c, 'set', goal='Make the checker pass', criteria='Checker output is PASS')
    control(c, 'start')
    c.service.tick()
    return c.service.documents[c.sid]['_roots'][-1]


def claim(c, task_id):
    item = c.state.assign_next_task(worker_id='test-worker')
    assert item and item['task_id'] == task_id, item
    return item


def finish(c, task_id, text='Work complete', *, evidence=True):
    task = c.state.tasks[task_id]
    if evidence:
        sid = task.input.get('child_session_id') or task.session_id
        c.store.append_batch(sid, [
            Message(id=task_id + '-call', role='assistant', content='', source_task_id=task_id,
                    tool_calls=[{'id': 'check', 'type': 'function', 'function': {'name': 'execute_command', 'arguments': '{"command":"check"}'}}]),
            Message(id=task_id + '-result', role='tool', content='PASS', source_task_id=task_id,
                    tool_call_id='check', name='execute_command', message_type='tool_result'),
        ])
    return c.state.complete_task(task_id=task_id, worker_id='test-worker', execution_id=task.execution_id,
                                result={'action': 'finish', 'result': {'text': text},
                                        '_execution_model': {'provider': 'openai', 'model': 'test-model'}})


def reviewer(c):
    c.service.tick()
    review = c.service.documents[c.sid]['_review']
    assert review, c.service.read(c.sid)
    claim(c, review['task_id'])
    return review['task_id']


def verdict(passed, progress=True):
    return json.dumps({'passed': passed, 'progress': progress, 'feedback': 'Verified' if passed else 'Run the checker again',
                       'missing_evidence': [] if passed else ['Checker details'], 'needs_user': False})


def test_normal_reply_precedes_review_and_failure_uses_new_input(case):
    c = case
    root = start(c)
    first = claim(c, root)
    finish(c, root)
    outputs = [e for e in c.state.eventlog.events if e['type'] == 'outbound_message']
    assert outputs and outputs[-1]['payload']['text'] == 'Work complete'
    assert c.state.tasks[root].status == 'completed'
    review_id = reviewer(c)
    assert c.state.tasks[review_id].caller_task_id is None
    assert c.state.tasks[review_id].input['_system_task'] is True
    assert c.state.tasks[review_id].input['_execution_options']['model'] == 'test-model'
    finish(c, review_id, verdict(False), evidence=False)
    c.service.tick()
    data = c.service.documents[c.sid]
    assert data['rounds'] == 1
    second = data['_roots'][-1]
    assert second != root
    assert c.state.tasks[second].source_inbound_seq != first['source_inbound_seq']
    assert 'Run the checker again' in c.state.tasks[second].input['instruction']
    assert not c.state.tasks[second].input.get('resume_data')
    claim(c, second)
    finish(c, second)
    review_id = reviewer(c)
    finish(c, review_id, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'
    assert c.service.read(c.sid)['rounds'] == 1


def test_workspace_pause_preserves_inflight_verdict_and_applies_after_resume(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    review_id = reviewer(c)
    before = c.service.read(c.sid)
    c.state.set_session_workspace(c.sid, {'name': 'another'})
    paused = c.service.read(c.sid)
    assert paused['status'] == 'paused'
    assert paused['goal_version'] == before['goal_version']
    finish(c, review_id, verdict(True), evidence=False)
    c.service.tick()
    saved = c.service.read(c.sid)
    assert saved['status'] == 'paused'
    assert saved['reviews'][-1]['verdict']['passed'] is True
    assert saved['reviews'][-1]['applied'] is False
    tasks_before = set(c.state.tasks)
    control(c, 'resume')
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'
    assert set(c.state.tasks) == tasks_before


def test_async_result_waits_for_delivery_admission_and_carrier_finish(case):
    c = case
    root = start(c)
    item = claim(c, root)
    ack = c.state.register_async_tool({'async_id': 'async-test', 'task_id': root,
        'execution_id': item['execution_id'], 'worker_id': 'test-worker', 'node_id': 'main',
        'session_id': c.sid, 'tool_name': 'checker'})
    assert ack['ok']
    finish(c, root)
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    c.state.finish_async_tool('async-test', {'status': 'done', 'execution_id': item['execution_id'],
        'boot_id': ack['boot_id'], 'target_generation': ack['target_generation']})
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    delivery = c.state.execution_runtime.queue_result(delivery_id='async-test:result', work_id='async-test',
        origin_task_id=root, session_id=c.sid, text='Checker returned PASS', target_generation=ack['target_generation'])
    carrier = delivery['task_id']
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    carrier_item = claim(c, carrier)
    msg = Message(id='received-result', role='user', content='Checker returned PASS', source_task_id=carrier,
                  meta={'delivery_ids': ['async-test:result']}, message_type='async_tool_result')
    c.store.append(c.state.tasks[carrier].session_id, msg)
    c.state.execution_runtime.admit(carrier, ['async-test:result'], msg.id, carrier_item['execution_id'])
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    finish(c, carrier)
    assert reviewer(c)


def test_original_output_chain_finishes_before_goal_candidate(case):
    c = case
    nodes = c.root / 'config' / 'nodes'
    (nodes / 'main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\noutput: qa\n')
    (nodes / 'qa.yaml').write_text('id: qa\ntype: ai\noutput_mode: hybrid\noutput: __source__\n')
    root = start(c)
    claim(c, root)
    finish(c, root, 'Draft')
    assert c.state.tasks[root].status == 'suspended'
    c.service.tick()
    assert c.service.documents[c.sid]['_review'] is None
    qa = c.state.tasks[root].waiting_for_task_id
    assert c.state.tasks[qa].node_id == 'qa'
    claim(c, qa)
    finish(c, qa, 'Checked final response')
    assert c.state.tasks[root].status == 'completed'
    rid = reviewer(c)
    assert c.service.documents[c.sid]['_candidate']['producer_task_id'] == qa
    outputs = [e for e in c.state.eventlog.events if e['type'] == 'outbound_message']
    assert outputs[-1]['payload']['text'] == 'Checked final response'
    finish(c, rid, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'


def test_paused_async_result_does_not_create_automatic_carrier(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    control(c, 'pause')
    tasks_before = set(c.state.tasks)
    result = c.state.execution_runtime.queue_result(delivery_id='held', work_id='held-work',
        origin_task_id=root, session_id=c.sid, text='late result')
    assert result['strategy'] == 'held'
    c.service.tick()
    assert set(c.state.tasks) == tasks_before
    assert c.service.read(c.sid)['status'] == 'paused'
    control(c, 'resume')
    assert len(c.state.tasks) == len(tasks_before) + 1


def test_step_limit_reached_runs_normal_review_and_continues_or_completes(case):
    c = case
    root = start(c)
    claim(c, root)
    sid = c.state.tasks[root].session_id
    c.store.append_batch(sid, [
        Message(
            id=root + '-call', role='assistant', content='running step 64 check', source_task_id=root,
            tool_calls=[{'id': 'step64', 'type': 'function', 'function': {'name': 'execute_command', 'arguments': '{"command":"pytest"}'}}],
            meta={'reasoning': 'drop me', 'usage': {'total_tokens': 5000}},
        ),
        Message(
            id=root + '-result', role='tool', content='partial progress ok', source_task_id=root,
            tool_call_id='step64', name='execute_command', message_type='tool_result',
            meta={'tool_result_structured': {'huge_duplicate': 'x' * 50000}},
        ),
    ])
    c.state.complete_task(
        task_id=root, worker_id='test-worker', execution_id=c.state.tasks[root].execution_id,
        result={
            'action': 'fail', 'node_id': 'main', 'error': 'Maximum step limit reached.',
            '_execution_model': {'provider': 'openai', 'model': 'test-model'},
        },
    )
    assert c.state.tasks[root].status == 'failed'
    assert 'partial progress ok' in json.dumps(c.state.execution_runtime.snapshot([root])['evidence'], ensure_ascii=False)

    # Must enter normal review instead of pausing on unresolved failure!
    rid1 = reviewer(c)
    payload1 = json.loads(c.state.tasks[rid1].input['instruction'])
    assert payload1['current']['latest_main_task']['task_id'] == root
    assert payload1['current']['latest_main_task']['status'] == 'failed'
    assert payload1['current']['candidate']['step_limit_reached'] is True

    # Reviewer rejects -> Goal archives failed root into _prior_runs and starts round 2 automatically.
    finish(c, rid1, verdict(False), evidence=False)
    c.service.tick()
    doc = c.service.documents[c.sid]
    assert doc['status'] in {'running', 'waiting'}
    assert doc['rounds'] == 1
    assert doc['_prior_runs'] == [[root]]
    second = doc['_roots'][-1]
    assert second != root

    claim(c, second)
    finish(c, second, 'All checks green')
    rid2 = reviewer(c)
    finish(c, rid2, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'


def test_large_4mb_trace_succeeds_and_extreme_truncation_auto_continues(case):
    c = case
    (c.root / 'config/runtime.yaml').write_text(
        'engine:\n  compact:\n    threshold_tokens: 900000\n',
        encoding='utf-8',
    )
    root = start(c)
    claim(c, root)
    sid = c.state.tasks[root].session_id
    # 400 KB tool output previously blew the 256 KB ceiling; now fits easily under 4 MB.
    c.store.append_batch(sid, [
        Message(
            id=root + '-call', role='assistant', content='', source_task_id=root,
            tool_calls=[{'id': 'big1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"a.txt"}'}}],
        ),
        Message(
            id=root + '-result', role='tool', content='A' * 400_000, source_task_id=root,
            tool_call_id='big1', name='read_file', message_type='tool_result',
        ),
    ])
    finish(c, root, 'Read 400KB and verified', evidence=False)
    assert c.state.execution_runtime.snapshot([root])['truncated_task_ids'] == []
    rid1 = reviewer(c)
    finish(c, rid1, verdict(False), evidence=False)
    c.service.tick()

    # Now simulate an extreme turn that exceeds even the configured storage ceiling -> auto-continues instead of pausing!
    second = c.service.documents[c.sid]['_roots'][-1]
    claim(c, second)
    with patch('supervisor.execution_runtime.EVIDENCE_LIMIT', 1024):
        c.store.append_batch(c.state.tasks[second].session_id, [
            Message(
                id=second + '-call', role='assistant', content='', source_task_id=second,
                tool_calls=[{'id': 'huge', 'type': 'function', 'function': {'name': 'execute_command', 'arguments': '{}'}}],
            ),
            Message(
                id=second + '-result', role='tool', content='B' * 5000, source_task_id=second,
                tool_call_id='huge', name='execute_command', message_type='tool_result',
            ),
        ])
        finish(c, second, 'Huge log turn', evidence=False)
    assert second in c.state.execution_runtime.snapshot([second])['truncated_task_ids']
    c.service.tick()
    doc = c.service.documents[c.sid]
    assert doc['status'] in {'running', 'waiting'}
    assert doc['rounds'] == 2
    third = doc['_roots'][-1]
    assert third != second
    assert '精简' in c.state.tasks[third].input['instruction']
