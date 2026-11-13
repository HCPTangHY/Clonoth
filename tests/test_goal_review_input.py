"""Review input contract: raw current trace, immutable old outputs, no silent clipping."""
import copy
import json
from types import SimpleNamespace

import pytest

from plugins.ide.goal import GoalService, _empty
from plugins.ide.goal_review import ReviewInputError, build_input, encode
from test_goal_plugin import env, launch, candidate, answer, command
from test_goal_integration import case, start, claim, finish, reviewer, verdict


def material(latest='a'):
    data = _empty('main')
    data.update(goal='Verify the check', criteria='Check returns PASS', goal_version=1,
                _candidate={'entry_task_id': latest, 'producer_task_id': 'qa', 'result': {'text': 'done'}})
    tasks = [{'task_id': tid, 'node_id': tid, 'status': 'completed',
              'result': {'action': 'finish', 'result': {'text': tid + ' done'}}} for tid in ['a', 'b', 'c', 'qa']]
    evidence = []
    outputs = []
    for i, task in enumerate(tasks):
        tid = task['task_id']
        evidence.extend([
            {'source_task_id': tid, 'id': tid + '-call', 'role': 'assistant', 'content': 'check ' + tid,
             'tool_calls': [{'id': 'call-' + tid, 'function': {'name': 'execute_command', 'arguments': '{"command":"check"}'}}],
             'response_info': {'model': 'unused'}, 'meta': {'usage': {'tokens': 900}, 'reasoning': 'not needed'}},
            {'source_task_id': tid, 'id': tid + '-result', 'role': 'tool', 'content': 'PASS ' + tid,
             'tool_call_id': 'call-' + tid, 'name': 'execute_command',
             'meta': {'tool_result_raw_inline': 'PASS ' + tid, 'attachments': [{'path': tid + '.txt'}]}},
        ])
        outputs.append({'id': tid + '-finish', 'task_id': tid, 'kind': 'finish', 'text': tid + ' done',
                        'attachments': [], 'sequence': i + 1})
    snap = {'tasks': tasks, 'evidence': evidence, 'outputs': outputs, 'truncated_task_ids': [],
            'output_truncated_task_ids': [], 'version': 'v1'}
    node = SimpleNamespace(extra={'context_limit': 100_000}, prompt='fixed rules')
    return data, snap, node


def test_latest_is_main_entry_not_last_task_or_output_producer():
    data, snap, node = material()
    instruction, info = build_input(data, snap, node)
    payload = json.loads(instruction)
    current = payload['current']['latest_main_task']
    assert current['task_id'] == info['latest_main_task_id'] == 'a'
    assert [row['content'] for row in current['trace']] == ['check a', 'PASS a']
    assert {row['task_id'] for row in payload['completed_outputs']} == {'b', 'c', 'qa'}
    assert 'PASS b' not in instruction and 'PASS qa' not in instruction
    assert 'usage' not in instruction and 'reasoning' not in instruction
    assert current['trace'][0]['tool_calls'][0]['arguments'] == '{"command":"check"}'
    assert current['trace'][1]['attachments'] == [{'path': 'a.txt'}]


def test_new_round_appends_and_retains_exact_prefix_even_for_late_outputs():
    data, snap, node = material('b')
    snap['outputs'] = snap['outputs'][:1]
    second, _ = build_input(data, snap, node)
    old_prefix = second.split('],"current":', 1)[0]
    data['_candidate']['entry_task_id'] = 'c'
    data['rounds'] = 2
    snap['outputs'].append({'id': 'b-reply', 'task_id': 'b', 'kind': 'reply', 'text': 'b reply', 'sequence': 3})
    third, _ = build_input(data, snap, node)
    assert third.startswith(old_prefix + ',')
    prefix3 = third.split('],"current":', 1)[0]
    # Arrives late with an earlier sort key. Never insert before committed records.
    snap['outputs'].insert(0, {'id': 'late', 'task_id': 'a', 'kind': 'reply', 'text': 'late reply', 'sequence': 0})
    fourth, _ = build_input(data, snap, node)
    assert fourth.startswith(prefix3 + ',')
    assert [row['id'] for row in data['_review_outputs']] == ['a-finish', 'b-reply', 'late']
    assert build_input(data, snap, node)[0] == fourth


def test_conflicting_output_identity_pauses_instead_of_rewriting():
    data, snap, node = material('b')
    build_input(data, snap, node)
    snap['outputs'][0]['text'] = 'changed'
    with pytest.raises(ReviewInputError, match='发生变化'):
        build_input(data, snap, node)
    assert data['_review_outputs'][0]['text'] == 'a done'


