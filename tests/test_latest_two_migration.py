"""Platform-neutral adaptations of upstream Round4 and finish/preempt ordering."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from engine.builtin.knowledge_inject import (
    build_scan_text, build_knowledge_messages, KnowledgeInjector, _build_skill_outline,
)
from engine.builtin.preempt import PreemptChecker, is_user_entry_task
from engine.builtin.plaintext import PlaintextRetryHandler
from engine.conversation_store import ConversationStore, Message
from engine.faces.prompt_sections import PromptSectionContext, prompt_section_registry
from engine.hooks.registry import HookRegistry
from engine.hooks.types import HookContext, HookResult
from engine.inference.message_assembly import (
    assemble_initial_messages, collect_active_injected_hashes,
)
from engine.node import Node
from engine.protocol import TaskAction
from engine.runner import _build_task_context
from providers.base import ToolCall
from supervisor.task_router import TaskRouterMixin
from test_engine_rw_callbacks import _task
from test_fallback_stream_integration import state, Provider


def skill(name, body, priority=0):
    return dict(id=name, kind='skill', content=body, strategy='keyword',
                keywords=['activate'], compiled_keywords=['activate'],
                scan_depth=0, node_ids=[], priority=priority, description='', path='')


def active_rows(dynamic):
    return [dict(role='user', content=m['content'], message_type='dynamic_context',
                 _meta={'injected_hashes': m['_injected_hashes']}) for m in dynamic]


def test_scan_uses_structured_types_and_multimodal_user_text():
    history = [
        {'role': 'user', 'content': 'earlier'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'actual image question'},
                                   {'type': 'text', 'text': '[File: notes.txt]\nDO_NOT_SCAN'},
                                   {'type': 'image_url', 'image_url': {'url': 'unused'}}]},
        {'role': 'user', 'content': 'SUMMARY_NOISE', '_meta': {'message_type': 'summary'}},
        {'role': 'user', 'content': 'CALLBACK_NOISE', 'message_type': 'dispatch_result'},
    ]
    text = build_scan_text('current', history, 1)
    assert 'actual image question' in text
    assert 'DO_NOT_SCAN' not in text and 'NOISE' not in text and 'earlier' not in text
    # Literal user content is never filtered by transport wrappers or text prefixes.
    literal = 'reject: 【当前用户指令】 this is user text'
    assert build_scan_text(literal, [], 0) == literal


def test_callback_instruction_does_not_activate_knowledge(tmp_path):
    folder = tmp_path / 'skills' / 'probe'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nkeywords: [activate]\n---\nSECRET_BODY')
    injector = KnowledgeInjector()
    context = PromptSectionContext(workspace_root=tmp_path, node=Node(id='n', type='ai'),
                                   instruction='activate', history=[],
                                   extra={'inbound_message': {'role': 'user', 'content': 'activate',
                                          'message_type': 'dispatch_result'}})
    assert injector.render_dynamic(context) is None
    assert 'probe' in injector.render_static(context)[0]['content']


def test_budget_counts_outline_and_drops_unaffordable_new_entries(tmp_path):
    entry = skill('long', 'B' * 8000)
    n = len(_build_skill_outline(entry))
    _, dynamic, _, _ = build_knowledge_messages(tmp_path, [entry], instruction_text='activate',
                                               skill_max_budget_chars=n - 1)
    assert dynamic == []
    static, dynamic, _, _ = build_knowledge_messages(tmp_path, [entry], instruction_text='activate',
                                                     skill_max_budget_chars=n)
    assert len(dynamic) == 1 and 'Large skill' in dynamic[0]['content']
    assert 'long' in static[0]['content']
    # A later larger budget does not upgrade the surviving outline implicitly.
    _, later, _, _ = build_knowledge_messages(tmp_path, [entry], instruction_text='activate',
                                             history=active_rows(dynamic), skill_max_budget_chars=20000)
    assert later == []


def test_surviving_bodies_do_not_consume_delta_budget_and_edits_reinject(tmp_path):
    first, second = skill('a', 'A' * 400), skill('b', 'B' * 400)
    _, old, _, _ = build_knowledge_messages(tmp_path, [first], instruction_text='activate')
    _, new, _, _ = build_knowledge_messages(tmp_path, [first, second], instruction_text='activate',
                                           history=active_rows(old), skill_max_budget_chars=500)
    assert 'B' * 400 in new[0]['content'] and 'A' * 400 not in new[0]['content']
    _, edited, _, _ = build_knowledge_messages(tmp_path, [{**first, 'content': 'edited'}],
                                              instruction_text='activate', history=active_rows(old))
    assert 'edited' in edited[0]['content']


def test_summary_hash_cannot_stand_in_for_discarded_body(tmp_path):
    entry = skill('old', 'ORIGINAL BODY')
    _, old, _, _ = build_knowledge_messages(tmp_path, [entry], instruction_text='activate')
    summary = {'role': 'user', 'message_type': 'summary', 'content': 'dialogue summary only',
               '_meta': {'injected_hashes': old[0]['_injected_hashes'],
                         'summarized_injected_hashes': old[0]['_injected_hashes']}}
    assert collect_active_injected_hashes([summary]) == {}
    _, reinjected, _, _ = build_knowledge_messages(tmp_path, [entry], instruction_text='activate',
                                                   history=[summary])
    assert 'ORIGINAL BODY' in reinjected[0]['content']


def loop(*, task_context=None, child=''):
    return SimpleNamespace(
        rctx=SimpleNamespace(task_context=task_context or {}, child_session_id=child,
                             task_id='entry', emit_event=AsyncMock(),
                             check_cancelled=AsyncMock(return_value=False),
                             check_preempted=AsyncMock(return_value={'preempted': True, 'message': 'next'})),
        node=Node(id='entry', type='ai'), plugin_data={}, messages=[],
        preempt_after_step=False, preempt_inject_info=None,
    )


@pytest.mark.parametrize('flags', [{'_system_task': True}, {'_output_chain': True},
                                  {'caller_task_id': 'parent'}, {'_dispatch_origin': {'id': 'x'}}])
def test_task_context_excludes_nonentry_finish_policy(flags):
    assert not is_user_entry_task(loop(task_context=_build_task_context(flags)))
    assert not is_user_entry_task(loop(child='child'))


@pytest.mark.asyncio
async def test_finish_hook_leaves_queued_input_for_supervisor_delivery_first():
    ls = loop()
    call = ToolCall(id='finish-id', name='finish', arguments={'text': 'completed answer'})
    hooks = HookRegistry()
    hooks.register('terminal_tool', PreemptChecker().handle_terminal)
    context = HookContext(messages=[], tools=[], node=ls.node, provider=None, rctx=ls.rctx,
                          extra={'loop_state': ls, 'terminal_call': call})
    result = await hooks.afire('terminal_tool', context)
    assert result.action is None and not result.skip_step
    ls.rctx.check_preempted.assert_not_awaited()
    events = []
    supervisor = SimpleNamespace(
        _route_session_id_for_task_locked=lambda task: task.session_id,
        _result_attachments=lambda result: [],
        eventlog=SimpleNamespace(append=lambda **kwargs: events.append(kwargs)),
        _event_task_snapshot=lambda event, task: events.append({'snapshot': event}),
    )
    task = _task('entry', 'session')
    task.preempt_requested = True
    task.preempt_message = 'next'
    task.node_id = 'entry'
    assert TaskRouterMixin._try_resume_entry_with_unconsumed_preempt_locked(
        supervisor, task, {'text': 'completed answer'})
    assert events[0]['type_'] == 'intermediate_reply'
    assert events[0]['payload']['text'] == 'completed answer'
    assert events[1]['snapshot'] == 'task_resumed'
    assert task.preempt_message == 'next' and task.input['resume_data']['type'] == 'unconsumed_preempt'


@pytest.mark.asyncio
async def test_repair_deferral_keeps_cancellation_and_hybrid_finish():
    ls = loop()
    ls.plugin_data = {'preempt': {'defer_for_output_reject': True}}
    context = SimpleNamespace(extra={'loop_state': ls}, node=ls.node,
                              response=SimpleNamespace(tool_calls=[], text='fixed'), step=0)
    assert await PreemptChecker().handle(context) is None
    ls.rctx.check_preempted.assert_not_awaited()
    ls.rctx.check_cancelled.return_value = True
    result = await PreemptChecker().handle(context)
    assert result.action.action == 'cancelled'
    ls.node.output_mode = 'hybrid'
    with patch('engine.builtin.plaintext._build_implicit_finish', return_value=TaskAction(action='finish')):
        result = await PlaintextRetryHandler().handle(context)
    assert result.action.action == 'finish'


@pytest.mark.asyncio
async def test_request_deferral_preserves_cancel_not_preempt():
    from engine.inference import llm_call
    ls = state(stream=False)
    ls.plugin_data = {'preempt': {'defer_for_output_reject': True}}
    ls.rctx.check_preempted.return_value = {'preempted': True, 'message': 'next'}
    async def delayed(**kwargs):
        await asyncio.sleep(.35)
        return Provider().response
    ls.provider.chat = delayed
    with patch.object(llm_call.hook_registry, 'afire', new=AsyncMock(return_value=HookResult())):
        response = await llm_call._call_llm_with_retry(ls, 0)
    assert response.ok
    ls.rctx.check_preempted.assert_not_awaited()
    assert ls.rctx.check_cancelled.await_count > 0


@pytest.mark.asyncio
async def test_duplicate_memory_id_never_selects_arbitrary_scope(tmp_path):
    from engine.builtin.dream import DreamHandler
    from engine.builtin.knowledge_inject.dream_router import DreamMemoryRouter
    from test_engine_knowledge_migration import make_ctx, hook_ctx
    route = DreamHandler._build_memory_route([
        {'book': 'notes', 'id': 'same', '_source_ns': 'owner', '_source_ws': '@first'},
        {'book': 'notes', 'id': 'same', '_source_ns': 'owner', '_source_ws': '@second'},
        {'book': 'notes', 'id': 'same', '_source_ns': 'owner', '_source_ws': '@third'},
    ])
    assert route['notes/same'] is None
    ctx, _ = make_ctx(tmp_path, node_id='system.dream')
    for action in ('list', 'upsert', 'delete'):
        hook = hook_ctx('system.dream', 'manage_memory',
                        {'action': action, 'book': 'notes', 'id': 'same'}, ctx, route=route)
        result = await DreamMemoryRouter().handle(hook)
        assert result.block and 'Ambiguous' in result.error_message
        assert not getattr(ctx, '_memory_target', None)


@pytest.mark.asyncio
async def test_dream_exact_upsert_keeps_workspace_and_source(tmp_path):
    import yaml
    from engine.builtin.knowledge_inject.dream_router import DreamMemoryRouter
    from engine.builtin.knowledge_inject import manage_memory
    from test_engine_knowledge_migration import make_ctx, hook_ctx
    path = tmp_path / 'data/memory/owner/@private-project/notes.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({'book': 'notes', 'entries': [
        {'id': 'same', 'content': 'original', 'source': 'auto', 'created_at': '2020-01-01',
         'constant': False, 'enabled': True}]}))
    tool_ctx, _ = make_ctx(tmp_path, node_id='system.dream')
    args = {'action': 'upsert', 'book': 'notes', 'id': 'same', 'content': 'updated'}
    route = {'notes': {'ns': 'owner', 'ws': ''},
             'notes/same': {'ns': 'owner', 'ws': '@private-project'}}
    hook = hook_ctx('system.dream', 'manage_memory', args, tool_ctx, route=route)
    await DreamMemoryRouter().handle(hook)
    assert tool_ctx._memory_target == ('owner', 'private-project')
    result = await manage_memory(args, tool_ctx)
    assert result['ok'], result
    entry = yaml.safe_load(path.read_text())['entries'][0]
    assert entry['content'] == 'updated' and entry['created_at'] == '2020-01-01'
    assert entry['source'] == 'auto'
    assert not (path.parents[1] / 'notes.yaml').exists()


def test_extraction_preserves_arbitrary_workspace_scope(tmp_path):
    from engine.builtin.memory_extract import MemoryExtractHandler
    from test_engine_knowledge_migration import fake_task, RUNTIME
    store = ConversationStore(tmp_path / 'data/conversations')
    store.append('s', Message(id='u', role='user', content='useful input', source_task_id='t1'))
    (tmp_path / 'data/sessions.json').write_text(json.dumps({'s': {'workspace': {'name': 'private-project'}}}))
    handler = MemoryExtractHandler()
    handler._memory_extract_pending_task_ids['s'] = ['t1']
    pending = handler._prepare_memory_extract_pending_locked(
        ctx={}, task=fake_task(session_id='s'), workspace_root=tmp_path,
        runtime_cfg=RUNTIME, session_id='s', task_count=1)
    assert pending['_source_ws'] == 'private-project'
    create = Mock()
    handler._dispatch_memory_extract_locked(ctx={'workspace_root': tmp_path, 'create_task': create},
                                            session_id='s', pending_extract=pending)
    assert create.call_args.kwargs['input_data']['_memory_route_ws'] == 'private-project'
    # Malformed scope data must not silently promote a scoped memory to node scope.
    (tmp_path / 'data/sessions.json').write_text('{bad')
    handler._memory_extract_pending_task_ids['s'] = ['t1']
    assert handler._prepare_memory_extract_pending_locked(
        ctx={}, task=fake_task(session_id='s'), workspace_root=tmp_path,
        runtime_cfg=RUNTIME, session_id='s', task_count=1) is None
