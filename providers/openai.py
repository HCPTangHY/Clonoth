from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall


def _parse_first_json_object(raw: str) -> dict | None:
    """Fallback: parse the first JSON object from concatenated '{...}{...}' strings."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(raw)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _normalize_base_url(base_url: str | None) -> str:
    """Normalize base_url for OpenAI-compatible APIs.

    Accepts:
    - https://api.openai.com
    - https://api.openai.com/v1
    - https://proxy.example.com/v1
    - https://api.deepseek.com

    Returns a base URL with trailing slash stripped. Does NOT force /v1 suffix
    — the caller writes the full path they intend, provider appends
    /chat/completions directly.
    """

    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"

    # 缺少协议前缀时补上 https://
    if base and not base.startswith("http://") and not base.startswith("https://"):
        base = "https://" + base

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

def _extract_usage(data: Any) -> dict[str, int] | None:
    """Extract token usage dict from an OpenAI response/chunk."""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        if isinstance(val, int):
            result[key] = val
    return result if result else None



class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider implemented with raw HTTP (no SDK).

    This adapter targets the Chat Completions API:
        POST {base_url}/chat/completions

    It supports tool calling via the `tools` field.
    """

    # [provider-registry 2026-05-03] 这是自动发现注册使用的 key。
    # 原因：engine 不再硬编码 OpenAIProvider 分支；做法：类声明 provider_name；
    # 目的：registry 能把配置里的 "openai" 映射回这个类。
    provider_name = "openai"

    # ------------------------------------------------------------------
    #  L3 Provider 层：消息预处理
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L3: 将 L2 输出转换为 OpenAI API 最终格式。

        【三层管道 L3 实现】两项职责：
        1. 防御性剥离残留的 _ 开头内部字段（_meta, _dynamic 等），
           正常情况下 L2 已经剥离，这里做二次保险。
        2. Prefill Guard：如果最后一条消息是 assistant，追加一条
           user 消息，避免 Anthropic 系模型（通过 OpenAI 兼容端点
           访问时）因连续 assistant 消息报错。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            # 剥离残留的内部字段（_ 开头的键）
            clean = {k: v for k, v in msg.items() if not k.startswith('_')}
            result.append(clean)
        # Prefill Guard：最后一条是 assistant 时追加 user 占位消息，
        # 防止某些 API 端点拒绝以 assistant 结尾的消息列表。
        if result and result[-1].get('role') == 'assistant':
            result.append({'role': 'user', 'content': '请继续。'})
        return result

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
    ) -> None:
        # [provider-registry 2026-05-03] 实例 name 复用 provider_name。
        # 原因：下游仍通过 provider.name 判断 provider 特性；做法：从类属性传入；
        # 目的：注册 key、实例名和配置 provider 字段保持一致。
        super().__init__(model=model, name=self.provider_name)

        k = (api_key or "").strip()
        if not k:
            raise RuntimeError("openai api_key is empty")

        self._http = http
        self._api_key = k
        self._base_url = _normalize_base_url(base_url)

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        """流式聊天补全。逐块回调 on_text / on_thinking / on_tool_delta，最终返回组装好的 ProviderResponse。"""

        url = f"{self._base_url}/chat/completions"

        # L3: 在发送前对消息做 Provider 层预处理（剥离内部字段 + Prefill Guard）
        prepared = self._prepare_messages(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": prepared,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

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
                    # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
                    return ProviderResponse(ok=False, error=msg, status_code=status, inline_data=[], provider_meta={})

                text_parts: list[str] = []
                # [refactor 2026-04-18] thinking_parts → reasoning_parts，
                # 变量名与 ProviderResponse.reasoning 对齐
                reasoning_parts: list[str] = []
                # index -> {id, name, arguments_parts}
                tc_map: dict[int, dict[str, Any]] = {}

                stream_usage: dict[str, int] | None = None
                _stream_finish_reason: str | None = None
                _stream_error: str | None = None
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

                    # [2026-05-24] Capture SSE-level error objects
                    # Proxied Gemini sends {"error": {"message": ..., "code": "content_filter"}}
                    _err_obj = chunk.get("error")
                    if isinstance(_err_obj, dict):
                        _stream_error = _err_obj.get("message") or str(_err_obj)

                    # 捕获 usage（流式最后一个 chunk 携带）
                    raw_usage = chunk.get("usage")
                    if isinstance(raw_usage, dict):
                        stream_usage = _extract_usage(chunk)

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    # [2026-05-24] Capture finish_reason for content_filter detection
                    _fr = choices[0].get("finish_reason")
                    if _fr:
                        _stream_finish_reason = _fr
                    delta = choices[0].get("delta") or {}

                    # 文本内容
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        if on_text:
                            await on_text(content)

                    # 思维链 (reasoning_content / thinking)
                    # 注意：delta 字段名是 API 定义的，不改；只改内部变量名
                    reasoning = delta.get("reasoning_content") or delta.get("thinking")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                        # on_thinking 回调签名暂不改名（engine 接口，后续再统一）
                        if on_thinking:
                            await on_thinking(reasoning)

                    # 工具调用
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
                                # [tool-stream 2026-05-19] 同步发出工具调用开始事件。
                                # 原因：OpenAI SSE 的 tool_calls delta 已经携带 index/id/name，过去只进入 tc_map。
                                # 做法：新 index 第一次出现时调用可选 on_tool_delta，不改变后续聚合逻辑。
                                # 目的：前端能实时看到 tool_call 建立，同时最终 ProviderResponse 仍完整返回。
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
                                # [tool-stream 2026-05-19] 逐片段转发工具参数。
                                # 原因：参数字符串可能很长，等完整 JSON 结束才返回会阻塞实时 UI。
                                # 做法：保留 arg_parts 组装，同时把原始片段作为 args_delta 发出。
                                # 目的：实现 tool_call 参数与文本、thinking 一致的流式通道。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": idx,
                                        "delta": arg_chunk,
                                    })

                text = "".join(text_parts) if text_parts else None
                # [refactor 2026-04-18] thinking_text 局部变量 → reasoning_text，
                # 与 ProviderResponse.reasoning 字段对齐
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

                # [2026-05-24] Detect upstream errors from SSE stream.
                # Various proxied providers (Gemini, OpenAI, Anthropic via relay)
                # may return HTTP 200 but signal failure through:
                #   1. SSE error objects: data: {"error": {"message": ..., "code": ...}}
                #   2. Abnormal finish_reason with empty content (content_filter, error, etc.)
                # Normal finish_reasons: stop, tool_calls — anything else with no
                # content is treated as an upstream error.
                _normal_finish = {"stop", "tool_calls", None}
                if _stream_error:
                    return ProviderResponse(
                        ok=False, text=text, tool_calls=[],
                        reasoning=reasoning_text, status_code=status, usage=stream_usage,
                        inline_data=[], provider_meta={},
                        error=_stream_error,
                    )
                if _stream_finish_reason not in _normal_finish and not text and not tool_calls:
                    return ProviderResponse(
                        ok=False, text=None, tool_calls=[],
                        reasoning=reasoning_text, status_code=status, usage=stream_usage,
                        inline_data=[], provider_meta={},
                        error=f"Upstream error (finish_reason={_stream_finish_reason})",
                    )

                # [refactor 2026-04-18] thinking= → reasoning=，新增 inline_data / provider_meta
                import sys as _ds; _ds.stderr.write(f"[DIAG-PROVIDER] stream end: text={repr((text or '')[:120])} fr={_stream_finish_reason} err={_stream_error} tc={len(tool_calls)}\n"); _ds.stderr.flush()
                return ProviderResponse(
                    ok=True, text=text, tool_calls=tool_calls,
                    reasoning=reasoning_text, status_code=status, usage=stream_usage,
                    inline_data=[], provider_meta={},
                )

        except Exception as e:
            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})


    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ProviderResponse:
        url = f"{self._base_url}/chat/completions"

        # L3: 在发送前对消息做 Provider 层预处理（剥离内部字段 + Prefill Guard）
        prepared = self._prepare_messages(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": prepared,
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
            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})

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

            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(
                ok=False,
                error=msg,
                status_code=status,
                raw=data if isinstance(data, dict) else None,
                inline_data=[], provider_meta={},
            )

        if not isinstance(data, dict):
            return ProviderResponse(ok=False, error="invalid JSON response", status_code=status, inline_data=[], provider_meta={})

        try:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return ProviderResponse(ok=False, error="missing choices", status_code=status, raw=data, inline_data=[], provider_meta={})

            choice0 = choices[0]
            if not isinstance(choice0, dict):
                return ProviderResponse(ok=False, error="invalid choices[0]", status_code=status, raw=data, inline_data=[], provider_meta={})

            msg = choice0.get("message")
            if not isinstance(msg, dict):
                return ProviderResponse(ok=False, error="missing message", status_code=status, raw=data, inline_data=[], provider_meta={})

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
                            fallback = _parse_first_json_object(raw_args)
                            args = fallback if fallback is not None else {"_raw": raw_args}

                    tool_calls.append(ToolCall(id=tc_id_str, name=name.strip(), arguments=args))

            # 解析 usage
            usage = _extract_usage(data)

            # [2026-05-24] Detect upstream errors in non-streaming responses.
            # Same logic as chat_stream: check for error objects and abnormal
            # finish_reason (content_filter, safety blocks, etc.)
            _err_obj = data.get("error")
            if isinstance(_err_obj, dict):
                _err_msg = _err_obj.get("message") or str(_err_obj)
                return ProviderResponse(
                    ok=False, text=text, tool_calls=[],
                    status_code=status, usage=usage,
                    inline_data=[], provider_meta={},
                    error=_err_msg,
                )
            _finish_reason = choice0.get("finish_reason")
            _normal_finish = {"stop", "tool_calls", None}
            if _finish_reason not in _normal_finish and not text and not tool_calls:
                return ProviderResponse(
                    ok=False, text=None, tool_calls=[],
                    status_code=status, usage=usage,
                    inline_data=[], provider_meta={},
                    error=f"Upstream error (finish_reason={_finish_reason})",
                )

            # [refactor 2026-04-18] 非流式也补齐 inline_data / provider_meta（OpenAI 目前不用）
            return ProviderResponse(ok=True, text=text, tool_calls=tool_calls, status_code=status, usage=usage, inline_data=[], provider_meta={})

        except Exception as e:
            return ProviderResponse(ok=False, error=f"failed to parse response: {e}", status_code=status, raw=data, inline_data=[], provider_meta={})
