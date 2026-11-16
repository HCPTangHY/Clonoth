"""Goal policy on real runtime: live edits, output chains, accepted input and check-ins."""
import copy
from datetime import datetime, timezone
from unittest.mock import patch

from test_goal_integration import case, claim, control, finish, reviewer, start, verdict


def test_real_runtime_checkin_is_deduped_and_does_not_restart(case):
    c = case
    with patch('plugins.ide.goal.time.time', return_value=1000):
        root = start(c)
    with patch('supervisor.execution_runtime.time.time', return_value=1000):
        claim(c, root)
    c.state.tasks[root].last_activity_at = datetime.fromtimestamp(1000, timezone.utc)
    with patch('plugins.ide.goal.time.time', return_value=2800):
        c.service.tick()
        message = c.state.tasks[root].preempt_message
        assert 'Goal 定期进度检查' in message
        c.service.tick()
        assert c.state.tasks[root].preempt_message == message
    assert c.service.documents[c.sid]['_roots'] == [root]
    assert c.service.read(c.sid)['status'] in {'running', 'waiting'}
    assert not c.state.tasks[root].cancel_requested


def test_edit_during_review_reuses_no_completed_task_and_ignores_old_approval(case):
    c = case
    old = start(c)
    claim(c, old)
    finish(c, old)
    review = reviewer(c)
    version = c.service.read(c.sid)['goal_version']
    control(c, 'set', goal='Updated while reviewing')
    c.service.tick()
    new = c.service.documents[c.sid]['_roots'][-1]
    assert new != old
    assert c.service.read(c.sid)['goal_version'] == version + 1
    finish(c, review, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] in {'running', 'waiting'}
    assert not c.service.read(c.sid)['reviews'][-1]['applied']
    claim(c, new)
    finish(c, new)
    current = reviewer(c)
    finish(c, current, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'


def test_output_leaf_user_preempt_keeps_root_identity_and_candidate(case):
    c = case
    (c.root / 'config/nodes/main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\noutput: qa\n')
    (c.root / 'config/nodes/qa.yaml').write_text('id: qa\ntype: ai\noutput_mode: hybrid\noutput: __source__\n')
    root = start(c)
    original = c.service.read(c.sid)['execution']
    claim(c, root)
    finish(c, root)
    leaf = c.state.tasks[root].waiting_for_task_id
    claim(c, leaf)
    target = c.service.read(c.sid)['preempt_target']
    assert target['task_id'] == leaf
    assert c.state.execution_runtime.preempt_execution(c.sid, leaf, c.state.tasks[leaf].execution_id,
        'Please verify the final details', 'user-output-update', roots=[root], source='user')
    assert c.service.read(c.sid)['execution'] == original
    assert c.service.documents[c.sid]['_roots'] == [root]
    c.state.consume_preempt_message(leaf)
    finish(c, leaf, 'New details verified')
    assert c.service.documents[c.sid]['_candidate']['entry_task_id'] == root
    review = reviewer(c)
    finish(c, review, verdict(True), evidence=False)
    c.service.tick()
    assert c.service.read(c.sid)['status'] == 'completed'


def test_completed_goal_ignores_user_activity_and_long_elapsed_time(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root)
    review = reviewer(c)
    finish(c, review, verdict(True), evidence=False)
    c.service.tick()
    before = copy.deepcopy(c.service.documents[c.sid])
    tasks = set(c.state.tasks)
    c.service.on_event({'kind': 'input_routed', 'session_id': c.sid, 'task_id': root, 'user_input': True})
    with patch('plugins.ide.goal.time.time', return_value=10**12):
        c.service.tick()
    assert c.service.documents[c.sid] == before
    assert set(c.state.tasks) == tasks
