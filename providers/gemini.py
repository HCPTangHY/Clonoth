"""
Shared Gemini native API implementation for explicit backend providers.

Implements the BaseProvider interface for Gemini's generateContent /
streamGenerateContent endpoints. Converts OpenAI-format messages and tools
to Gemini's native format.

Created: 2026-05-01
Reason: Complete the provider trio (Anthropic, OpenAI, Gemini) so the engine
        can route to Gemini models natively without an OpenAI-compatible shim.
"""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall

log = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Shared transport; select GeminiVertexProvider or GeminiAIStudioProvider."""

    # An empty registry key keeps this implementation out of channel selection.
    provider_name = ""
    default_base_url = ""
    is_vertex = False
    default_max_tokens = 65535
    default_thought_summary = True

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
        if not self.provider_name:
            raise TypeError("Select gemini-vertex or gemini-aistudio; GeminiProvider is a shared implementation")
        resolved_base_url = (base_url or "").strip() or self.default_base_url
        if not resolved_base_url:
            raise ValueError(f"{self.provider_name} requires an explicit base_url")
        super().__init__(model=model, name=self.provider_name)
        self._http = http
        self._api_key = api_key
        self._base_url = resolved_base_url.rstrip("/")
        self._options = provider_options or {}

    # ── URL helpers ──────────────────────────────────────────────────

    def _endpoint(self, stream: bool = False) -> str:
        """Build the request URL for generateContent / streamGenerateContent."""
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self._base_url}/v1beta/models/{self.model}:{action}"

    def _headers(self) -> dict[str, str]:
        """Return auth headers.

        Official googleapis.com → x-goog-api-key header.
        Reverse-proxy (any other base_url) → standard Bearer token.
        """
        h: dict[str, str] = {"Content-Type": "application/json"}
        if "googleapis.com" in self._base_url:
            h["x-goog-api-key"] = self._api_key
        else:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    # ── Message conversion ───────────────────────────────────────────

    @classmethod
    def _convert_messages(
        cls,
        messages: list[dict[str, Any]],
        *,
        strip_thoughts: bool = False,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages → Gemini contents + system instruction.

        Returns (system_text_or_None, gemini_contents_list).

        Conversion rules:
        - leading system/developer → systemInstruction; later ones stay as user contents
        - role=user    → role="user", text/image parts
        - role=assistant → role="model", text + optional functionCall parts
        - role=tool     → role="function", functionResponse parts
                          (name is resolved from prior assistant tool_calls via
                           a tool_call_id→name mapping built on the fly)
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        # Map tool_call_id → function name so we can fill functionResponse.name
        # when processing role=tool messages (which only carry tool_call_id).
        tc_id_to_name: dict[str, str] = {}

        seen_non_system = False
        for msg in messages:
            role = msg.get("role", "")

            # Keep only the leading static instructions in the cache prefix.
            if role in ("system", "developer"):
                if not seen_non_system:
                    txt = _extract_text(msg)
                    if txt:
                        system_parts.append(txt)
                    continue
                role = "user"

            seen_non_system = True
            # ── user ──
            if role == "user":
                contents.append({"role": "user", "parts": _user_parts(msg)})
                continue

            # ── assistant / model ──
            if role == "assistant":
                # [2026-05-01] Gemini 3 文档要求 "Return the entire response with
                # all parts back to the model"。任何 part（text/functionCall/thought）
                # 都可能带 thoughtSignature，必须原样回传，不能合并或重建。
                # 优先使用 provider_meta 中保存的原始 parts。
                _meta = msg.get("_meta")
                _raw_parts = None
                _gem_meta: dict[str, Any] = {}
                if isinstance(_meta, dict):
                    metadata = _meta.get("metadata")
                    if isinstance(metadata, dict):
                        # Replay only this explicit backend's parts, never the retired
                        # "gemini" namespace or another backend's thought signatures.
                        saved = metadata.get(cls.provider_name)
                        if isinstance(saved, dict):
                            _gem_meta = saved
                            _raw_parts = saved.get("raw_parts")

                if _raw_parts and isinstance(_raw_parts, list):
                    # Complete responses remain exact. Only known stream frame
                    # boundaries permit coalescing in this request-local copy.
                    parts = _coalesce_stream_parts(_raw_parts, _gem_meta.get("stream_part_counts"))
                    if strip_thoughts:
                        parts = _strip_thought_signatures(parts)
                    for index, part in enumerate(p for p in parts if "functionCall" in p):
                        tc_id_to_name[f"call_{index + 1}"] = part["functionCall"].get("name", "")
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            tc_id_to_name[tc["id"]] = fn.get("name") or tc.get("name", "")
                    if parts:
                        contents.append({"role": "model", "parts": parts})
                    elif _extract_text(msg):
                        contents.append({"role": "model", "parts": [{"text": _extract_text(msg)}]})
                else:
                    # 无原始 parts（旧消息或非 Gemini provider 产生的历史）：从 OpenAI 格式重建
                    parts: list[dict[str, Any]] = []
                    txt = _extract_text(msg)
                    if txt:
                        parts.append({"text": txt})
                    for tc in msg.get("tool_calls") or []:
                        # Support both OpenAI API format {function: {name, arguments}}
                        # and Clonoth simplified storage format {name, arguments}
                        fn = tc.get("function") or {}
                        name = fn.get("name", "") or tc.get("name", "")
                        tc_id = tc.get("id", "")
                        if tc_id:
                            tc_id_to_name[tc_id] = name
                        raw_args = fn.get("arguments", tc.get("arguments", {}))
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except (json.JSONDecodeError, TypeError):
                                raw_args = {}
                        parts.append({"functionCall": {"name": name, "args": raw_args}})
                    if parts:
                        # Synthesized calls have no original signatures. The
                        # documented compatibility marker is not a borrowed signature.
                        _ensure_fc_thought_signature(parts)
                        contents.append({"role": "model", "parts": parts})
                continue

            # ── tool result ──
            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                # Resolve function name: prefer msg["name"] if present,
                # otherwise look up from the mapping we built.
                name = msg.get("name") or tc_id_to_name.get(tc_id, "unknown")
                raw_content = msg.get("content", "")
                # Gemini expects response to be an object, not a bare string.
                # Try to parse as JSON; fall back to wrapping in {"result": ...}.
                try:
                    resp_obj = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                    if not isinstance(resp_obj, dict):
                        resp_obj = {"result": resp_obj}
                except (json.JSONDecodeError, TypeError):
                    resp_obj = {"result": raw_content}
                # Gemini requires all function responses for the same turn to be
                # grouped in a single role="user" entry with functionResponse parts.
                # (2026-08-19: changed from role="function" to role="user" per current
                # Gemini API spec; the old "function" role is no longer accepted.)
                _fr_part = {"functionResponse": {"name": name, "response": resp_obj}}
                if contents and contents[-1].get("role") == "user" and contents[-1]["parts"] and "functionResponse" in contents[-1]["parts"][-1]:
                    contents[-1]["parts"].append(_fr_part)
                else:
                    contents.append({
                        "role": "user",
                        "parts": [_fr_part],
                    })
                continue

            # ── fallback: treat unknown roles as user ──
            txt = _extract_text(msg)
            if txt:
                contents.append({"role": "user", "parts": [{"text": txt}]})

        system_text = "\n\n".join(system_parts) if system_parts else None
        return system_text, contents

    # ── Tool declaration conversion ──────────────────────────────────

    @staticmethod
    def _convert_tools(
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Convert OpenAI tool declarations → Gemini functionDeclarations.

        OpenAI format:
          [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        Gemini format:
          [{"functionDeclarations": [{"name": ..., "description": ..., "parameters": ...}]}]
        """
        if not tools:
            return None
        decls: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function") or t  # tolerate flat format too
            decl: dict[str, Any] = {"name": fn.get("name", "")}
            if fn.get("description"):
                decl["description"] = fn["description"]
            if fn.get("parameters"):
                decl["parameters"] = _clean_schema(fn["parameters"])
            decls.append(decl)
        return [{"functionDeclarations": decls}] if decls else None

    # ── Build request body ───────────────────────────────────────────

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        strip_thoughts: bool = False,
        strip_system_role: bool = False,
    ) -> dict[str, Any]:
        system_text, contents = self._convert_messages(messages, strip_thoughts=strip_thoughts)
        # Request-only channel compatibility tails, not recorded execution results.
        # This matches the selected upstream policy, not a general Google API guarantee.
        if not contents:
            contents = [{"role": "user", "parts": [{"thought": True, "text": " "}]}]
        elif contents[-1].get("role") == "model":
            contents.append({"role": "user", "parts": [{"thought": True, "text": " "}]})
        elif self.is_vertex and not strip_system_role:
            contents.append({"role": "system", "parts": [{"text": " "}]})
        elif not self.is_vertex:
            contents.append({
                "role": "model",
                "parts": [{"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": " "}}],
            })
            contents.append({"role": "user", "parts": [{"thought": True, "text": " "}]})
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        gem_tools = self._convert_tools(tools)
        if gem_tools:
            body["tools"] = gem_tools

        custom_safety = self._options.get("safety_settings")
        if isinstance(custom_safety, list):
            body["safetySettings"] = deepcopy(custom_safety)
        else:
            model_lower = self.model.lower()
            threshold = "OFF" if any(name in model_lower for name in ("gemini-2", "gemini-3", "gemini-4", "image")) else "BLOCK_NONE"
            categories = [
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_CIVIC_INTEGRITY", "HARM_CATEGORY_JAILBREAK",
            ]
            if self.is_vertex:
                categories.extend([
                    "HARM_CATEGORY_IMAGE_HATE", "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_IMAGE_HARASSMENT", "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT",
                ])
            body["safetySettings"] = [{"category": category, "threshold": threshold} for category in categories]

        raw_max_tokens = self._options.get("max_tokens")
        if raw_max_tokens is None:
            raw_max_tokens = self._options.get("max_output_tokens")
        try:
            max_tokens = int(raw_max_tokens) if raw_max_tokens is not None else self.default_max_tokens
        except (TypeError, ValueError, OverflowError):
            max_tokens = self.default_max_tokens
        gen_config: dict[str, Any] = {"maxOutputTokens": max_tokens if max_tokens > 0 else self.default_max_tokens}
        for option, field in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
            if self._options.get(option) is not None:
                gen_config[field] = self._options[option]

        # Preserve production precedence: effort/summary aliases override the
        # low-level thinking config; budget-based thinking is no longer emitted.
        raw_thinking = self._options.get("thinking_config")
        thinking = deepcopy(raw_thinking) if isinstance(raw_thinking, dict) else {}
        thinking.pop("thinkingBudget", None)
        effort = self._options.get("reasoning_effort")
        if effort is not None or "thinkingLevel" in thinking:
            level = str(effort if effort is not None else thinking.get("thinkingLevel") or "").strip().upper()
            if level and level != "NONE":
                thinking["thinkingLevel"] = level
            else:
                thinking.pop("thinkingLevel", None)
        summary = self._options.get("thought_summary", self.default_thought_summary)
        enabled = summary.strip().lower() not in ("false", "0", "no", "off", "none") if isinstance(summary, str) else bool(summary)
        if enabled:
            thinking["includeThoughts"] = True
        elif "thought_summary" in self._options:
            thinking.pop("includeThoughts", None)
        if thinking:
            gen_config["thinkingConfig"] = thinking
        body["generationConfig"] = gen_config
        return body

    def prepare_history(
        self, messages: list[dict[str, Any]], *, tool_mode: str = "native",
    ) -> list[dict[str, Any]]:
        from .tool_format import (
            FakeNativeToolFormatter, _assistant_tool_calls,
            build_llm_messages, create_tool_formatter, prepare_raw_history,
        )

        formatter = create_tool_formatter(tool_mode)
        if formatter.mode == "json":
            return build_llm_messages(messages, formatter)

        history = prepare_raw_history(messages)
        text_formatter = FakeNativeToolFormatter()
        text_result_ids: set[str] = set()
        for index, message in enumerate(history):
            if message.get("role") == "tool":
                if message.get("tool_call_id") in text_result_ids:
                    history[index] = text_formatter.message_to_llm(message)
                continue
            text_result_ids.clear()
            if message.get("role") != "assistant":
                continue
            meta = message.get("_meta")
            metadata = meta.get("metadata") if isinstance(meta, dict) else None
            saved = metadata.get(self.provider_name) if isinstance(metadata, dict) else None
            parts = saved.get("raw_parts") if isinstance(saved, dict) else None
            if not isinstance(parts, list) or not parts:
                continue

            calls = _assistant_tool_calls(message)
            raw_calls = [part["functionCall"] for part in parts if isinstance(part, dict) and "functionCall" in part]
            valid_parts = all(isinstance(part, dict) for part in parts)
            valid_calls = all(isinstance(call, dict) for call in raw_calls)
            same_call_ids = meta.get("tool_call_ids", []) == [call["id"] for call in calls]
            # JSON comparison distinguishes booleans from numbers, unlike Python equality.
            if valid_parts and valid_calls and same_call_ids and json.dumps([
                (call.get("name", ""), call.get("args") or {}) for call in raw_calls
            ], sort_keys=True) == json.dumps([
                (call["name"], call["arguments"]) for call in calls
            ], sort_keys=True):
                continue

            # ponytail: incomplete signed turns are retained as text; native replay
            # requires complete matching calls. Never move or invent a signature:
            # parallel calls may share a signature attached only to the first part.
            text_result_ids.update(call["id"] for call in calls)
            history[index] = text_formatter.message_to_llm({
                "role": "assistant", "content": _extract_text(message), "tool_calls": calls,
            })
        return history

    def build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, tool_mode: str = "native", stream: bool = False,
        strip_thoughts: bool = False, strip_system_role: bool = False,
    ) -> dict[str, Any]:
        return self._build_body(
            self.prepare_history(messages, tool_mode=tool_mode), tools,
            strip_thoughts=strip_thoughts, strip_system_role=strip_system_role,
        )

    async def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
        *, tool_mode: str, stream: bool,
    ) -> httpx.Response | ProviderResponse:
        """Send one request and at most one request-local compatibility retry."""
        strip_thoughts = False
        strip_system_role = False
        for attempt in range(2):
            body = self.build_payload(
                messages, tools, tool_mode=tool_mode, stream=stream,
                strip_thoughts=strip_thoughts, strip_system_role=strip_system_role,
            )
            try:
                if stream:
                    request = self._http.build_request(
                        "POST", self._endpoint(stream=True), headers=self._headers(), json=body, timeout=300,
                    )
                    response = await self._http.send(request, stream=True)
                else:
                    response = await self._http.post(
                        self._endpoint(), headers=self._headers(), json=body, timeout=300,
                    )
            except Exception as exc:
                return ProviderResponse(ok=False, error=f"Gemini request failed: {exc}")
            if response.status_code == 200:
                return response
            if stream:
                error = ProviderResponse(
                    ok=False, error=await _read_stream_error(response), status_code=response.status_code,
                )
            else:
                error = _error_from_response(response)
                await response.aclose()
            strip_thoughts = _is_thought_signature_error(error.error)
            strip_system_role = self.is_vertex and _is_system_role_error(error.error)
            if attempt or response.status_code != 400 or not (strip_thoughts or strip_system_role):
                return error
            log.warning(
                "Gemini 400 (%s); retrying once with strip_thoughts=%s, strip_system_role=%s",
                error.error, strip_thoughts, strip_system_role,
            )
        raise AssertionError("bounded Gemini request loop exhausted")

    # ── Non-streaming chat ───────────────────────────────────────────

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_mode: str = "native",
    ) -> ProviderResponse:
        response = await self._request(messages, tools, tool_mode=tool_mode, stream=False)
        if isinstance(response, ProviderResponse):
            return response
        try:
            result = _parse_response(response.json())
            if result.ok:
                result.usage = self._usage((result.raw or {}).get("usageMetadata") or {})
            return result
        except (ValueError, TypeError, AttributeError) as exc:
            return ProviderResponse(ok=False, error=f"Gemini returned an invalid response: {exc}", status_code=response.status_code)
        finally:
            await response.aclose()

    def _usage(self, metadata: dict[str, Any]) -> dict[str, int]:
        # ponytail: the duplicate prompt-counter workaround is specific to the
        # observed Vertex argon gateway; other models retain additive accounting.
        return _parse_usage(
            metadata, deduplicate_tool_prompt=self.is_vertex and "gemini-4-argon" in self.model.lower(),
        )

    # ── Streaming chat ───────────────────────────────────────────────

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_mode: str = "native",
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        response = await self._request(messages, tools, tool_mode=tool_mode, stream=True)
        if isinstance(response, ProviderResponse):
            return response

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_usage: dict[str, Any] = {}
        inline_data: list[dict[str, Any]] = []
        raw_parts: list[dict[str, Any]] = []
        part_counts: list[int] = []
        stream_error: str | None = None
        error_status = response.status_code

        async def consume(chunk: Any) -> None:
            nonlocal error_status
            if not isinstance(chunk, dict):
                raise ValueError("Gemini stream chunk is not an object")
            if "error" in chunk:
                error = chunk["error"]
                if isinstance(error, dict):
                    error_status = error.get("code") or response.status_code
                    raise ValueError(str(error.get("message") or error))
                raise ValueError(str(error))
            feedback = chunk.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                raise ValueError(f"Blocked by safety filter: {feedback['blockReason']}")
            usage = chunk.get("usageMetadata")
            if isinstance(usage, dict):
                raw_usage.update(usage)
            candidates = chunk.get("candidates") or []
            parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
            if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
                raise ValueError("Gemini stream contains invalid parts")
            part_counts.append(len(parts))
            raw_parts.extend(deepcopy(parts))
            for part in parts:
                text = part.get("text")
                if isinstance(text, str) and text:
                    if part.get("thought"):
                        thinking_parts.append(text)
                        if on_thinking:
                            await on_thinking(text)
                    else:
                        text_parts.append(text)
                        if on_text:
                            await on_text(text)
                if "functionCall" in part:
                    call = part["functionCall"]
                    if not isinstance(call, dict):
                        raise ValueError("Gemini stream contains an invalid functionCall")
                    index = len(tool_calls)
                    arguments = call.get("args") or {}
                    if not isinstance(arguments, dict):
                        arguments = {"_raw": arguments}
                    parsed = ToolCall(id=f"call_{index + 1}", name=call.get("name", ""), arguments=arguments)
                    tool_calls.append(parsed)
                    if on_tool_delta:
                        await on_tool_delta({"event": "tool_call_start", "index": index, "id": parsed.id, "name": parsed.name})
                        await on_tool_delta({"event": "tool_call_args_delta", "index": index, "delta": json.dumps(arguments, ensure_ascii=False)})
                        await on_tool_delta({"event": "tool_call_done", "index": index})
                if isinstance(part.get("inlineData"), dict):
                    inline_data.append(part["inlineData"])

        try:
            saw_sse = False
            json_lines: list[str] = []
            # aiter_lines handles CRLF, split UTF-8 and the final unterminated line.
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    saw_sse = True
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if data:
                        await consume(json.loads(data))
                elif not saw_sse and line and not line.startswith((":", "event:", "id:", "retry:")):
                    json_lines.append(line)
            if not saw_sse:
                document = json.loads("\n".join(json_lines))
                chunks = document if isinstance(document, list) else [document]
                for chunk in chunks:
                    await consume(chunk)
            if not raw_parts:
                stream_error = "Gemini stream returned no candidate parts"
        except Exception as exc:
            stream_error = f"Gemini stream failed: {exc}"
            log.warning("%s", stream_error)
        finally:
            await response.aclose()

        # Raw fragments remain intact. Their frame sizes allow request-local
        # assembly without treating a complete response as a sequence of deltas.
        provider_meta = {"raw_parts": raw_parts, "stream_part_counts": part_counts} if raw_parts else {}
        return ProviderResponse(
            ok=stream_error is None, error=stream_error,
            status_code=error_status, text="".join(text_parts) or None,
            reasoning="".join(thinking_parts) or None,
            tool_calls=tool_calls if stream_error is None else [],
            usage=self._usage(raw_usage) if raw_usage else None,
            inline_data=inline_data, provider_meta=provider_meta,
        )


