"""OpenAI Responses API provider (/v1/responses).

Converts engine's chat/completions-style messages into the Responses API
'input' array format, sends requests, and parses both streaming and
non-streaming responses back into the unified ProviderResponse.

Key differences from chat/completions:
  - system messages become a top-level 'instructions' string
  - message items carry explicit 'type' fields (message / function_call / function_call_output)
  - content blocks use input_text/output_text/input_image instead of text/image_url
  - SSE events carry type in the JSON body, not in an 'event:' line
  - tool arguments arrive as strings and must be json.loads'd

Created 2026-05-01 to support the Responses API alongside the existing
chat/completions provider in openai.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from providers.base import BaseProvider, ProviderResponse, ToolCall
# Reuse the base-URL normalizer from the sibling openai provider
from providers.openai import _normalize_base_url

log = logging.getLogger(__name__)

# [2026-05-01] NativeToolFormatter stores tool results as user text, and older
# fake-native history stores tool calls as textual records.  The Responses
# provider recognizes both shapes so it can rebuild native input items without
# changing other providers or the global formatter contract.
_TOOL_RESULT_RE = re.compile(r'^Tool result for "(?P<name>[^"]+)":\r?\n(?P<output>.*)\Z', re.DOTALL)
# [2026-05-04] Engine pseudo-result markers may now contain dynamic names such
# as dispatch:target.id. Why: per-target dispatch stores the target in the tool
# name. How: allow colon, dot, and hyphen after the initial function-name
# character while preserving the older simple-name markers. Purpose: Responses
# can pair dynamic dispatch results with the exact preceding function_call.
_ENGINE_TOOL_RESULT_RE = re.compile(
    r'^\[(?P<name>[A-Za-z_][A-Za-z0-9_:\.-]*) result: (?P<output>.*)\]\Z',
    re.DOTALL,
)
_LEGACY_TOOL_RECORD_RE = re.compile(
    r'^\[Tool call history record: (?P<name>.+?) was executed with args: (?P<arguments>\{.*\})\]$'
)
_SYNTHETIC_TOOL_OUTPUT = "[No tool result was recorded by the engine.]"


def _strip_encrypted_content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove encrypted_content from reasoning items, keeping summary."""
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            # [2026-05-06] Why: OpenAI can reject stale encrypted reasoning
            # content when verifying prior turns. How: copy only reasoning items
            # and drop encrypted_content while preserving summary. Purpose: keep
            # useful visible reasoning summaries without resending unverifiable
            # encrypted blobs.
            new_item = {k: v for k, v in item.items() if k != "encrypted_content"}
            if "summary" not in new_item:
                new_item["summary"] = []
            cleaned.append(new_item)
        else:
            cleaned.append(item)
    return cleaned


def _is_encrypted_content_error(error_msg: str) -> bool:
    """Return True for OpenAI encrypted reasoning verification failures."""
    lower = str(error_msg or "").lower()
    # [2026-05-06] Why: generic verification/decryption errors may refer to
    # unrelated inputs. How: require an encrypted-content marker plus the known
    # OpenAI verification/decryption phrases. Purpose: retry only the safe
    # fallback path requested for reasoning.encrypted_content failures.
    has_encrypted_marker = "encrypted content" in lower or "encrypted_content" in lower
    has_verification_failure = "could not be verified" in lower or "could not be decrypted" in lower
    return has_encrypted_marker and has_verification_failure


def _response_error_message(resp: httpx.Response) -> str:
    """Extract an API error message from an HTTP response."""
    try:
        body = resp.json()
        return body.get("error", {}).get("message", resp.text[:500])
    except Exception:
        return resp.text[:500] if resp.text else f"HTTP {resp.status_code}"


