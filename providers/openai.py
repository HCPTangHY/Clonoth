from __future__ import annotations

import json
from typing import Any

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall


def _normalize_base_url(base_url: str | None) -> str:
    """Normalize base_url for OpenAI-compatible APIs.

    Accepts:
    - https://api.openai.com
    - https://api.openai.com/v1
    - https://proxy.example.com/v1

    Returns a base URL that ends with `/v1`.
    """

    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"

    if not base.endswith("/v1"):
        base = base + "/v1"

    return base


def _extract_openai_error_message(payload: Any) -> str | None:
    """Try to extract a human-readable error message from OpenAI-style JSON."""

    if not isinstance(payload, dict):
        return None

    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

    return None


class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider implemented with raw HTTP (no SDK).

    This adapter targets the Chat Completions API:
        POST {base_url}/chat/completions

    It supports tool calling via the `tools` field.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
    ) -> None:
        super().__init__(model=model)

        k = (api_key or "").strip()
        if not k:
            raise RuntimeError("openai api_key is empty")

        self._http = http
        self._api_key = k
        self._base_url = _normalize_base_url(base_url)

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ProviderResponse:
        url = f"{self._base_url}/chat/completions"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            r = await self._http.post(url, headers=headers, json=payload)
        except Exception as e:
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__)

        status = int(r.status_code)

        data: Any
        try:
            data = r.json()
        except Exception:
            data = None

        if status >= 400:
            msg = _extract_openai_error_message(data)
            if not msg:
                try:
                    msg = (r.text or "").strip()
                except Exception:
                    msg = ""
            if not msg:
                msg = f"HTTP {status}"

            return ProviderResponse(
                ok=False,
                error=msg,
                status_code=status,
                raw=data if isinstance(data, dict) else None,
            )

        if not isinstance(data, dict):
            return ProviderResponse(ok=False, error="invalid JSON response", status_code=status)

        try:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return ProviderResponse(ok=False, error="missing choices", status_code=status, raw=data)

            choice0 = choices[0]
            if not isinstance(choice0, dict):
                return ProviderResponse(ok=False, error="invalid choices[0]", status_code=status, raw=data)

            msg = choice0.get("message")
            if not isinstance(msg, dict):
                return ProviderResponse(ok=False, error="missing message", status_code=status, raw=data)

            content = msg.get("content")
            text = content if isinstance(content, str) else None

            tool_calls: list[ToolCall] = []
            raw_tcs = msg.get("tool_calls")
            if isinstance(raw_tcs, list):
                for tc in raw_tcs:
                    if not isinstance(tc, dict):
                        continue

                    if tc.get("type") != "function":
                        continue

                    tc_id = tc.get("id")
                    tc_id_str = tc_id if isinstance(tc_id, str) else ""

                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue

                    name = fn.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue

                    raw_args = fn.get("arguments")
                    args: dict[str, Any] = {}
                    if isinstance(raw_args, str) and raw_args.strip():
                        try:
                            parsed = json.loads(raw_args)
                            if isinstance(parsed, dict):
                                args = parsed
                            else:
                                args = {"_raw": parsed}
                        except Exception:
                            args = {"_raw": raw_args}

                    tool_calls.append(ToolCall(id=tc_id_str, name=name.strip(), arguments=args))

            return ProviderResponse(ok=True, text=text, tool_calls=tool_calls, status_code=status)

        except Exception as e:
            return ProviderResponse(ok=False, error=f"failed to parse response: {e}", status_code=status, raw=data)