# ── Module-level helpers ─────────────────────────────────────────────


def _coalesce_stream_parts(parts: list[dict[str, Any]], counts: Any) -> list[dict[str, Any]]:
    """Assemble text deltas only across unambiguous single-part stream frames.

    Complete responses and legacy records without frame boundaries are copied
    unchanged. Every signed part, multi-part frame or unknown frame breaks a
    run. A signed part is never merged with an unsigned part, even in a stream.
    """
    copied = deepcopy(parts)
    if not isinstance(counts, list) or not counts or any(type(count) is not int or count < 0 for count in counts):
        return copied
    if sum(counts) != len(parts):
        return copied
    assembled: list[dict[str, Any]] = []
    position = 0
    previous_delta = False
    for count in counts:
        frame = copied[position:position + count]
        position += count
        part = frame[0] if count == 1 else {}
        # ponytail: without explicit boundaries, only consecutive single-part
        # text frames are deltas. Preserve every ambiguous frame rather than
        # borrowing or reattaching a signature from another part.
        delta = (
            count == 1 and set(part) <= {"text", "thought"}
            and (isinstance(part.get("text"), str) or part.get("thought") is True)
        )
        if delta and previous_delta and bool(assembled[-1].get("thought")) == bool(part.get("thought")):
            assembled[-1]["text"] = assembled[-1].get("text", "") + part.get("text", "")
        else:
            assembled.extend(frame)
        previous_delta = delta
    return assembled


