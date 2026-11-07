"""DeepSeek Chat Completions, sharing OpenAI transport and error handling.

The official endpoint and original model default are retained. Thinking is
controlled by reasoning_effort; "none" disables it and history replay. The
legacy thinking=False constructor argument is still accepted.
"""
from __future__ import annotations

from typing import Any

import httpx

from .openai import OpenAIProvider

_DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIProvider):
    provider_name = "deepseek"
    default_reasoning_field = "reasoning_content"
    default_reasoning_effort = "high"

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_key: str,
        base_url: str | None = None,
        model: str = "deepseek-v4-pro",
        timeout: float = 600.0,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        reasoning_field: str | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise RuntimeError("deepseek api_key is empty")
        self._owns_http = http is None
        # Validate options before allocating an owned client.
        super().__init__(
            http=http, api_key=api_key, base_url=(base_url or "").strip() or _DEFAULT_BASE_URL,
            model=model, reasoning_field=reasoning_field,
            reasoning_effort=reasoning_effort, max_tokens=max_tokens,
            max_completion_tokens=max_completion_tokens, provider_options=provider_options,
        )
        if self._owns_http:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        if self._max_tokens is None:
            self._max_tokens = self._max_completion_tokens
        self._max_completion_tokens = None
        self._thinking = thinking is not False and (self._reasoning_effort or "").lower() != "none"
        self._always_backfill_reasoning = self._thinking
        if not self._thinking:
            self._reasoning_effort = None
            self._reasoning_field = ""

    def prepare_history(
        self, messages: list[dict[str, Any]], *, tool_mode: str = "native",
    ) -> list[dict[str, Any]]:
        from .tool_format import create_tool_formatter

        history = super().prepare_history(messages, tool_mode=tool_mode)
        if not self._thinking or self._reasoning_field != "reasoning_content" or create_tool_formatter(tool_mode).mode != "native":
            return history
        for message in history:
            meta = message.get("_meta")
            if message.get("role") != "assistant" or not isinstance(meta, dict) or meta.get("provider") != self.provider_name:
                continue
            metadata = meta.get("metadata")
            saved = metadata.get(self.provider_name) if isinstance(metadata, dict) else None
            if not isinstance(saved, dict) or "reasoning_content" in saved:
                continue
            # ponytail: recover only the known legacy DeepSeek shape; other
            # historical schemas need an explicit metadata migration.
            if saved.get("provider") != self.provider_name:
                continue
            if not meta.get("thought_summary") and isinstance(meta.get("reasoning"), str):
                message.setdefault("reasoning_content", meta["reasoning"])
        return history

    def build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, tool_mode: str = "native", stream: bool = False,
    ) -> dict[str, Any]:
        payload = super().build_payload(messages, tools, tool_mode=tool_mode, stream=stream)
        payload["thinking"] = {"type": "enabled" if self._thinking else "disabled"}
        return payload

    def _build_provider_meta(self) -> dict[str, Any]:
        # Do not collide with the configurable native response field "thinking".
        return {"provider": self.provider_name, "thinking_enabled": self._thinking}

    async def aclose(self) -> None:
        """Close only an HTTP client created by this provider."""
        if self._owns_http:
            await self._http.aclose()
