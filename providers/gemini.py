"""
Google Gemini native API provider.

Implements the BaseProvider interface for Gemini's generateContent /
streamGenerateContent endpoints. Converts OpenAI-format messages and tools
to Gemini's native format.

Created: 2026-05-01
Reason: Complete the provider trio (Anthropic, OpenAI, Gemini) so the engine
        can route to Gemini models natively without an OpenAI-compatible shim.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall

log = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Provider for Google Gemini (generativelanguage.googleapis.com)."""

    # [provider-registry 2026-05-03] 这是自动发现注册使用的 key。
    # 原因：engine 不再硬编码 GeminiProvider 分支；做法：类声明 provider_name；
    # 目的：registry 能把配置里的 "gemini" 映射回这个类。
    provider_name = "gemini"

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
        self._base_url = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
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

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages → Gemini contents + system instruction.

        Returns (system_text_or_None, gemini_contents_list).

        Conversion rules:
        - role=system  → extracted and merged into a single systemInstruction
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

        for msg in messages:
            role = msg.get("role", "")

            # ── system ──
            if role == "system":
                txt = _extract_text(msg)
                if txt:
                    system_parts.append(txt)
                continue

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
                if isinstance(_meta, dict):
                    _gem_meta = _meta.get("metadata", {}).get("gemini", {})
                    _raw_parts = _gem_meta.get("raw_parts")

                if _raw_parts and isinstance(_raw_parts, list):
                    # 有原始 parts：直接用，保留所有 thoughtSignature
                    # 同时从 functionCall parts 中提取 tc_id_to_name 映射
                    # 使用独立计数器避免多轮 ID 冲突
                    _fc_idx = 0
                    for rp in _raw_parts:
                        fc = rp.get("functionCall")
                        if fc:
                            _fc_idx += 1
                            tc_id_to_name[f"call_{_fc_idx}"] = fc.get("name", "")
                    contents.append({"role": "model", "parts": _raw_parts})
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
                # grouped in a single role="function" entry.  Merge into the
                # previous entry if it's also a function response.
                _fr_part = {"functionResponse": {"name": name, "response": resp_obj}}
                if contents and contents[-1].get("role") == "function":
                    contents[-1]["parts"].append(_fr_part)
                else:
                    contents.append({
                        "role": "function",
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
    ) -> dict[str, Any]:
        system_text, contents = self._convert_messages(messages)
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        gem_tools = self._convert_tools(tools)
        if gem_tools:
            body["tools"] = gem_tools
        # generationConfig: let model defaults govern most settings;
        # only set maxOutputTokens as a reasonable ceiling.
        gen_config: dict[str, Any] = {"maxOutputTokens": 65536}
        # 注入 thinkingConfig（如 {"thinkingBudget": 8192, "includeThoughts": true}）
        _thinking = self._options.get("thinking_config")
        if _thinking and isinstance(_thinking, dict):
            gen_config["thinkingConfig"] = _thinking
        body["generationConfig"] = gen_config
        return body

    # ── Non-streaming chat ───────────────────────────────────────────

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        body = self._build_body(messages, tools)
        try:
            resp = await self._http.post(
                self._endpoint(stream=False),
                headers=self._headers(),
                json=body,
                timeout=300,
            )
        except Exception as exc:
            return ProviderResponse(ok=False, error=f"Gemini request failed: {exc}")

        if resp.status_code != 200:
            return _error_from_response(resp)

        try:
            data = resp.json()
        except Exception:
            return ProviderResponse(
                ok=False, error="Gemini returned non-JSON response", status_code=resp.status_code,
            )

        return _parse_response(data)

    # ── Streaming chat ───────────────────────────────────────────────

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        body = self._build_body(messages, tools)
        try:
            req = self._http.build_request(
                "POST",
                self._endpoint(stream=True),
                headers=self._headers(),
                json=body,
                timeout=300,
            )
            resp = await self._http.send(req, stream=True)
        except Exception as exc:
            return ProviderResponse(ok=False, error=f"Gemini stream request failed: {exc}")

        if resp.status_code != 200:
            # Need to read the body for error details
            try:
                raw_body = await resp.aread()
                error_text = raw_body.decode("utf-8", errors="replace")
                try:
                    err_data = json.loads(error_text)
                    err_info = err_data.get("error", {})
                    msg = err_info.get("message", error_text[:500])
                except (json.JSONDecodeError, TypeError):
                    msg = error_text[:500]
            except Exception:
                msg = f"HTTP {resp.status_code}"
            finally:
                await resp.aclose()
            return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)

        # Accumulate streaming chunks
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] = {}
        inline_data_list: list[dict[str, Any]] = []
        tc_counter = 0  # for generating synthetic tool_call ids
        # [2026-05-01] 收集所有原始 parts，Gemini 3 要求整个 response 原样回传
        all_raw_parts: list[dict[str, Any]] = []

        try:
            # Gemini SSE: each line is "data: {json}\n\n"
            buf = ""
            async for raw_chunk in resp.aiter_text():
                buf += raw_chunk
                # Process complete SSE events in buffer
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:]  # strip "data: " prefix
                    if not json_str.strip():
                        continue
                    try:
                        chunk_data = json.loads(json_str)
                    except json.JSONDecodeError:
                        continue

                    # Extract parts from the first candidate
                    candidates = chunk_data.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        for part in parts:
                            # 收集所有原始 parts 用于 round-trip
                            all_raw_parts.append(part)
                            # Thinking / reasoning parts
                            if part.get("thought"):
                                t = part.get("text", "")
                                if t:
                                    thinking_parts.append(t)
                                    if on_thinking:
                                        await on_thinking(t)
                            # Regular text
                            elif "text" in part:
                                t = part["text"]
                                text_parts.append(t)
                                if on_text:
                                    await on_text(t)
                            # Function calls
                            elif "functionCall" in part:
                                fc = part["functionCall"]
                                tc_counter += 1
                                tc_id = f"call_{tc_counter}"
                                tc_index = tc_counter - 1
                                tc_name = fc.get("name", "")
                                tc_args_raw = fc.get("args") or {}
                                tc_args = tc_args_raw if isinstance(tc_args_raw, dict) else {"_raw": tc_args_raw}
                                # [tool-stream 2026-05-19] Gemini 当前返回完整 functionCall part，而非逐字符参数流。
                                # 原因：没有真实 args delta 时仍要接入统一实时管道。
                                # 做法：收到完整 functionCall 后合成 start、完整 args_delta、done 三个事件。
                                # 目的：前端和 supervisor 不需要为 Gemini 维护另一套非流式工具调用逻辑。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_start",
                                        "index": tc_index,
                                        "id": tc_id,
                                        "name": tc_name,
                                    })
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": tc_index,
                                        "delta": json.dumps(tc_args, ensure_ascii=False),
                                    })
                                    await on_tool_delta({"event": "tool_call_done", "index": tc_index})
                                tool_calls.append(ToolCall(
                                    id=tc_id,
                                    name=tc_name,
                                    arguments=tc_args,
                                ))
                            # Inline data (images etc generated by model)
                            elif "inlineData" in part:
                                inline_data_list.append(part["inlineData"])

                    # Usage metadata (typically present in the last chunk)
                    um = chunk_data.get("usageMetadata")
                    if um:
                        usage = _parse_usage(um)
        except Exception as exc:
            # Partial result is still better than nothing; log and continue
            log.warning("Gemini stream read error: %s", exc)
        finally:
            await resp.aclose()

        full_text = "".join(text_parts) or None
        reasoning = "".join(thinking_parts) or None

        # [2026-05-01] 存储所有原始 parts，Gemini 3 要求整个 response 原样回传
        provider_meta: dict[str, Any] = {}
        if all_raw_parts:
            provider_meta["raw_parts"] = all_raw_parts

        return ProviderResponse(
            ok=True,
            text=full_text,
            tool_calls=tool_calls,
            reasoning=reasoning,
            usage=usage or None,
            inline_data=inline_data_list,
            raw=None,
            provider_meta=provider_meta,
        )