def test_old_trace_truncation_does_not_exclude_old_finish_or_block_current():
    data, snap, node = material('b')
    snap.update(evidence_truncated=True, truncated_task_ids=['a'])
    text, _ = build_input(data, snap, node)
    assert 'a done' in text and 'PASS b' in text
    assert 'PASS a' not in text
    snap['truncated_task_ids'].append('b')
    with pytest.raises(ReviewInputError, match='完整过程未被保存'):
        build_input(data, snap, node)


@pytest.mark.parametrize('missing', ['call', 'result', 'all', 'output'])
def test_incomplete_material_is_not_a_reviewer_failure(missing):
    data, snap, node = material()
    if missing == 'output':
        snap['output_truncated_task_ids'] = ['qa']
    else:
        remove = {'call': ['a-call'], 'result': ['a-result'], 'all': ['a-call', 'a-result']}[missing]
        snap['evidence'] = [row for row in snap['evidence'] if row['id'] not in remove]
    with pytest.raises(ReviewInputError):
        build_input(data, snap, node)


@pytest.mark.parametrize('limit', [128000, 384000, 0])
def test_review_budget_inherits_global_threshold_without_a_private_default(limit):
    data, snap, node = material()
    node.extra.clear()
    snap['evidence'][1]['content'] = '完整工具结果' * 10000
    config = {'engine': {'compact': {'threshold_tokens': limit}}}
    text, info = build_input(data, snap, node, runtime_cfg=config)
    assert info['context_limit'] == limit
    assert info['context_limit_source'] == 'runtime'
    assert info['estimated_total_tokens'] > 8192
    assert info['input_bytes'] > info['estimated_total_tokens']
    assert json.loads(text)['current']['latest_main_task']['trace'][1]['content'] == '完整工具结果' * 10000


def test_review_budget_keeps_explicit_override_and_main_loop_fallback():
    data, snap, node = material()
    config = {'engine': {'compact': {'threshold_tokens': 384000}}}
    assert build_input(data, snap, node, runtime_cfg=config)[1]['context_limit'] == 100000
    node.extra['context_limit'] = 0
    assert build_input(data, snap, node, runtime_cfg=config)[1]['context_limit_source'] == 'node'
    node.extra['context_limit'] = None
    assert build_input(data, snap, node, runtime_cfg=config)[1]['context_limit'] == 384000
    node.extra.clear()
    assert build_input(data, snap, node)[1]['context_limit'] == 100000


@pytest.mark.parametrize('invalid', [True, False, -1, 2048, '384000', 384000.0])
def test_review_budget_rejects_invalid_explicit_values(invalid):
    data, snap, node = material()
    node.extra['context_limit'] = invalid
    with pytest.raises(ReviewInputError, match='token 预算'):
        build_input(data, snap, node)


def test_capacity_change_uses_current_runtime_config_only_after_explicit_resume(env, monkeypatch):
    root = launch(env)
    monkeypatch.setattr('engine.node.load_node', lambda *args: SimpleNamespace(extra={}, prompt='rules'))
    config = env.root / 'config/runtime.yaml'
    config.parent.mkdir(exist_ok=True)
    config.write_text('engine:\n  compact:\n    threshold_tokens: 8192\n')
    env.rt.tasks[root]['status'] = 'completed'
    env.rt.facts[root] = {'evidence': [{'source_task_id': root, 'role': 'assistant', 'content': '完整执行证据' * 10000}]}
    env.service.on_event({'kind': 'candidate', 'session_id': 'main', 'candidate': {'entry_task_id': root, 'result': {'text': 'done'}}})
    env.service.tick()
    paused = env.service.read('main')
    assert paused['status'] == 'paused' and '估算' in paused['pause_reason']
    assert not env.rt.reviews and not env.service.documents['main']['_review_attempts']
    config.write_text('engine:\n  compact:\n    threshold_tokens: 384000\n')
    env.service.tick()
    assert env.service.read('main') == paused
    command(env, 'resume')
    env.service.tick()
    review = env.service.documents['main']['_review']
    assert review['context_limit'] == 384000 and review['token_count_source'] == 'estimate'
    assert len(env.rt.reviews) == len(env.rt.inputs) == 1
    assert env.rt.reviews[0][1]['current']['latest_main_task']['trace'][0]['content'] == '完整执行证据' * 10000


@pytest.mark.parametrize('value', ['中文\n"\\\u0000' * 200, 'ascii' * 500])
def test_budget_counts_actual_json_and_fixed_prompt_without_clipping(value):
    data, snap, node = material()
    snap['evidence'][1]['content'] = value
    text, info = build_input(data, snap, node)
    assert json.loads(text)['current']['latest_main_task']['trace'][1]['content'] == value
    from engine.compact import estimate_message_tokens
    expected = (estimate_message_tokens({'role': 'system', 'content': node.prompt})
                + estimate_message_tokens({'role': 'user', 'content': text}))
    assert info['estimated_input_tokens'] == expected
    assert info['estimated_total_tokens'] == expected + info['reserved_tokens']
    assert info['input_bytes'] == len(text.encode('utf-8'))
    assert info['token_count_source'] == 'estimate'
    node.extra['context_limit'] = info['estimated_total_tokens']
    assert build_input(data, snap, node)[0] == text
    node.extra['context_limit'] -= 1
    with pytest.raises(ReviewInputError, match='context_limit'):
        build_input(data, snap, node)


