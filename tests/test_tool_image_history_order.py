"""Image result ordering: real JSONL round-trip and conservative legacy recovery.

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest tests/test_tool_image_history_order.py
"""
import asyncio
import base64
import copy
import json
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.builtin.attachment import AttachmentCollector
from engine.attachments import prepare_messages_for_llm
from engine.conversation_store import ConversationStore
from engine.hooks.types import HookResult
from engine.inference import ai_step
from engine.runner import _message_to_history_dict
from engine.protocol import ACTION_PREEMPTED, TaskAction
from providers import AnthropicProvider, GeminiAIStudioProvider, GeminiVertexProvider
from providers.tool_format import create_tool_formatter, repair_tool_result_pairing_with_stats
from test_engine_execution_migration import make_ls, FakeRegistry, response, tool_call, spec

_png_buffer = io.BytesIO()
Image.new('RGB', (2, 2), 'white').save(_png_buffer, 'PNG')
PNG = _png_buffer.getvalue()


@pytest.mark.parametrize('mode', ['native', 'fake-native', 'json'])
@pytest.mark.parametrize('count', [1, 2])
def test_image_tools_round_trip_keep_native_pairs(tmp_path, mode, count):
    async def run():
        image = tmp_path / 'data' / 'attachments' / 'test.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(PNG)
        attachment = {'path': 'data/attachments/test.png', 'type': 'image', 'mime_type': 'image/png'}
        registry = FakeRegistry({'generate_nai_image': spec('generate_nai_image')}, {
            'generate_nai_image': lambda args, ctx: {'ok': True, 'data': {'result': 'generated', 'attachments': [attachment]}},
        })
        store = ConversationStore(tmp_path / 'conversations')
        ls = make_ls(tmp_path, registry=registry, store=store, allowed_real={'generate_nai_image'})
        ls.formatter = create_tool_formatter(mode)
        ls.node.tool_mode = mode
        collector = AttachmentCollector()

        async def hook(point, ctx):
            if point == 'after_tool_call':
                return await collector.handle(ctx) or HookResult()
            return HookResult()

        calls = [tool_call(f'call_{i+1}', 'generate_nai_image') for i in range(count)]
        with patch.object(ai_step.hook_registry, 'afire', side_effect=hook):
            await ai_step._handle_tool_calls(ls, response(*calls), 0)
        loaded = ConversationStore(tmp_path / 'conversations').load('session')
        expected_roles = ['assistant'] + ['tool'] * count + ['user']
        assert [m.role for m in loaded] == expected_roles
        assert [m['role'] for m in ls.messages] == expected_roles
        assert loaded[-1].message_type == 'tool_result_attachment'
        assert ls.messages[-1]['_meta']['message_type'] == 'tool_result_attachment'
        assert ls.rctx.last_shadow_message_id == loaded[-1].id
        assert loaded[-1].content == ls.messages[-1]['content']
        assert any(part['type'] == 'image_url' for part in loaded[-1].content)
        history = [_message_to_history_dict(m) for m in loaded]
        repaired, stats = repair_tool_result_pairing_with_stats(history, native_results=True)
        assert not any(stats.values())
        assert [m['tool_call_id'] for m in repaired if m['role'] == 'tool'] == [c.id for c in calls]
        # Both live and reloaded history must deliver image bytes after the results,
        # through the same file:// resolution used for real inference requests.
        for original in (history, ls.messages):
            prepared = prepare_messages_for_llm(copy.deepcopy(original), tmp_path)
            for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
                provider = cls(http=object(), api_key='test', base_url='https://example.test', model='test')
                payload = provider.build_payload(prepared, tool_mode='native')
                parts = [p for content in payload['contents'] for p in content['parts']]
                assert sum('functionCall' in p for p in parts) == count
                assert sum('functionResponse' in p for p in parts) == count
                assert any(p.get('inlineData', {}).get('data') for p in parts)
                assert '[Tool call:' not in json.dumps(payload)
            provider = AnthropicProvider(http=object(), api_key='test', base_url='https://example.test', model='test')
            payload = provider.build_payload(prepared, tool_mode='native')
            parts = [p for m in payload['messages'] if isinstance(m['content'], list) for p in m['content']]
            result_positions = [i for i, p in enumerate(parts) if p['type'] == 'tool_result']
            image_positions = [i for i, p in enumerate(parts) if p['type'] == 'image']
            assert len(result_positions) == count and image_positions
            assert max(result_positions) < min(image_positions)
            assert all(parts[i]['source']['data'] for i in image_positions)
    asyncio.run(run())