_SKIP_THOUGHT_SIG = "skip_thought_signature_validator"


def _ensure_fc_thought_signature(parts: list[dict[str, Any]]) -> None:
    """Add the API's compatibility marker only when no signature is available."""
    if not any("thoughtSignature" in part for part in parts):
        for part in parts:
            if "functionCall" in part:
                part["thoughtSignature"] = _SKIP_THOUGHT_SIG
                return


def _strip_thought_signatures(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make the bounded signature-retry projection, without changing raw data."""
    cleaned = []
    for part in parts:
        if part.get("thought"):
            continue
        copied = {key: deepcopy(value) for key, value in part.items() if key != "thoughtSignature"}
        if copied:
            cleaned.append(copied)
    _ensure_fc_thought_signature(cleaned)
    return cleaned


def _is_thought_signature_error(message: str | None) -> bool:
    lower = (message or "").lower()
    return any(marker in lower for marker in ("thought signature", "thoughtsignature", "thought_signature"))


def _is_system_role_error(message: str | None) -> bool:
    lower = (message or "").lower()
    return "role 'system' is not supported" in lower or 'role "system" is not supported' in lower


async def _read_stream_error(response: httpx.Response) -> str:
    """Read an HTTP error and always close the first stream before any retry."""
    try:
        await response.aread()
        return _error_from_response(response).error or f"HTTP {response.status_code}"
    except Exception:
        return f"HTTP {response.status_code}"
    finally:
        await response.aclose()


def _extract_text(msg: dict[str, Any]) -> str | None:
    """Extract text from a message's 'content' field.

    Handles both plain-string content and the OpenAI multi-part
    [{"type": "text", "text": "..."}] format.
    """
    content = msg.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    # Multi-part array (OpenAI vision format)
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if p.get("type") == "text"]
        joined = "".join(texts)
        return joined or None
    return None


def _user_parts(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Gemini 'parts' array for a user message.

    Handles plain text and OpenAI multi-modal content arrays
    (text + image_url with base64 data URIs).
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return [{"text": content}] if content else [{"text": " "}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if item.get("type") == "text":
                parts.append({"text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                # Handle base64 data URIs: "data:<mime>;base64,<data>"
                if url.startswith("data:"):
                    # Parse "data:image/png;base64,iVBOR..."
                    header, _, b64data = url.partition(",")
                    mime = header.split(":", 1)[-1].split(";", 1)[0]
                    parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                else:
                    # External URL — Gemini supports fileData for GCS URIs;
                    # for http(s) URLs, we'd need to download. For now, pass
                    # as text fallback since Gemini doesn't accept arbitrary URLs.
                    parts.append({"text": f"[Image: {url}]"})
        return parts or [{"text": " "}]
    return [{"text": str(content)}]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Clean a JSON Schema for Gemini compatibility.

    Gemini's function-calling schema support is stricter than OpenAI's.
    Strategy:
    - Drop unsupported keywords outright.
    - Convert exclusiveMinimum/exclusiveMaximum to minimum/maximum (widen
      exclusive bound to inclusive, keep the tighter value when both exist).
    - Recursively clean nested 'properties', 'items', and composition arrays.
    """
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        types = list(dict.fromkeys(kind for kind in raw_type if isinstance(kind, str)))
        non_null = [kind for kind in types if kind != "null"]
        schema = dict(schema)
        if not non_null:
            schema["type"] = "null" if "null" in types else "string"
        elif len(non_null) == 1:
            schema["type"] = non_null[0]
            if "null" in types:
                schema["nullable"] = True
        else:
            schema.pop("type")
            # Each alternative retains all existing constraints, including an
            # existing anyOf, instead of silently selecting the first type.
            return {"anyOf": [_clean_schema(dict(schema, type=kind)) for kind in types]}

    # Keywords Gemini's OpenAPI-3.0 subset does not accept at all
    DROP = {"additionalProperties", "default", "$schema", "$id", "$ref",
            "$defs", "definitions", "multipleOf", "uniqueItems",
            "patternProperties", "propertyNames", "const",
            "oneOf", "allOf", "not", "if", "then", "else"}
    # exclusiveMinimum/exclusiveMaximum: convert, not drop
    exc_min = schema.get("exclusiveMinimum")
    exc_max = schema.get("exclusiveMaximum")
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in DROP or k in ("exclusiveMinimum", "exclusiveMaximum"):
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) if isinstance(pv, dict) else pv for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k == "items" and isinstance(v, list):
            # ponytail: Gemini's native schema cannot express positional tuple
            # constraints. Keep every item alternative; exact tuple semantics
            # require the parametersJsonSchema transport in a future change.
            alternatives = [_clean_schema(item) for item in v if isinstance(item, dict)]
            cleaned[k] = {"anyOf": alternatives} if alternatives else {}
            log.warning("Gemini tuple schema uses an item union; positional constraints are not supported")
        elif k in ("anyOf",) and isinstance(v, list):
            cleaned[k] = [_clean_schema(item) if isinstance(item, dict) else item for item in v]
        elif k == "enum" and isinstance(v, list):
            # [2026-05-06] Gemini rejects empty enum members in function schemas.
            # Why: stale or dynamically generated tools can contain target enum values
            # such as "" for internal runtime semantics, which causes Gemini to reject
            # the whole GenerateContentRequest before the model runs. How: remove None
            # and blank-string members, de-duplicate the remaining values while keeping
            # their order, and omit enum entirely if nothing valid remains. Purpose: keep
            # Gemini requests valid without changing the runtime handlers that may still
            # accept an omitted or empty argument value.
            enum_values: list[Any] = []
            for item in v:
                if item is None:
                    continue
                if isinstance(item, str) and not item.strip():
                    continue
                if item not in enum_values:
                    enum_values.append(item)
            if enum_values:
                cleaned[k] = enum_values
        else:
            cleaned[k] = v
    # Widen exclusive bounds to inclusive (Gemini has no exclusive bound)
    if isinstance(exc_min, (int, float)):
        cur = cleaned.get("minimum")
        cleaned["minimum"] = max(cur, exc_min) if isinstance(cur, (int, float)) else exc_min
    if isinstance(exc_max, (int, float)):
        cur = cleaned.get("maximum")
        cleaned["maximum"] = min(cur, exc_max) if isinstance(cur, (int, float)) else exc_max
    return cleaned