# ── Module-level helpers ─────────────────────────────────────────────


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
        return [{"text": content}] if content else [{"text": ""}]
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
        return parts or [{"text": ""}]
    return [{"text": str(content)}]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Clean a JSON Schema for Gemini compatibility.

    Gemini's function-calling schema support is stricter than OpenAI's.
    Remove unsupported keys like 'additionalProperties', 'default', '$schema'.
    Recursively clean nested 'properties' and 'items'.
    """
    # Keys that Gemini's schema validator rejects
    UNSUPPORTED = {"additionalProperties", "default", "$schema", "$id", "$ref", "$defs", "definitions"}
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) if isinstance(pv, dict) else pv for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
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
    return cleaned


def _parse_usage(um: dict[str, Any]) -> dict[str, int]:
    """Convert Gemini usageMetadata to the standard usage dict format."""
    return {
        "prompt_tokens": um.get("promptTokenCount", 0),
        "completion_tokens": um.get("candidatesTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", 0),
    }


def _parse_response(data: dict[str, Any]) -> ProviderResponse:
    """Parse a non-streaming Gemini generateContent response into ProviderResponse."""
    # Check for API-level errors
    if "error" in data:
        err = data["error"]
        return ProviderResponse(
            ok=False,
            error=err.get("message", str(err)),
            status_code=err.get("code"),
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
        provider_meta["raw_parts"] = parts

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
        err = data.get("error", {})
        msg = err.get("message", resp.text[:500])
    except Exception:
        msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
    return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)
