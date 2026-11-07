"""Reasoning timestamps belong to the current provider attempt, including retries."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from engine.hooks.types import HookResult
from engine.inference import llm_call
from providers.base import ProviderResponse
from test_fallback_stream_integration import Provider, state


@pytest.mark.parametrize("stream", [False, True])
def test_primary_without_thinking_does_not_inherit_fallback_timing(stream):
    async def check():
        ls = state()
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            first = await llm_call.call_provider_once(ls, 0, Provider())
            assert first.ok and ls._reasoning_started_iso and ls._reasoning_ended_iso
            ls.provider = Provider(thinking=None)
            ls.use_stream = stream
            second = await llm_call._call_llm_with_retry(ls, 1)
        assert second.ok and second.reasoning is None
        assert ls._reasoning_started_iso == ls._reasoning_ended_iso == ""
    asyncio.run(check())


def test_primary_thinking_replaces_old_timing():
    async def check():
        ls = state()
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            response = await llm_call._call_llm_with_retry(ls, 0)
        assert response.ok
        assert ls._reasoning_started_iso not in ("", "primary-start")
        assert ls._reasoning_ended_iso not in ("", "primary-end")
    asyncio.run(check())


@pytest.mark.parametrize("retry_thinking", [None, "new attempt thinking"])
def test_retry_resets_failed_attempt_timing_before_transport(retry_thinking):
    async def check():
        ls = state()
        ls.retry_max = 1
        ls.retry_initial_delay = 0
        observed = []

        class RetryingProvider(Provider):
            async def chat_stream(self, **kwargs):
                observed.append((ls._reasoning_started_iso, ls._reasoning_ended_iso))
                if len(observed) == 1:
                    self.thinking = "failed attempt thinking"
                    self.response = ProviderResponse(ok=False, text="", status_code=503, error="retry")
                else:
                    self.thinking = retry_thinking
                    self.response = ProviderResponse(ok=True, text="answer")
                return await super().chat_stream(**kwargs)

        ls.provider = RetryingProvider()
        with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
            response = await llm_call._call_llm_with_retry(ls, 0)
        assert response.ok and observed == [("", ""), ("", "")]
        assert bool(ls._reasoning_started_iso) == bool(retry_thinking)
        assert bool(ls._reasoning_ended_iso) == bool(retry_thinking)
    asyncio.run(check())
