"""DeepSeek V4 Provider — extends OpenAI-compatible provider with thinking mode.

Why: DeepSeek V4 uses OpenAI-compatible chat/completions but adds:
  1. `thinking` parameter to enable/disable CoT reasoning
  2. `reasoning_effort` parameter (high/max)
  3. `reasoning_content` in both request (for multi-turn with tool calls) and response
  4. Base URL is https://api.deepseek.com (no /v1 suffix)

How: Subclass OpenAIProvider, override payload construction to inject DS-specific params,
     and handle reasoning_content round-trip for tool call scenarios.

Purpose: First-class DeepSeek support with proper thinking chain passthrough,
         without polluting the generic OpenAI provider.

Ref: https://api-docs.deepseek.com/guides/thinking_mode
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall
from .openai import OpenAIProvider, _normalize_base_url, _extract_usage, _extract_openai_error_message, _parse_first_json_object

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek V4 provider with native thinking mode support.

    Inherits OpenAI-compatible streaming/non-streaming logic, but injects
    thinking parameters and handles reasoning_content round-trip.
    """

    provider_name = "deepseek"

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_key: str,
        base_url: str | None = None,
        model: str = "deepseek-v4-pro",
        timeout: float = 600.0,
        thinking: bool = True,
        reasoning_effort: str = "high",
    ) -> None:
        # If no http client passed, create one with appropriate timeout
        if http is None:
            http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
            self._owns_http = True
        else:
            self._owns_http = False

        # Default base_url for DeepSeek
        if not base_url:
            base_url = _DEFAULT_BASE_URL

        super().__init__(
            http=http,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        # Override provider_name after super().__init__ sets it to "openai"
        self.name = self.provider_name

        self._thinking = thinking
        self._reasoning_effort = reasoning_effort

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build DeepSeek-specific request payload.

        Handles:
        - thinking mode toggle
        - reasoning_effort
        - reasoning_content passthrough for tool call multi-turn
        """
        # Prepare messages: strip internal fields, prefill guard
        prepared = self._prepare_messages(messages)

        # DeepSeek-specific: preserve reasoning_content in assistant messages
        # that contain tool_calls (required by API for multi-turn reasoning)
        final_messages = []
        for msg in prepared:
            if msg.get("role") == "assistant":
                clean_msg = {"role": "assistant"}
                if msg.get("content") is not None:
                    clean_msg["content"] = msg["content"]
                # Pass through reasoning_content if present (needed for tool call chains)
                if msg.get("reasoning_content"):
                    clean_msg["reasoning_content"] = msg["reasoning_content"]
                # Pass through tool_calls if present
                if msg.get("tool_calls"):
                    clean_msg["tool_calls"] = msg["tool_calls"]
                final_messages.append(clean_msg)
            else:
                final_messages.append(msg)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": final_messages,
        }

        # Thinking mode
        if self._thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self._reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}

        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        return payload

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        """Streaming chat with DeepSeek thinking mode.

        Uses the same SSE parsing as OpenAI but with DS-specific payload.
        DS streams reasoning_content via delta.reasoning_content (same field
        that OpenAIProvider already parses at L229).
        """
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, stream=True)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self._http.stream("POST", url, headers=headers, json=payload) as resp:
                status = int(resp.status_code)

                if status >= 400:
                    body = await resp.aread()
                    try:
                        data = json.loads(body)
                        msg = _extract_openai_error_message(data)
                    except Exception:
                        msg = body.decode("utf-8", errors="replace").strip()
                    if not msg:
                        msg = f"HTTP {status}"
                    return ProviderResponse(ok=False, error=msg, status_code=status, inline_data=[], provider_meta={})

                text_parts: list[str] = []
                reasoning_parts: list[str] = []
                tc_map: dict[int, dict[str, Any]] = {}
                stream_usage: dict[str, int] | None = None

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue

                    # Usage (last chunk)
                    raw_usage = chunk.get("usage")
                    if isinstance(raw_usage, dict):
                        stream_usage = _extract_usage(chunk)

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # Text content
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        if on_text:
                            await on_text(content)

                    # Reasoning chain (DeepSeek uses delta.reasoning_content)
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                        if on_thinking:
                            await on_thinking(reasoning)

                    # Tool calls (same format as OpenAI)
                    raw_tcs = delta.get("tool_calls")
                    if isinstance(raw_tcs, list):
                        for tc in raw_tcs:
                            if not isinstance(tc, dict):
                                continue
                            idx = int(tc.get("index", 0))
                            fn = tc.get("function") or {}
                            if not isinstance(fn, dict):
                                fn = {}
                            fn_name = fn.get("name")
                            if idx not in tc_map:
                                tc_map[idx] = {
                                    "id": tc.get("id") or "",
                                    "name": fn_name or "",
                                    "arg_parts": [],
                                }
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_start",
                                        "index": idx,
                                        "id": tc_map[idx]["id"],
                                        "name": tc_map[idx]["name"],
                                    })
                            else:
                                if tc.get("id"):
                                    tc_map[idx]["id"] = tc["id"]
                                if fn_name:
                                    tc_map[idx]["name"] = fn_name
                            arg_chunk = fn.get("arguments", "")
                            if arg_chunk:
                                tc_map[idx]["arg_parts"].append(arg_chunk)
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": idx,
                                        "delta": arg_chunk,
                                    })

                text = "".join(text_parts) if text_parts else None
                reasoning_text = "".join(reasoning_parts) if reasoning_parts else None

                tool_calls: list[ToolCall] = []
                for idx in sorted(tc_map.keys()):
                    tc_data = tc_map[idx]
                    name = tc_data["name"]
                    if not name:
                        continue
                    raw_args = "".join(tc_data["arg_parts"])
                    args: dict[str, Any] = {}
                    if raw_args.strip():
                        try:
                            parsed = json.loads(raw_args)
                            args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                        except Exception:
                            fallback = _parse_first_json_object(raw_args)
                            args = fallback if fallback is not None else {"_raw": raw_args}
                    tool_calls.append(ToolCall(id=tc_data["id"], name=name.strip(), arguments=args))

                return ProviderResponse(
                    ok=True, text=text, tool_calls=tool_calls,
                    reasoning=reasoning_text, status_code=status, usage=stream_usage,
                    inline_data=[], provider_meta={"provider": "deepseek", "thinking": self._thinking},
                )

        except Exception as e:
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        """Non-streaming chat with DeepSeek thinking mode."""
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, stream=False)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(url, headers=headers, json=payload)
            status = int(resp.status_code)

            if status >= 400:
                try:
                    data = resp.json()
                    msg = _extract_openai_error_message(data)
                except Exception:
                    msg = resp.text.strip()
                if not msg:
                    msg = f"HTTP {status}"
                return ProviderResponse(ok=False, error=msg, status_code=status, inline_data=[], provider_meta={})

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})

            text = message.get("content")
            reasoning_text = message.get("reasoning_content")

            # Parse tool calls
            tool_calls: list[ToolCall] = []
            raw_tool_calls = message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "").strip()
                    if not name:
                        continue
                    raw_args = fn.get("arguments", "")
                    args: dict[str, Any] = {}
                    if raw_args and isinstance(raw_args, str) and raw_args.strip():
                        try:
                            parsed = json.loads(raw_args)
                            args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                        except Exception:
                            fallback = _parse_first_json_object(raw_args)
                            args = fallback if fallback is not None else {"_raw": raw_args}
                    tool_calls.append(ToolCall(id=tc.get("id", ""), name=name, arguments=args))

            usage = _extract_usage(data)

            return ProviderResponse(
                ok=True, text=text, tool_calls=tool_calls,
                reasoning=reasoning_text, status_code=status, usage=usage,
                raw=data, inline_data=[], provider_meta={"provider": "deepseek", "thinking": self._thinking},
            )

        except Exception as e:
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})