def test_image_flush_on_early_action(tmp_path):
    async def run():
        image = tmp_path / 'data' / 'attachments' / 'test.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(PNG)
        attachment = {'path': 'data/attachments/test.png', 'type': 'image', 'mime_type': 'image/png'}
        registry = FakeRegistry({'generate_nai_image': spec('generate_nai_image')}, {
            'generate_nai_image': lambda args, ctx: {'ok': True, 'attachments': [attachment]},
        })
        store = ConversationStore(tmp_path / 'conversations')
        ls = make_ls(tmp_path, registry=registry, store=store, allowed_real={'generate_nai_image'})
        collector = AttachmentCollector()
        stopped = TaskAction(action=ACTION_PREEMPTED, node_id='audit')

        async def hook(point, ctx):
            if point == 'after_tool_call':
                await collector.handle(ctx)
                return HookResult(action=stopped)
            return HookResult()

        with patch.object(ai_step.hook_registry, 'afire', side_effect=hook):
            action = await ai_step._handle_tool_calls(ls, response(tool_call('one', 'generate_nai_image')), 0)
        assert action is stopped
        loaded = ConversationStore(tmp_path / 'conversations').load('session')
        assert [m.role for m in loaded] == ['assistant', 'tool', 'user']
        repaired, stats = repair_tool_result_pairing_with_stats([_message_to_history_dict(m) for m in loaded])
        assert not any(stats.values())
        assert repaired[-1]['content'] == ls.messages[-1]['content']
    asyncio.run(run())


def test_direct_result_writer_buffers_attachment_together(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    ls = make_ls(tmp_path, store=store)
    ls._pending_shadow_writes = []
    ls._step_tool_calls = [tool_call('one', 'generate_nai_image')]
    ls._step_tool_msg_start = 0
    # Exercise the fallback attachment path without the full round's accumulator.
    attachment = {'path': 'not-present.txt', 'type': 'file'}
    ai_step._persist_tool_results(ls, [{'id': 'one', 'name': 'generate_nai_image', 'raw_inline': 'done'}], [attachment])
    assert store.load('session') == []
    assert ls.messages[-1]['_meta']['message_type'] == 'tool_result_attachment'
    ai_step._flush_step_shadow_writes(ls)
    loaded = store.load('session')
    assert [m.role for m in loaded] == ['tool', 'user']
    assert ls.rctx.last_shadow_message_id == loaded[-1].id


def legacy_history():
    return [
        {'id': 'assistant-id', 'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'one', 'name': 'generate_nai_image', 'arguments': {'prompt': 'first'}},
            {'id': 'two', 'name': 'generate_nai_image', 'arguments': {'prompt': 'second'}},
        ], '_meta': {'tool_mode': 'native', 'metadata': {'gemini-aistudio': {'raw_parts': [
            {'functionCall': {'name': 'generate_nai_image', 'args': {'prompt': 'first'}}, 'thoughtSignature': 'sig-one'},
            {'functionCall': {'name': 'generate_nai_image', 'args': {'prompt': 'second'}}, 'thoughtSignature': 'sig-two'},
        ]}}}},
        {'id': 'image-id', 'role': 'user', 'message_type': 'tool_result_attachment', 'content': [
            {'type': 'text', 'text': 'image results'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}},
        ]},
        {'id': 'result-two', 'role': 'tool', 'tool_call_id': 'two', 'name': 'generate_nai_image', 'content': 'second'},
        {'id': 'result-one', 'role': 'tool', 'tool_call_id': 'one', 'name': 'generate_nai_image', 'content': 'first'},
    ]


