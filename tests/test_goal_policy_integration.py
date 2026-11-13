"""Additional Goal policy checks using real supervisor lifecycle machinery."""
import json

from test_goal_integration import case, start, claim, finish, reviewer, verdict, control


def test_clear_removes_review_conversation_but_not_main_history(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    tid = reviewer(c)
    review_session = c.state.tasks[tid].session_id
    finish(c, tid, verdict(False), evidence=False)
    # The actual engine writes review evidence into its separate history.
    from engine.conversation_store import Message
    c.store.append(review_session, Message(id='review-output', role='assistant', content='review evidence'))
    c.service.tick()
    path = c.root / 'data/conversations' / f'{review_session}.jsonl'
    assert path.exists()
    control(c, 'clear')
    assert not path.exists()
    assert c.service.read(c.sid)['status'] == 'empty'
    assert c.store.load(c.sid)


def test_clear_inflight_review_removes_late_history_on_completion(case):
    from engine.conversation_store import Message
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    tid = reviewer(c)
    task = c.state.tasks[tid]
    sid = task.session_id
    control(c, 'clear')
    assert c.service.read(c.sid)['status'] == 'empty'
    path = c.root / 'data/conversations' / f'{sid}.jsonl'
    for index in range(2):
        c.store.append(sid, Message(id=f'late-{index}', role='assistant', content='late verdict'))
        assert path.exists()
        c.state.complete_task(task_id=tid, worker_id='test-worker', execution_id=task.execution_id,
            result={'action': 'finish', 'result': {'text': verdict(True)}})
        assert not path.exists()
        assert c.service.read(c.sid)['status'] == 'empty'
    assert c.store.load(c.sid)


def test_review_context_budget_blocks_oversized_goal_explicitly(case):
    c = case
    (c.root / 'config/runtime.yaml').write_text('engine:\n  compact:\n    threshold_tokens: 8192\n')
    control(c, 'set', goal='目标' * 7000, criteria='保留证据')
    control(c, 'start')
    c.service.tick()
    root = c.service.documents[c.sid]['_roots'][-1]
    claim(c, root)
    finish(c, root)
    c.service.tick()
    data = c.service.read(c.sid)
    assert data['status'] == 'paused'
    assert 'context_limit' in data['pause_reason']
    assert c.service.documents[c.sid]['_review'] is None


def test_full_review_retains_prior_progress_evidence(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    first = reviewer(c)
    finish(c, first, verdict(False), evidence=False)
    c.service.tick()
    second = c.service.documents[c.sid]['_roots'][-1]
    claim(c, second)
    finish(c, second)
    tid = reviewer(c)
    payload = json.loads(c.state.tasks[tid].input['instruction'])
    assert payload['current']['previous_review']['passed'] is False
    assert root in {row['task_id'] for row in payload['completed_outputs']}
    assert payload['current']['latest_main_task']['task_id'] == second
    assert all(row.get('source_task_id', second) == second for row in payload['current']['latest_main_task']['trace'])
