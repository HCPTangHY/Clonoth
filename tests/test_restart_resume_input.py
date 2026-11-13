"""Original resume payloads survive restart once; no model or live service calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from engine import runner
from engine.conversation_store import ConversationStore, Message
from engine.inference.resume_builder import apply_resume_messages, collect_resume_attachments, _build_resume_messages
from engine.node import Node
from providers.tool_format import create_tool_formatter


def context(tmp_path):
    store = ConversationStore(tmp_path/'history')
    store.append('s', Message(id='original', role='user', content='work', source_task_id='t'))
    return SimpleNamespace(task_id='t', session_id='s', child_session_id='', conversation_store=store,
                           emit_event=AsyncMock(), _resume_execution_id='old-execution')


def history(ctx):
    return [runner._message_to_history_dict(m, current_task_id='t') for m in ctx.conversation_store.load('s')]


def apply(ctx, messages, resume):
    asyncio.run(apply_resume_messages(messages=messages, resume_data=resume, formatter=create_tool_formatter('native'),
                                     node=Node(id='n',type='ai',tool_mode='native'), rctx=ctx))


@pytest.mark.parametrize('kind', ['child_result','batch_results','output_rejected'])
@pytest.mark.parametrize('already_consumed', [False,True])
def test_original_resume_payload_survives_nested_restart_once_with_attachments(tmp_path, kind, already_consumed):
    ctx=context(tmp_path)
    attachment={'path':'data/result.txt','type':'file'}
    payload={'type':kind,'resume_id':'result-1','child_node_id':'child','result':{'text':'original result','attachments':[attachment]},
             'entries':[{'kind':'node','node_id':'child','text':'original result','attachments':[attachment]}]}
    if already_consumed:
        apply(ctx,history(ctx),payload)
    nested={'type':'restart_interrupted','previous_resume_data':{'type':'restart_interrupted','previous_resume_data':payload}}
    messages=history(ctx)
    apply(ctx,messages,nested)
    persisted=ctx.conversation_store.load('s')
    owned=[m for m in persisted if m.meta.get('resume_id')=='result-1']
    assert owned and 'original result' in str([m.content for m in owned])
    assert attachment in collect_resume_attachments(None,nested)
    assert 'result.txt' in str([m.content for m in owned])
    before=(tmp_path/'history/s.jsonl').read_bytes()
    apply(ctx,history(ctx),nested)
    assert (tmp_path/'history/s.jsonl').read_bytes()==before
    assert len([m for m in messages if (m.get('_meta') or {}).get('resume_id')=='result-1'])==len(owned)


def test_identical_results_from_distinct_resume_ids_are_not_deduplicated(tmp_path):
    ctx=context(tmp_path)
    for identity in ['child-one','child-two']:
        apply(ctx,history(ctx),{'type':'child_result','resume_id':identity,'result':{'text':'same'}})
    assert len(ctx.conversation_store.load('s'))==3


def test_explicit_output_rejected_preserves_native_pair_and_consumption_identity(tmp_path):
    ctx=context(tmp_path)
    ctx.conversation_store.append_batch('s',[
        Message(id='a',role='assistant',content='',tool_calls=[{'id':'f','name':'finish'}],source_task_id='t'),
        Message(id='r',role='tool',tool_call_id='f',name='finish',content='ok',source_task_id='t')])
    payload={'type':'output_rejected','resume_id':'reject1','_finish_tool_call_id':'f','reject_reason':'fix it'}
    apply(ctx,history(ctx),payload)
    rows=ctx.conversation_store.load('s')
    assert len(rows)==3 and rows[-1].id=='r' and rows[-1].meta['resume_id']=='reject1'
    apply(ctx,history(ctx),{'type':'restart_interrupted','previous_resume_data':payload})
    assert len(ctx.conversation_store.load('s'))==3


def test_resume_write_failure_is_not_marked_consumed(tmp_path):
    ctx=context(tmp_path)
    payload={'type':'child_result','resume_id':'retry','result':{'text':'keep'}}
    with patch.object(ctx.conversation_store,'append_batch',side_effect=OSError('disk full')):
        with pytest.raises(OSError):apply(ctx,history(ctx),payload)
    assert len(ctx.conversation_store.load('s'))==1
    apply(ctx,history(ctx),payload)
    assert len(ctx.conversation_store.load('s'))==2


@pytest.mark.parametrize('kind',['tool_results','batch_results'])
def test_cancelled_tool_results_render_reason_and_uncertain_side_effects(kind):
    entry={'kind':'tool','name':'write_file','status':'cancelled','cancelled':True,'text':'interrupted by restart'}
    payload={'type':kind,'tool_results':[entry],'entries':[entry]}
    text=str(_build_resume_messages(payload))
    assert 'cancelled' in text.lower() and 'interrupted by restart' in text and 'effects may already' in text
