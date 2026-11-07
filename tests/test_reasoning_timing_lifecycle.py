"""Reasoning timestamps belong to the current transport attempt only."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from engine.hooks.types import HookResult
from engine.inference import llm_call
from providers.base import ProviderResponse
from test_fallback_stream_integration import Provider, state


def times(ls):
    return ls._reasoning_started_iso, ls._reasoning_ended_iso


@pytest.mark.parametrize("first_is_fallback", [False, True])
@pytest.mark.parametrize("next_stream", [False, True])
def test_next_primary_without_thinking_does_not_reuse_previous_times(first_is_fallback, next_stream):
    async def check():
        ls = state()
        thinking = Provider(response=ProviderResponse(ok=True, text="first", reasoning="thought"))
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            if first_is_fallback:
                first = await llm_call.call_provider_once(ls, 0, thinking)
            else:
                ls.provider = thinking
                first = await llm_call._call_llm_with_retry(ls, 0)
            assert first.ok and all(times(ls))
            ls.provider = Provider(thinking=None)
            ls.use_stream = next_stream
            second = await llm_call._call_llm_with_retry(ls, 1)
        assert second.ok and second.reasoning is None
        assert times(ls) == ("", "")
        assert ls.provider.calls == (["stream"] if next_stream else ["chat"])
    asyncio.run(check())


@pytest.mark.parametrize("second_thinking", [None, "new thought"])
def test_retry_uses_only_final_attempt_reasoning_times(second_thinking):
    class RetryProvider(Provider):
        def __init__(self):
            super().__init__()
            self.observed_starts = []

        async def chat_stream(self, **kwargs):
            self.observed_starts.append(times(ls))
            if not self.calls:
                self.thinking = "failed thought"
                self.response = ProviderResponse(ok=False, text="", status_code=503, error="offline retry")
            else:
                self.thinking = second_thinking
                self.response = ProviderResponse(ok=True, text="answer", reasoning=second_thinking)
            return await super().chat_stream(**kwargs)

    ls = state()
    candidate = RetryProvider()
    ls.provider = candidate
    ls.retry_max = 1
    ls.retry_initial_delay = 0

    async def check():
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            response = await llm_call._call_llm_with_retry(ls, 0)
        assert response.ok
        assert candidate.calls == ["stream", "stream"]
        assert candidate.observed_starts == [("", ""), ("", "")]
        assert bool(all(times(ls))) is bool(second_thinking)
        if second_thinking is None:
            assert times(ls) == ("", "")
    asyncio.run(check())


def test_nonstream_reasoning_has_no_invented_token_timestamps():
    async def check():
        ls = state(stream=False)
        ls.provider = Provider(response=ProviderResponse(ok=True, text="answer", reasoning="batch thought"))
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            response = await llm_call._call_llm_with_retry(ls, 0)
        assert response.reasoning == "batch thought"
        assert times(ls) == ("", "")
    asyncio.run(check())