class OpenAIResponsesProvider(BaseProvider):
    """Provider targeting the OpenAI Responses API (`POST /v1/responses`)."""

    # [provider-registry 2026-05-03] 这是自动发现注册使用的 key。
    # 原因：engine 不再硬编码 OpenAIResponsesProvider 分支；做法：类声明 provider_name；
    # 目的：registry 能把配置里的 "openai-responses" 映射回这个类。
    provider_name = "openai-responses"

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_options: dict[str, Any] | None = None,
    ):
        # [provider-registry 2026-05-03] 实例 name 复用 provider_name。
        # 原因：下游仍通过 provider.name 判断 provider 特性；做法：从类属性传入；
        # 目的：注册 key、实例名和配置 provider 字段保持一致。
        super().__init__(model=model, name=self.provider_name)
        self._http = http
        self._api_key = api_key
        # _normalize_base_url ensures the URL ends with /v1
        self._base_url = _normalize_base_url(base_url)
        self._options = provider_options or {}

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _endpoint(self) -> str:
        """Return the full POST URL for /v1/responses."""
        return self._base_url.rstrip("/") + "/responses"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Message conversion (chat/completions → Responses API input)
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Convert engine messages to Responses API format.

        Returns (instructions, input_items):
          - instructions: concatenated system message text (may be empty)
          - input_items: list of Responses-API items

        [2026-05-01] This converter accepts both chat/completions-style tool
        messages and Clonoth's storage format.  Clonoth stores tool calls as
        simplified assistant.tool_calls and stores tool results as user text;
        converting those pairs here lets OpenAI Responses receive native
        function_call/function_call_output items.
        """
        instructions_parts: list[str] = []
        items: list[dict[str, Any]] = []
        pending_calls: list[dict[str, str]] = []
        synthetic_call_index = 0

        def _next_call_id() -> str:
            nonlocal synthetic_call_index
            synthetic_call_index += 1
            return f"call_{synthetic_call_index}"

        def _append_function_call(call: dict[str, str]) -> None:
            call_id = call.get("call_id") or _next_call_id()
            item = {
                "type": "function_call",
                "name": call.get("name", ""),
                "call_id": call_id,
                "arguments": call.get("arguments") or "{}",
            }
            items.append(item)
            pending_calls.append({"call_id": call_id, "name": item["name"]})

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                # System messages are extracted into the top-level instructions field.
                instructions_parts.append(_extract_text(msg))
                continue

            if role == "user":
                text = _extract_text(msg)

                # Clonoth native tool results are stored as user text.  Pair the
                # result with the nearest pending tool call so Responses receives
                # the required function_call_output item instead of plain text.
                tool_result = _parse_native_tool_result(text) or _parse_engine_tool_result(text)
                if tool_result:
                    pending = _pop_pending_call(pending_calls, tool_result["name"])
                    if pending:
                        items.append({
                            "type": "function_call_output",
                            "call_id": pending["call_id"],
                            "output": tool_result["output"],
                        })
                        continue

                # Older fake-native history may already contain textual tool-call
                # records.  Rehydrate those records into native function_call
                # items while preserving any non-record text as a user message.
                remaining_text, legacy_calls = _split_legacy_tool_records(text)
                if legacy_calls:
                    if remaining_text.strip():
                        _flush_unpaired_function_calls(pending_calls, items)
                        items.append({
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": remaining_text}],
                        })
                    for legacy_call in legacy_calls:
                        _append_function_call(legacy_call)
                    continue

                # Normal user message → {type: message, role: user, content: [...]}.
                # If a previous terminal pseudo tool did not record a tool result,
                # synthesize one before the real user input so Responses receives
                # valid function_call/function_call_output pairing.
                _flush_unpaired_function_calls(pending_calls, items)
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": _build_user_content(msg),
                })
                continue

            if role == "assistant":
                _flush_unpaired_function_calls(pending_calls, items)
                # [2026-05-01] Prefer saved raw Responses output items so encrypted
                # reasoning content round-trips exactly.  Function-call items in
                # that raw output also seed pending_calls for the following tool result.
                _meta = msg.get("_meta")
                _raw_output = None
                if isinstance(_meta, dict):
                    _resp_meta = _meta.get("metadata", {}).get("openai-responses", {})
                    _raw_output = _resp_meta.get("raw_output")

                if _raw_output and isinstance(_raw_output, list):
                    for _raw_item in _raw_output:
                        if not isinstance(_raw_item, dict):
                            continue
                        # Copy before adding a missing summary so stored history is not mutated.
                        _item = dict(_raw_item)
                        if _item.get("type") == "reasoning" and "summary" not in _item:
                            _item["summary"] = []
                        items.append(_item)
                        if _item.get("type") == "function_call" and _item.get("call_id"):
                            pending_calls.append({
                                "call_id": str(_item.get("call_id") or ""),
                                "name": str(_item.get("name") or ""),
                            })
                    continue

                # No raw output: rebuild from content plus either chat/completions
                # tool_calls or Clonoth's simplified storage tool_calls.
                text = _extract_text(msg)
                if text:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for tc in msg.get("tool_calls", []):
                    normalized = _normalize_tool_call(tc)
                    if normalized:
                        _append_function_call(normalized)
                continue

            if role == "tool":
                # Chat/completions-style tool result.  Keep explicit tool_call_id
                # when present; otherwise fall back to the pending-call queue.
                call_id = str(msg.get("tool_call_id") or "")
                if call_id:
                    pending_calls[:] = [p for p in pending_calls if p.get("call_id") != call_id]
                else:
                    pending = _pop_pending_call(pending_calls, "")
                    call_id = pending["call_id"] if pending else ""
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _extract_text(msg),
                })

        _flush_unpaired_function_calls(pending_calls, items)
        return "\n\n".join(instructions_parts), items

    # ------------------------------------------------------------------
    # Non-streaming chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        instructions, input_items = self._convert_messages(messages)
        payload = self._build_payload(
            input_items=input_items,
            instructions=instructions,
            tools=tools,
            stream=False,
        )

        try:
            resp = await self._http.post(
                self._endpoint(),
                headers=self._headers(),
                json=payload,
                timeout=600,
            )
        except Exception as exc:
            return ProviderResponse(ok=False, error=str(exc))

        if resp.status_code >= 400:
            err_msg = _response_error_message(resp)
            if resp.status_code == 400 and _is_encrypted_content_error(err_msg):
                # [2026-05-06] Why: stale encrypted reasoning from saved
                # Responses output can make OpenAI reject the request. How: strip
                # encrypted_content from reasoning input items and resend this
                # payload once. Purpose: recover the conversation while keeping
                # the retry bounded and preserving summaries.
                log.warning("Encrypted content verification failed, retrying without encrypted reasoning")
                payload["input"] = _strip_encrypted_content(payload["input"])
                try:
                    resp = await self._http.post(
                        self._endpoint(),
                        headers=self._headers(),
                        json=payload,
                        timeout=600,
                    )
                except Exception as exc:
                    return ProviderResponse(ok=False, error=str(exc))
                if resp.status_code >= 400:
                    err_msg = _response_error_message(resp)
                    return ProviderResponse(ok=False, error=err_msg, status_code=resp.status_code)
            else:
                return ProviderResponse(ok=False, error=err_msg, status_code=resp.status_code)

        body = resp.json()
        result = self._parse_response(body)
        # [2026-05-01] 存储原始 output items（含 reasoning encrypted_content）到 provider_meta
        raw_output = body.get("output")
        if raw_output and isinstance(raw_output, list):
            result.provider_meta["raw_output"] = raw_output
        return result

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        instructions, input_items = self._convert_messages(messages)
        payload = self._build_payload(
            input_items=input_items,
            instructions=instructions,
            tools=tools,
            stream=True,
        )

        # [2026-05-06] Why: stream retries must reopen the whole SSE request
        # because a failed connection cannot be resumed. How: run at most two
        # attempts and rebuild accumulators per attempt. Purpose: keep fallback
        # bounded while avoiding polluted state from a failed first stream.
        for attempt in range(2):
            # -- Accumulators for the full response ----------------------
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            # tool_calls keyed by output index so we can accumulate arguments
            pending_calls: dict[int, dict[str, str]] = {}  # idx → {name, call_id, arguments}
            # [tool-stream 2026-05-19] 记录已发送 done 的 output index。
            # 原因：Responses API 可能同时给 arguments.done 与 output_item.done。
            # 做法：每次尝试内用集合去重生命周期结束事件。
            # 目的：前端只收到一次 tool_call_done，同时 final ProviderResponse 仍按 pending_calls 构建。
            done_indices: set[int] = set()
            usage: dict[str, int] = {}
            raw_output_items: list[dict[str, Any]] = []  # captured from response.completed
            retry_without_encrypted = False
            # ------------------------------------------------------------

            try:
                async with self._http.stream(
                    "POST",
                    self._endpoint(),
                    headers=self._headers(),
                    json=payload,
                    timeout=600,
                ) as stream:
                    if stream.status_code >= 400:
                        body = await stream.aread()
                        try:
                            data = json.loads(body)
                            err_msg = data.get("error", {}).get("message", body.decode()[:500])
                        except Exception:
                            err_msg = body.decode("utf-8", errors="replace")[:500]
                        if (
                            attempt == 0
                            and stream.status_code == 400
                            and _is_encrypted_content_error(err_msg)
                        ):
                            # [2026-05-06] Why: OpenAI can reject encrypted
                            # reasoning before sending any SSE event. How: strip
                            # encrypted_content and continue to the second and
                            # final attempt. Purpose: recover once without an
                            # unbounded retry loop.
                            log.warning(
                                "Encrypted content verification failed, retrying stream without encrypted reasoning"
                            )
                            payload["input"] = _strip_encrypted_content(payload["input"])
                            continue
                        return ProviderResponse(ok=False, error=err_msg, status_code=stream.status_code)
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")

                        # --- text deltas ---
                        if etype == "response.output_text.delta":
                            delta = event.get("delta", "")
                            if delta:
                                text_parts.append(delta)
                                if on_text:
                                    await on_text(delta)

                        # --- reasoning / thinking deltas ---
                        elif etype in (
                            "response.reasoning_text.delta",
                            "response.reasoning_summary_text.delta",
                        ):
                            delta = event.get("delta", "")
                            if delta:
                                thinking_parts.append(delta)
                                if on_thinking:
                                    await on_thinking(delta)

                        # --- new function_call item added ---
                        elif etype == "response.output_item.added":
                            item = event.get("item", {})
                            idx = int(event.get("output_index", 0) or 0)
                            if item.get("type") == "function_call":
                                pending_calls[idx] = {
                                    "name": item.get("name", ""),
                                    "call_id": item.get("call_id", ""),
                                    "arguments": "",
                                }
                                # [tool-stream 2026-05-19] Responses 的 output_item.added 是 function_call 起点。
                                # 原因：之前只创建 pending_calls，实时链路无法知道工具调用已开始。
                                # 做法：登记 pending_calls 后发送统一 tool_call_start。
                                # 目的：Responses provider 与 Chat Completions provider 暴露相同事件格式。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_start",
                                        "index": idx,
                                        "id": pending_calls[idx]["call_id"],
                                        "name": pending_calls[idx]["name"],
                                    })

                        # --- function_call arguments delta ---
                        elif etype == "response.function_call_arguments.delta":
                            idx = int(event.get("output_index", 0) or 0)
                            delta = event.get("delta", "")
                            if idx in pending_calls:
                                pending_calls[idx]["arguments"] += delta
                                # [tool-stream 2026-05-19] 转发 Responses 参数增量。
                                # 原因：response.function_call_arguments.delta 已是最小参数片段。
                                # 做法：保留原有字符串累积，同时把 delta 透传给 on_tool_delta。
                                # 目的：不改变最终 JSON 解析，又支持工具参数实时显示。
                                if delta and on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": idx,
                                        "delta": delta,
                                    })

                        # --- function_call arguments done ---
                        elif etype == "response.function_call_arguments.done":
                            idx = int(event.get("output_index", 0) or 0)
                            arguments = event.get("arguments")
                            if isinstance(arguments, str) and idx in pending_calls and not pending_calls[idx]["arguments"]:
                                pending_calls[idx]["arguments"] = arguments
                            # [tool-stream 2026-05-19] Responses 有显式 arguments.done 时发送 done。
                            # 原因：OpenAI Responses 相比 Chat Completions 多了参数完成事件。
                            # 做法：按 output_index 去重后发送 tool_call_done。
                            # 目的：让支持结束信号的 provider 暴露完整工具调用生命周期。
                            if idx in pending_calls and idx not in done_indices:
                                done_indices.add(idx)
                                if on_tool_delta:
                                    await on_tool_delta({"event": "tool_call_done", "index": idx})

                        # --- function_call output item done ---
                        elif etype == "response.output_item.done":
                            item = event.get("item", {})
                            idx = int(event.get("output_index", 0) or 0)
                            if item.get("type") == "function_call" and idx in pending_calls:
                                if item.get("name"):
                                    pending_calls[idx]["name"] = item.get("name", "")
                                if item.get("call_id"):
                                    pending_calls[idx]["call_id"] = item.get("call_id", "")
                                if isinstance(item.get("arguments"), str) and not pending_calls[idx]["arguments"]:
                                    pending_calls[idx]["arguments"] = item.get("arguments", "")
                                # [tool-stream 2026-05-19] output_item.done 作为兜底结束信号。
                                # 原因：不同 Responses 版本可能只发送 output_item.done。
                                # 做法：复用 done_indices 去重，避免与 arguments.done 重复。
                                # 目的：在事件形态变化时仍能通知前端工具调用结束。
                                if idx not in done_indices:
                                    done_indices.add(idx)
                                    if on_tool_delta:
                                        await on_tool_delta({"event": "tool_call_done", "index": idx})

                        # --- response completed: extract usage + raw output ---
                        elif etype in ("response.completed", "response.done"):
                            resp_obj = event.get("response", {})
                            u = resp_obj.get("usage", {})
                            if u:
                                usage = u
                            # [2026-05-01] 流式完成时捕获原始 output（含 reasoning encrypted_content）
                            _raw_out = resp_obj.get("output")
                            if _raw_out and isinstance(_raw_out, list):
                                raw_output_items = _raw_out

                        # --- error / failure ---
                        elif etype == "response.failed":
                            err = event.get("response", {}).get("error", {})
                            err_msg = err.get("message", str(err))
                            if attempt == 0 and _is_encrypted_content_error(err_msg):
                                # [2026-05-06] Why: OpenAI may report encrypted
                                # reasoning failure as response.failed after the
                                # SSE connection opens. How: close this stream,
                                # strip encrypted_content, and retry exactly once.
                                # Purpose: support both HTTP 400 and in-stream
                                # failure shapes for the same fallback.
                                log.warning(
                                    "Encrypted content verification failed, retrying stream without encrypted reasoning"
                                )
                                payload["input"] = _strip_encrypted_content(payload["input"])
                                retry_without_encrypted = True
                                break
                            return ProviderResponse(ok=False, error=f"API failed: {err_msg}")
                        elif etype == "error":
                            err_msg = event.get("message", str(event))
                            return ProviderResponse(ok=False, error=f"Stream error: {err_msg}")

            except Exception as exc:
                return ProviderResponse(ok=False, error=str(exc))

            if retry_without_encrypted:
                continue

            # -- Build final ProviderResponse ----------------------------
            # raw_output_items 在 response.completed 事件中捕获
            provider_meta: dict[str, Any] = {}
            if raw_output_items:
                provider_meta["raw_output"] = raw_output_items

            tool_calls: list[ToolCall] = []
            for _idx, pc in sorted(pending_calls.items()):
                tool_calls.append(ToolCall(
                    id=pc["call_id"],
                    name=pc["name"],
                    arguments=_safe_json_loads(pc["arguments"]),
                ))

            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            return ProviderResponse(
                ok=True,
                text="".join(text_parts) or None,
                tool_calls=tool_calls,
                reasoning="".join(thinking_parts) or None,
                usage={"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
                provider_meta=provider_meta,
            )

        return ProviderResponse(ok=False, error="Encrypted content retry failed before receiving a response")

    # ------------------------------------------------------------------
    # Payload builder (shared between chat and chat_stream)
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        *,
        input_items: list[dict[str, Any]],
        instructions: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "stream": stream,
            # Request encrypted reasoning tokens for multi-turn round-trip
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            # Responses API tools format: flatten the {type, function: {name, ...}} wrapper
            # chat/completions: [{type: "function", function: {name, description, parameters}}]
            # Responses API: [{type: "function", name, description, parameters}]
            resp_tools = []
            for t in tools:
                fn = t.get("function", {})
                if fn:
                    resp_tools.append({
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    })
                else:
                    resp_tools.append(t)
            payload["tools"] = resp_tools
        # 注入 reasoning 参数（如 {"effort": "medium", "summary": "auto"}）
        _reasoning = self._options.get("reasoning")
        if _reasoning and isinstance(_reasoning, dict):
            payload["reasoning"] = _reasoning
        return payload

    # ------------------------------------------------------------------
    # Non-streaming response parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(body: dict[str, Any]) -> ProviderResponse:
        """Parse a non-streaming Responses API JSON body into ProviderResponse."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in body.get("output", []):
            itype = item.get("type", "")

            if itype == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        text_parts.append(block.get("text", ""))

            elif itype == "reasoning":
                # Reasoning summary blocks
                for block in item.get("summary", []):
                    if block.get("type") == "summary_text":
                        thinking_parts.append(block.get("text", ""))

            elif itype == "function_call":
                tool_calls.append(ToolCall(
                    id=item.get("call_id", ""),
                    name=item.get("name", ""),
                    arguments=_safe_json_loads(item.get("arguments", "{}")),
                ))

        usage = body.get("usage", {})

        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        return ProviderResponse(
            ok=True,
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            reasoning="".join(thinking_parts) or None,
            usage={"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
        )


# ======================================================================
# Module-level helpers
# ======================================================================

def _extract_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a chat/completions message.

    Handles both string content and the list-of-blocks format.
    """
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # list-of-blocks: concatenate all text blocks
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _build_user_content(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a user message's content into Responses API content blocks.

    Handles:
      - Plain string → [{type: input_text, text: ...}]
      - List with text blocks → input_text
      - List with image_url blocks → input_image
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]

    result: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            result.append({"type": "input_text", "text": block})
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                result.append({"type": "input_text", "text": block.get("text", "")})
            elif btype == "image_url":
                # image_url block: {type: image_url, image_url: {url: "data:..."}}
                url = block.get("image_url", {}).get("url", "")
                result.append({"type": "input_image", "image_url": url})
    return result or [{"type": "input_text", "text": ""}]


def _arguments_to_json_string(raw: Any) -> str:
    """Return a JSON object string for a function_call.arguments field.

    [2026-05-01] Clonoth stores simplified tool arguments as dicts, while
    chat/completions stores function.arguments as a JSON string.  Responses
    input items require the string form, so this helper normalizes both shapes.
    """
    if isinstance(raw, str):
        return raw if raw.strip() else "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if raw is None:
        return "{}"
    return json.dumps(raw, ensure_ascii=False)


def _normalize_tool_call(tc: Any) -> dict[str, str] | None:
    """Normalize one tool call into Responses function_call fields.

    Supports the nested chat/completions shape and Clonoth's storage shape:
    ``{"id", "function": {"name", "arguments"}}`` and
    ``{"id", "name", "arguments"}`` respectively.
    """
    if not isinstance(tc, dict):
        return None

    call_id = str(tc.get("id") or "")
    fn = tc.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
        raw_args = fn.get("arguments", "{}")
    else:
        name = str(tc.get("name") or "").strip()
        raw_args = tc.get("arguments", {})

    if not name:
        return None
    return {"call_id": call_id, "name": name, "arguments": _arguments_to_json_string(raw_args)}


def _parse_native_tool_result(text: str) -> dict[str, str] | None:
    """Parse NativeToolFormatter's textual tool-result storage shape.

    The engine currently stores native tool results as user messages like
    ``Tool result for "name":\n...``.  Recognizing that exact format lets this
    provider rebuild a Responses ``function_call_output`` item.
    """
    match = _TOOL_RESULT_RE.match(text or "")
    if not match:
        return None
    return {"name": match.group("name"), "output": match.group("output")}


def _parse_engine_tool_result(text: str) -> dict[str, str] | None:
    """Parse engine-generated pseudo-tool result text.

    [2026-05-01] Some pseudo tools currently append bracketed result text such
    as ``[dispatch:target result: ...]`` instead of using format_tool_result().
    [2026-05-04] Dynamic dispatch tools may have names like dispatch:target, and
    the stored result marker now keeps that exact name. The Responses provider
    still has to pair that text with the preceding function_call, otherwise the
    next API request contains an unpaired tool call.
    """
    raw = text or ""
    match = _ENGINE_TOOL_RESULT_RE.match(raw)
    if match:
        return {"name": match.group("name"), "output": match.group("output")}
    if raw.startswith("[Context compression "):
        return {"name": "compact_context", "output": raw}
    return None


def _split_legacy_tool_records(text: str) -> tuple[str, list[dict[str, str]]]:
    """Split old fake-native tool-call records from ordinary user text.

    NativeToolFormatter used to expose tool calls as lines such as
    ``[Tool call history record: name was executed with args: {...}]``.  The
    provider rehydrates those lines into native function_call items and leaves
    all other lines as user text.
    """
    remaining: list[str] = []
    calls: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        match = _LEGACY_TOOL_RECORD_RE.match(line.strip())
        if not match:
            remaining.append(line)
            continue
        calls.append({
            "call_id": "",
            "name": match.group("name"),
            "arguments": match.group("arguments") or "{}",
        })
    return "\n".join(remaining), calls


def _pop_pending_call(pending_calls: list[dict[str, str]], name: str) -> dict[str, str] | None:
    """Pop the best matching pending function call for a tool result.

    Prefer matching by function name because Clonoth's textual tool-result
    storage does not carry a call ID.  Fall back to FIFO so older histories still
    make progress when names are unavailable or mismatched.
    """
    if name:
        for idx, pending in enumerate(pending_calls):
            if pending.get("name") == name:
                return pending_calls.pop(idx)
    if pending_calls:
        return pending_calls.pop(0)
    return None


def _flush_unpaired_function_calls(
    pending_calls: list[dict[str, str]],
    items: list[dict[str, Any]],
) -> None:
    """Emit synthetic outputs for pending calls that have no stored result.

    [2026-05-01] Terminal pseudo tools such as switch_node, and some historical
    compact paths, can leave an assistant function_call without a following
    stored result.  Responses requires every function_call to be paired with a
    function_call_output before later messages, so this keeps the request valid
    while making the missing result explicit to the model.
    """
    while pending_calls:
        pending = pending_calls.pop(0)
        items.append({
            "type": "function_call_output",
            "call_id": pending.get("call_id", ""),
            "output": _SYNTHETIC_TOOL_OUTPUT,
        })


def _safe_json_loads(s: str | dict) -> dict:
    """Parse a JSON string into a dict, returning {} on failure.

    If the input is already a dict (shouldn't happen from the API but
    defensive), return it directly.
    """
    if isinstance(s, dict):
        return s
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