def test_current_artifact_content_is_available_without_file_tools():
    data, snap, node = material()
    snap['evidence'].append({'source_task_id': 'a', 'artifact': 'call-a.txt', 'content': 'full original result', 'truncated': False})
    payload = json.loads(build_input(data, snap, node)[0])
    assert payload['current']['latest_main_task']['trace'][-1]['content'] == 'full original result'


def test_frozen_outputs_survive_goal_reload_and_reset_on_goal_edit(env):
    launch(env)
    answer(env, candidate(env))
    candidate(env)
    frozen = encode(env.service.documents['main']['_review_outputs'])
    assert 'input-0' in frozen
    command(env, 'pause')
    env.service.close()
    env.service = GoalService(env.rt, env.root, background=False)
    assert encode(env.service.documents['main']['_review_outputs']) == frozen
    command(env, 'set', goal='new goal')
    assert env.service.documents['main']['_review_outputs'] == []


def test_overflow_pauses_without_consuming_review_or_continuation_budget(env, monkeypatch):
    root = launch(env)
    monkeypatch.setattr('engine.node.load_node', lambda *args: SimpleNamespace(extra={'context_limit': 2049}, prompt='rules'))
    env.rt.tasks[root]['status'] = 'completed'
    env.rt.facts[root] = {'evidence': [{'source_task_id': root, 'role': 'assistant', 'content': 'done'}]}
    env.service.on_event({'kind': 'candidate', 'session_id': 'main', 'candidate': {'entry_task_id': root, 'result': {'text': 'done'}}})
    env.service.tick()
    first = env.service.read('main')
    assert first['status'] == 'paused' and 'context_limit' in first['pause_reason']
    for _ in range(4):
        env.service.tick()
    assert env.service.read('main') == first
    assert len(env.rt.inputs) == 1 and not env.rt.reviews
    assert first['rounds'] == first['stagnation'] == 0
    assert not env.service.documents['main']['_review_attempts']
    monkeypatch.setattr('engine.node.load_node', lambda *args: SimpleNamespace(extra={'context_limit': 8192}, prompt='rules'))
    command(env, 'resume')
    env.service.tick()
    assert len(env.rt.inputs) == 1 and len(env.rt.reviews) == 1


def test_output_chain_supplies_qa_finish_but_only_main_tool_trace(case):
    c = case
    (c.root/'config/nodes/main.yaml').write_text('id: main\ntype: ai\noutput_mode: hybrid\noutput: qa\n')
    (c.root/'config/nodes/qa.yaml').write_text('id: qa\ntype: ai\noutput_mode: hybrid\noutput: __source__\n')
    root = start(c)
    claim(c, root)
    finish(c, root, 'Draft')
    qa = c.state.tasks[root].waiting_for_task_id
    claim(c, qa)
    finish(c, qa, 'Checked final')
    rid = reviewer(c)
    payload = json.loads(c.state.tasks[rid].input['instruction'])
    assert payload['current']['latest_main_task']['task_id'] == root
    assert payload['completed_outputs'][0]['task_id'] == qa
    assert payload['completed_outputs'][0]['text'] == 'Checked final'
    assert [row['id'] for row in payload['current']['latest_main_task']['trace']] == [root + '-call', root + '-result']


def test_real_two_round_policy_has_one_rejection_then_completion(case):
    c = case
    root = start(c)
    claim(c, root)
    finish(c, root, 'first report')
    first = reviewer(c)
    finish(c, first, verdict(False), evidence=False)
    c.service.tick()
    second = c.service.documents[c.sid]['_roots'][-1]
    claim(c, second)
    finish(c, second, 'second report')
    last = reviewer(c)
    payload = json.loads(c.state.tasks[last].input['instruction'])
    assert payload['completed_outputs'][0]['text'] == 'first report'
    trace = payload['current']['latest_main_task']['trace']
    assert trace[0]['tool_calls'][0]['id'] == trace[1]['tool_call_id'] == 'check'
    assert trace[1]['content'] == 'PASS'
    finish(c, last, verdict(True), evidence=False)
    c.service.tick()
    data = c.service.read(c.sid)
    assert data['status'] == 'completed' and data['rounds'] == 1
    assert [row['verdict']['passed'] for row in data['reviews']] == [False, True]
    assert len(c.service.documents[c.sid]['_roots']) == 2