def _parse_usage(um: dict[str, Any], *, deduplicate_tool_prompt: bool = False) -> dict[str, int]:
    """Include tool-use prompts and thinking tokens in normalized usage."""
    def count(key: str) -> int:
        try:
            return max(0, int(um.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    prompt = count("promptTokenCount")
    tool_prompt = count("toolUsePromptTokenCount")
    cached = count("cachedContentTokenCount")
    if deduplicate_tool_prompt and min(prompt, tool_prompt) > 50_000 and min(prompt, tool_prompt) >= 0.5 * max(prompt, tool_prompt):
        prompt = max(prompt, tool_prompt, cached)
    else:
        prompt += tool_prompt
    completion = count("candidatesTokenCount") + count("thoughtsTokenCount")
    result = {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": count("totalTokenCount") or prompt + completion,
    }
    if cached:
        result["cached_prompt_tokens"] = cached
    return result


def _parse_response(data: dict[str, Any]) -> ProviderResponse:
    """Parse a non-streaming Gemini generateContent response into ProviderResponse."""
    if not isinstance(data, dict):
        return ProviderResponse(ok=False, error="Gemini returned a non-object response")
    # Check for API-level errors
    if "error" in data:
        err = data["error"]
        return ProviderResponse(
            ok=False,
            error=str(err.get("message") or err) if isinstance(err, dict) else str(err),
            status_code=err.get("code") if isinstance(err, dict) else None,
        )

    candidates = data.get("candidates") or []
    if not candidates:
        # Could be a safety block or empty response
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            return ProviderResponse(ok=False, error=f"Blocked by safety filter: {block_reason}")
        return ProviderResponse(ok=False, error="Gemini returned no candidates")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
        return ProviderResponse(ok=False, error="Gemini returned invalid parts")

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    inline_data_list: list[dict[str, Any]] = []
    tc_counter = 0

    for part in parts:
        if part.get("thought"):
            t = part.get("text", "")
            if t:
                thinking_parts.append(t)
        elif "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tc_counter += 1
            tool_calls.append(ToolCall(
                id=f"call_{tc_counter}",
                name=fc.get("name", ""),
                arguments=fc.get("args") or {},
            ))
        elif "inlineData" in part:
            inline_data_list.append(part["inlineData"])

    usage = _parse_usage(data.get("usageMetadata") or {})
    full_text = "".join(text_parts) or None
    reasoning = "".join(thinking_parts) or None

    # [2026-05-01] 存储整个 response 的原始 parts 到 provider_meta，
    # Gemini 3 要求 "Return the entire response with all parts back"，
    # 任何 part 都可能带 thoughtSignature，不能只存 thought parts
    provider_meta: dict[str, Any] = {}
    if parts:
        provider_meta["raw_parts"] = deepcopy(parts)

    return ProviderResponse(
        ok=True,
        text=full_text,
        tool_calls=tool_calls,
        reasoning=reasoning,
        usage=usage or None,
        inline_data=inline_data_list,
        raw=data,
        provider_meta=provider_meta,
    )


def _error_from_response(resp: httpx.Response) -> ProviderResponse:
    """Build an error ProviderResponse from a failed httpx.Response."""
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", {}) if isinstance(data, dict) else {}
        msg = str(err.get("message") or resp.text[:500]) if isinstance(err, dict) else str(err)
    except Exception:
        msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
    return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)