@pytest.mark.parametrize('marker', ['message_type', '_meta', 'meta'])
def test_legacy_recovery_is_idempotent_and_preserves_images_and_signatures(marker):
    history = legacy_history()
    if marker != 'message_type':
        history[1][marker] = {'message_type': history[1].pop('message_type')}
    before = copy.deepcopy(history)
    repaired, stats = repair_tool_result_pairing_with_stats(history, native_results=True)
    assert history == before
    assert stats['reordered_tool_result_attachments'] == 1
    assert stats['fixed_orphan_tool_calls'] == stats['removed_orphan_tool_results'] == 0
    assert [m['id'] for m in repaired] == ['assistant-id', 'result-one', 'result-two', 'image-id']
    assert repaired[-1] == before[1]
    assert repaired[0] == before[0]
    again, stats2 = repair_tool_result_pairing_with_stats(repaired, native_results=True)
    assert again == repaired and not any(stats2.values())
    provider = GeminiAIStudioProvider(http=object(), api_key='test', base_url='https://example.test', model='test')
    payload = provider.build_payload(history, tool_mode='native')
    text = json.dumps(payload)
    assert 'sig-one' in text and 'sig-two' in text
    assert '[Tool call:' not in text


@pytest.mark.parametrize('case', ['user_input', 'missing', 'wrong_id', 'duplicate', 'another_turn', 'no_ids'])
def test_ambiguous_legacy_rows_do_not_cross_user_boundaries(case):
    history = legacy_history()
    if case == 'user_input':
        del history[1]['message_type']  # Even identical text/images are not a marker.
    elif case == 'missing':
        history.pop()
    elif case == 'wrong_id':
        history[-1]['tool_call_id'] = 'wrong'
    elif case == 'duplicate':
        history[-1]['tool_call_id'] = 'two'
    elif case == 'another_turn':
        history.insert(2, {'role': 'user', 'content': 'new user instruction'})
    else:
        for m in history[2:]:
            del m['tool_call_id']
    before = copy.deepcopy(history)
    _, stats = repair_tool_result_pairing_with_stats(history, native_results=True)
    assert history == before
    assert stats['reordered_tool_result_attachments'] == 0
    assert stats['fixed_orphan_tool_calls'] > 0


def test_consecutive_rounds_reusing_ids_stay_separate():
    first = legacy_history()
    second = copy.deepcopy(first)
    second[0]['id'] = 'second-assistant'
    boundary = {'role': 'user', 'content': 'Generate another pair', 'id': 'boundary'}
    repaired, stats = repair_tool_result_pairing_with_stats(first + [boundary] + second, native_results=True)
    assert stats['reordered_tool_result_attachments'] == 2
    assert stats['fixed_orphan_tool_calls'] == stats['removed_orphan_tool_results'] == 0
    assert [m['id'] for m in repaired] == [
        'assistant-id', 'result-one', 'result-two', 'image-id', 'boundary',
        'second-assistant', 'result-one', 'result-two', 'image-id',
    ]


def test_attachment_marker_does_not_reset_repeated_tool_guard(tmp_path):
    async def run():
        registry = FakeRegistry({'generate_nai_image': spec('generate_nai_image')})
        ls = make_ls(tmp_path, registry=registry, allowed_real={'generate_nai_image'})
        ls.messages = [legacy_history()[1]]
        ls.messages[0]['_meta'] = {'message_type': 'tool_result_attachment'}
        ls._after_prev_round_msg_idx = 0
        ls._last_round_tool_sig = json.dumps([('generate_nai_image', {})], sort_keys=True)
        ls._consecutive_round_tool_count = 3
        async def hook(*args):
            return HookResult()
        with patch.object(ai_step.hook_registry, 'afire', side_effect=hook):
            await ai_step._handle_tool_calls(ls, response(tool_call('one', 'generate_nai_image')), 3)
        assert not registry.started
        assert ls._consecutive_round_tool_count == 4
        assert 'DUPLICATE_CALL_BLOCKED' in ls.messages[-1]['content']
    asyncio.run(run())


def test_multiple_attachment_rows_between_results():
    history = legacy_history()
    history.insert(3, {**copy.deepcopy(history[1]), 'id': 'image-two'})
    repaired, stats = repair_tool_result_pairing_with_stats(history, native_results=True)
    assert [m['id'] for m in repaired] == ['assistant-id', 'result-one', 'result-two', 'image-id', 'image-two']
    assert stats['reordered_tool_result_attachments'] == 2