def test_preview_requires_full_artifact_and_is_replaced_without_duplication():
    data, snap, node = material()
    snap['evidence'][1]['meta'].update(tool_result_truncated=True, tool_result_ref='data/artifacts/tool_results/a/result.txt')
    with pytest.raises(ReviewInputError, match='artifact 缺失'):
        build_input(data, snap, node)
    snap['evidence'].append({'source_task_id': 'a', 'artifact': 'result.txt', 'content': 'full result', 'truncated': False})
    trace = json.loads(build_input(data, snap, node)[0])['current']['latest_main_task']['trace']
    assert len(trace) == 2 and trace[1]['content'] == 'full result'


def test_anthropic_wire_has_stable_output_blocks_and_cache_boundaries():
    import asyncio
    from unittest.mock import Mock
    from providers.anthropic import AnthropicProvider
    from plugins.ide.goal_review import review_cache_blocks
    provider = AnthropicProvider(http=Mock(), api_key='test', base_url='https://example.invalid', model='test')
    data, snap, node = material('b')
    node.id = 'ide.goal_reviewer'
    snap['outputs'] = snap['outputs'][:1]
    text2, _ = build_input(data, snap, node)

    def wire(text):
        ctx = SimpleNamespace(node=node, rctx=SimpleNamespace(is_system_task=True), provider=provider,
                              messages=[{'role': 'system', 'content': 'fixed'}, {'role': 'user', 'content': text}])
        assert asyncio.run(review_cache_blocks(ctx)).modified
        assert ''.join(row['text'] for row in ctx.messages[1]['content']) == text
        return provider.build_payload(ctx.messages, tool_mode='fake-native')

    second = wire(text2)
    data['_candidate']['entry_task_id'] = 'c'
    data['reviews'] = [{'output_ids': ['a-finish'], 'verdict': None}]
    snap['outputs'].append({'id': 'b-finish', 'task_id': 'b', 'kind': 'finish', 'text': 'b done', 'sequence': 2})
    third = wire(build_input(data, snap, node)[0])
    blocks2 = second['messages'][0]['content']
    blocks3 = third['messages'][0]['content']
    assert [b['text'] for b in blocks2[:2]] == [b['text'] for b in blocks3[:2]]
    assert 'cache_control' in blocks2[1] and 'cache_control' in blocks3[1] and 'cache_control' in blocks3[2]
    assert 'cache_control' not in blocks3[-1]
    assert second['system'] == third['system']
    assert sum('cache_control' in b for b in blocks3) <= 2
    provider._options['cache_control'] = False
    ctx = SimpleNamespace(node=node, rctx=SimpleNamespace(is_system_task=True), provider=provider,
                          messages=[{'role': 'user', 'content': text2}])
    assert asyncio.run(review_cache_blocks(ctx)) is None
    assert ctx.messages[0]['content'] == text2


@pytest.mark.parametrize('status', [200, 500])
def test_reply_event_carries_execution_and_marks_unacknowledged_output(tmp_path, status):
    import asyncio
    import httpx
    from unittest.mock import AsyncMock, Mock
    from engine.context import RunContext
    from engine.restart_recovery import runtime_state
    response = httpx.Response(status, request=httpx.Request('POST', 'http://test/events'))
    http = SimpleNamespace(post=AsyncMock(return_value=response))
    ctx = RunContext(workspace_root=tmp_path, supervisor_url='http://test', session_id='s', worker_id='w',
                     http=http, llm_http=Mock(), task_id='task', execution_id='execution')
    asyncio.run(ctx.emit_event('intermediate_reply', {'text': 'reply', 'task_id': 'task'}))
    assert http.post.call_args.kwargs['json']['payload']['execution_id'] == 'execution'
    assert bool(getattr(ctx, '_history_write_error', '')) == (status != 200)
    assert bool(runtime_state(ctx).get('checkpoint_error')) == (status != 200)


def test_engine_persists_spill_identity_for_review(tmp_path):
    from engine.inference.ai_step import _persist_tool_results
    from test_engine_execution_migration import make_ls, RecordingStore
    ls = make_ls(tmp_path, store=RecordingStore())
    _persist_tool_results(ls, [{'id': 'tool', 'name': 'execute_command', 'raw_inline': 'preview',
        'truncated': True, 'ref': 'data/artifacts/tool_results/task/result.txt'}], [])
    meta = ls.messages[-1]['_meta']
    assert meta['tool_result_truncated'] is True
    assert meta['tool_result_ref'] == 'data/artifacts/tool_results/task/result.txt'
    assert ls.rctx.conversation_store.rows[-1].meta['tool_result_ref'] == meta['tool_result_ref']
