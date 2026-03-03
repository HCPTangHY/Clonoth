from __future__ import annotations

from typing import Any

from .base import BaseProvider, ProviderResponse


class AnthropicProvider(BaseProvider):
    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        return ProviderResponse(ok=False, error="Anthropic provider not implemented yet")
