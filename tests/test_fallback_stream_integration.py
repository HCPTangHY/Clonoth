"""Offline checks for alternate-provider streaming and per-attempt timing."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from engine.hooks.types import HookResult
from engine.inference import llm_call
from engine.inference.loop_state import _LoopState
from engine.node import Node
from providers.base import ProviderResponse


class Provider:
    name = "alternate"
    model = "offline"

    def __init__(self, *, thinking="reasoning", response=None, wait=False):
        self.thinking = thinking
        self.response = response or ProviderResponse(ok=True, text="answer")
        self.wait = wait
        self.calls = []
        self.cancelled = False

    async def chat(self, **kwargs):
        self.calls.append("chat")
        return self.response

    async def chat_stream(self, *, messages, on_text, on_thinking, **kwargs):
        self.calls.append("stream")
        messages[0]["content"] = "provider-private mutation"
        try:
            if self.thinking:
                await on_thinking(self.thinking)
            if self.wait:
                await asyncio.Event().wait()
            await on_text(self.response.text)
            return self.response
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def state(stream=True):
    request_ids = iter(("alternate-1", "alternate-2", "alternate-3"))
    rctx = SimpleNamespace(
        workspace_root=None, task_id="task", session_id="session",
        begin_llm_request=lambda: next(request_ids), emit_event=AsyncMock(),
        check_cancelled=AsyncMock(return_value=False),
        check_preempted=AsyncMock(return_value={}),
    )
    ls = _LoopState(
        rctx=rctx, node=Node(id="test", type="ai"), provider=Provider(),
        registry=None, run_id="run", context_ref="", runtime_cfg={},
        streaming=stream, use_stream=stream, messages=[{"role": "user", "content": "input"}],
        system_prompt=[], is_block_mode=False, openai_tools=[], history=[],
        collected_attachments=[], tool_produced_attachments=[], retry_max=5,
    )
    ls._reasoning_started_iso = "primary-start"
    ls._reasoning_ended_iso = "primary-end"
    return ls


async def invoke(ls, candidate):
    with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())) as hooks:
        result = await llm_call.call_provider_once(ls, 0, candidate)
    assert [call.args[0] for call in hooks.await_args_list] == ["before_llm_call"]
    return result


@pytest.mark.parametrize("stream", [False, True])
def test_alternate_preserves_stream_setting_and_request_isolation(stream):
    async def check():
        ls, candidate = state(stream), Provider()
        primary, original = ls.provider, copy.deepcopy(ls.messages)
        assert (await invoke(ls, candidate)).ok
        assert candidate.calls == (["stream"] if stream else ["chat"])
        assert ls.provider is primary and ls.messages == original
        assert ls.use_stream is stream and ls.retry_max == 5
        if stream:
            assert ls._reasoning_started_iso not in ("", "primary-start")
            assert ls._reasoning_ended_iso not in ("", "primary-end")
            deltas = [c.args[1] for c in ls.rctx.emit_event.await_args_list if c.args[0] == "stream_delta"]
            assert deltas and all(d["llm_request_id"] == "alternate-1" for d in deltas)
        else:
            assert ls._reasoning_started_iso == ls._reasoning_ended_iso == ""
    asyncio.run(check())


def test_success_without_reasoning_clears_previous_attempt_times():
    async def check():
        ls = state()
        await invoke(ls, Provider())
        assert ls._reasoning_started_iso
        await invoke(ls, Provider(thinking=""))
        assert ls._reasoning_started_iso == ls._reasoning_ended_iso == ""
    asyncio.run(check())


def test_failed_candidate_keeps_primary_timing_and_has_no_retry_or_compaction():
    async def check():
        for code in (503, 413):
            ls = state()
            candidate = Provider(response=ProviderResponse(ok=False, text="", status_code=code, error="context too long"))
            with patch("engine.compact.microcompact_messages", side_effect=AssertionError("second compaction")):
                assert not (await invoke(ls, candidate)).ok
            assert candidate.calls == ["stream"]
            assert ls._reasoning_started_iso == "primary-start"
            assert ls._reasoning_ended_iso == "primary-end"
    asyncio.run(check())


def test_cancelled_stream_stops_transport_without_replacing_timing():
    async def check():
        ls, candidate = state(), Provider(wait=True)
        ls.rctx.check_cancelled.return_value = True
        result = await invoke(ls, candidate)
        assert result.action == "cancelled"
        assert candidate.cancelled
        assert ls._reasoning_started_iso == "primary-start"
    asyncio.run(check())


def test_attachment_preempt_stops_reasoning_and_propagates_payload():
    async def check():
        ls, candidate = state(), Provider(wait=True)
        payload = {"preempted": True, "attachments": [{"path": "example.png"}]}
        ls.rctx.check_preempted.return_value = payload
        assert await invoke(ls, candidate) is None
        assert candidate.cancelled and ls.preempt_inject_info is payload
        assert ls._reasoning_started_iso == "primary-start"
    asyncio.run(check())


def test_worker_cancellation_stops_stream_transport():
    async def check():
        ls, candidate = state(), Provider(wait=True)
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            task = asyncio.create_task(llm_call.call_provider_once(ls, 0, candidate))
            while not candidate.calls:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert candidate.cancelled
        assert ls._reasoning_started_iso == "primary-start"
    asyncio.run(check())


def test_exception_keeps_timing_and_propagates_scheduling_flags():
    async def fail(call, step):
        assert call.use_stream and call._reactive_compact_done
        assert call._reasoning_started_iso == call._reasoning_ended_iso == ""
        call.preempt_after_step = True
        raise RuntimeError("offline transport failure")

    async def check():
        ls = state()
        with patch.object(llm_call, "_call_llm_with_retry", side_effect=fail):
            with pytest.raises(RuntimeError, match="offline transport failure"):
                await llm_call.call_provider_once(ls, 0, Provider())
        assert ls.preempt_after_step
        assert ls._reasoning_started_iso == "primary-start"
    asyncio.run(check())
