"""One response tool per model response; over-limit batches have no side effects."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.builtin.finish_guard import FinishGuardHandler
from engine.hooks.types import HookResult
from engine.inference import ai_step
from engine.inference.pseudo_tools import _finish_spec, _ask_spec, _reply_spec
from test_engine_execution_migration import make_ls, response, tool_call, RecordingStore


@pytest.mark.parametrize('names', [
    ['intermediate_reply'] * 2, ['finish'] * 2, ['ask'] * 2,
    ['intermediate_reply', 'finish'], ['ask', 'finish'], ['intermediate_reply', 'ask'],
])
def test_duplicate_or_mixed_response_tools_rejected(names):
    import asyncio
    ctx = SimpleNamespace(tool_calls=[tool_call(str(i), name) for i, name in enumerate(names)],
                          extra={}, node=SimpleNamespace(id='test'), step=0)
    result = asyncio.run(FinishGuardHandler().handle(ctx))
    assert result.block
    assert result.reason == 'response_tools_colocated'


def test_burst_blocks_whole_batch_preserves_pairing_and_resets(tmp_path):
    import asyncio

    async def run():
        store = RecordingStore()
        ls = make_ls(tmp_path, allowed_real={'write_file'}, store=store)
        guard = FinishGuardHandler()

        async def hook(_point, ctx):
            return await guard.handle(ctx) or HookResult()

        burst = [tool_call('write', 'write_file', {'path': 'never'})]
        burst += [tool_call(str(i), 'intermediate_reply', {'text': 'spam'}) for i in range(3216)]
        with patch.object(ai_step.hook_registry, 'afire', side_effect=hook), \
             patch.object(ai_step, '_handle_pseudo_tool', new_callable=AsyncMock) as handler:
            result = await ai_step._handle_tool_calls_inner(ls, response(*burst), 0)
            assert result is None
            handler.assert_not_awaited()
            assert not ls.registry.started
            assert not ls.rctx.emit_event.await_args_list
            results = [m for m in ls.messages if m.get('role') == 'tool']
            assert [m['tool_call_id'] for m in results] == [c.id for c in burst]
            assert all(m['_meta']['tool_rejected'] for m in results)
            assert len(store.rows) == len(burst) + 1
            await ai_step._handle_tool_calls_inner(
                ls, response(tool_call('next', 'intermediate_reply', {'text': 'one message'})), 1,
            )
            handler.assert_awaited_once()
    asyncio.run(run())


def test_single_reply_with_real_tool_still_allowed():
    import asyncio
    ctx = SimpleNamespace(tool_calls=[tool_call('a', 'intermediate_reply'), tool_call('b', 'read_file')],
                          extra={}, node=SimpleNamespace(id='test'), step=0)
    assert asyncio.run(FinishGuardHandler().handle(ctx)) is None


def test_specs_explain_combined_limit():
    for factory in (_finish_spec, _ask_spec, _reply_spec):
        text = factory()['function']['description']
        assert 'ONCE IN TOTAL' in text
        assert 'ALL tool calls' in text
        assert 'except intermediate_reply' not in text
