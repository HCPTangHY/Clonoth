"""Display reasoning is public; request replay remains provider-owned.

All HTTP traffic uses MockTransport. Run with unittest discovery under tests/.
"""
from __future__ import annotations

import copy
from dataclasses import fields
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin import plaintext
from engine.hooks.types import HookResult
from engine.inference import ai_step, llm_call
from engine.inference.message_model import MessageMeta, set_message_meta
from engine.protocol import TaskAction
from providers import AnthropicProvider, GeminiAIStudioProvider, GeminiVertexProvider, OpenAIProvider, OpenAIResponsesProvider
from providers.base import ProviderResponse, ToolCall
from providers.deepseek import DeepSeekProvider
from providers.tool_format import NativeToolFormatter
from test_provider_history_contract import RecordingProvider, history, loop_state, make_provider, no_hook


def make_replay_provider(cls, http=None):
    """OpenAI now requires explicit opt-in for nonstandard request fields."""
    if cls is OpenAIProvider:
        return cls(http=http, api_key="test-key", base_url="https://api.example", model="test-model", reasoning_field="reasoning_content")
    return make_provider(cls, http)


def stored_reply(provider, response):
    message = NativeToolFormatter().build_assistant_message(response, response.text or "", response.tool_calls)
    set_message_meta(message, MessageMeta(
        provider=provider.name, tool_mode="native", message_type="assistant",
        reasoning=response.reasoning or "", metadata={provider.name: response.provider_meta},
        tool_call_ids=[call.id for call in response.tool_calls],
    ))
    result = [message]
    for call in response.tool_calls:
        result.append({"role": "tool", "name": call.name, "tool_call_id": call.id, "content": "done"})
    result.append({"role": "user", "content": "continue"})
    # Test the same JSON-compatible representation used by persisted history.
    return json.loads(json.dumps(result))


class ReasoningBoundaryTests(unittest.TestCase):
    def test_response_has_one_public_display_field(self):
        names = {field.name for field in fields(ProviderResponse)}
        self.assertIn("reasoning", names)
        self.assertTrue({"thought_summary", "has_reasoning", "reasoning_content"}.isdisjoint(names))
        self.assertIn("provider_meta", names)

    def test_native_replay_uses_private_value_not_display_text(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            messages = history()[:-1]
            messages[1]["_meta"].update(provider=cls.provider_name, reasoning="DISPLAY_ONLY")
            messages[1]["_meta"]["metadata"][cls.provider_name] = {"reasoning_content": "NATIVE_ONLY"}
            messages[1]["reasoning_content"] = "STALE_COPY"
            before = copy.deepcopy(messages)
            payload = make_replay_provider(cls, http=object()).build_payload(messages)
            self.assertEqual(payload["messages"][1]["reasoning_content"], "NATIVE_ONLY")
            self.assertNotIn("DISPLAY_ONLY", json.dumps(payload))
            self.assertNotIn("STALE_COPY", json.dumps(payload))
            self.assertNotIn('"_meta"', json.dumps(payload))
            self.assertEqual(messages, before)

    def test_foreign_display_and_legacy_api_fields_are_not_replayed(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            messages = history()[:-1]
            messages[1]["_meta"].update(provider="gemini-aistudio", reasoning="FOREIGN_DISPLAY")
            messages[1]["reasoning_content"] = "FOREIGN_OLD_COPY"
            payload = make_provider(cls, http=object()).build_payload(messages)
            self.assertNotIn("FOREIGN_DISPLAY", json.dumps(payload))
            self.assertNotIn("FOREIGN_OLD_COPY", json.dumps(payload))
            if cls is DeepSeekProvider:
                self.assertEqual(payload["messages"][1]["reasoning_content"], "")
            else:
                self.assertNotIn("reasoning_content", payload["messages"][1])

    def test_new_records_without_native_reasoning_do_not_fall_back_to_display(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            messages = history()[:-1]
            messages[1]["_meta"].update(provider=cls.provider_name, reasoning="DISPLAY_ONLY")
            messages[1]["_meta"]["metadata"][cls.provider_name] = {"reasoning_content": None}
            payload = make_provider(cls, http=object()).build_payload(messages)
            self.assertNotIn("DISPLAY_ONLY", json.dumps(payload))

    def test_direct_api_shaped_history_remains_supported(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            messages = history()[:-1]
            messages[1].pop("_meta")
            messages[1]["reasoning_content"] = "EXPLICIT_API_VALUE"
            payload = make_replay_provider(cls, http=object()).build_payload(messages)
            self.assertEqual(payload["messages"][1]["reasoning_content"], "EXPLICIT_API_VALUE")

    def test_legacy_deepseek_recovery_is_provider_owned_and_summary_safe(self):
        provider = make_provider(DeepSeekProvider, http=object())
        messages = history()[:-1]
        messages[1]["_meta"].update(provider="deepseek", reasoning="LEGACY_NATIVE")
        messages[1]["_meta"]["metadata"]["deepseek"] = {"provider": "deepseek", "thinking": True}
        before = copy.deepcopy(messages)
        self.assertEqual(provider.build_payload(messages)["messages"][1]["reasoning_content"], "LEGACY_NATIVE")
        self.assertEqual(messages, before)
        messages[1]["_meta"]["thought_summary"] = "LEGACY_SUMMARY"
        self.assertEqual(provider.build_payload(messages)["messages"][1]["reasoning_content"], "")
        self.assertNotIn("LEGACY_SUMMARY", json.dumps(provider.build_payload(messages)))

    def test_display_only_history_is_not_assumed_to_be_legacy_deepseek_data(self):
        provider = make_provider(DeepSeekProvider, http=object())
        messages = history()[:-1]
        messages[1]["_meta"].update(provider="deepseek", reasoning="DISPLAY_ONLY")
        payload = provider.build_payload(messages)
        self.assertEqual(payload["messages"][1]["reasoning_content"], "")
        self.assertNotIn("DISPLAY_ONLY", json.dumps(payload))

    def test_text_tool_modes_and_disabled_deepseek_do_not_replay_native_reasoning(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            provider = make_replay_provider(cls, http=object())
            messages = history()[:-1]
            messages[1]["_meta"].update(provider=cls.provider_name, reasoning="DISPLAY_ONLY")
            messages[1]["_meta"]["metadata"][cls.provider_name] = {"reasoning_content": "NATIVE_ONLY"}
            for mode in ("json", "fake-native", "fake_native"):
                payload = provider.build_payload(messages, tool_mode=mode)
                self.assertTrue(all("reasoning_content" not in message for message in payload["messages"]))
            if cls is DeepSeekProvider:
                provider = DeepSeekProvider(http=object(), api_key="test-key", reasoning_effort="none")
                payload = provider.build_payload(messages)
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertTrue(all("reasoning_content" not in message for message in payload["messages"]))


class ReasoningTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_openai_response_does_not_add_empty_replay_metadata(self):
        def handle(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            response = await make_provider(OpenAIProvider, http).chat(messages=[{"role": "user", "content": "hello"}])
        self.assertTrue(response.ok, response.error)
        self.assertIsNone(response.reasoning)
        self.assertEqual(response.provider_meta, {})

    async def test_chat_completions_display_and_native_data_round_trip_separately(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            response_fields = ("reasoning_content", "reasoning", "thinking") if cls is OpenAIProvider else ("reasoning_content",)
            for field in response_fields:
                for stream in (False, True):
                    with self.subTest(provider=cls.provider_name, field=field, stream=stream):
                        native = "provider text\n"
                        tool_call = {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}

                        def handle(request):
                            message = {"content": "answer", field: native, "tool_calls": [tool_call]}
                            if stream:
                                delta = dict(message, tool_calls=[dict(tool_call, index=0)])
                                event = {"choices": [{"delta": delta, "finish_reason": "tool_calls"}]}
                                return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
                            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]})

                        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                            provider = make_replay_provider(cls, http)
                            chunks = []

                            async def on_thinking(text):
                                chunks.append(text)

                            if stream:
                                response = await provider.chat_stream(messages=[{"role": "user", "content": "hello"}], on_thinking=on_thinking)
                                self.assertEqual("".join(chunks), native)
                            else:
                                response = await provider.chat(messages=[{"role": "user", "content": "hello"}])
                            self.assertTrue(response.ok, response.error)
                            self.assertEqual(response.reasoning, native)
                            self.assertEqual(response.provider_meta["reasoning_content"], native if field == "reasoning_content" else None)
                            messages = stored_reply(provider, response)
                            self.assertNotIn("reasoning_content", messages[0])
                            self.assertNotIn("has_reasoning", messages[0]["_meta"])
                            messages[0]["_meta"]["reasoning"] = "EDITED_DISPLAY"
                            before = copy.deepcopy(messages)
                            payload = provider.build_payload(messages)
                            if field == "reasoning_content":
                                self.assertEqual(payload["messages"][0]["reasoning_content"], native)
                            else:
                                self.assertEqual(payload["messages"][0]["reasoning_content"], "")
                            self.assertNotIn("EDITED_DISPLAY", json.dumps(payload))
                            self.assertEqual(messages, before)

    async def test_responses_body_and_summary_share_display_but_preserve_raw_output(self):
        body_text, summary_text = "body\n", "summary\n"
        raw_output = [{
            "type": "reasoning", "encrypted_content": "encrypted-test",
            "content": [{"type": "reasoning_text", "text": body_text}],
            "summary": [{"type": "summary_text", "text": summary_text}],
        }, {"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]
        for stream in (False, True):
            def handle(request):
                response = {"output": raw_output, "usage": {}}
                if stream:
                    events = [
                        {"type": "response.reasoning_text.delta", "delta": body_text},
                        {"type": "response.reasoning_summary_text.delta", "delta": summary_text},
                        {"type": "response.output_text.delta", "delta": "answer"},
                        {"type": "response.completed", "response": response},
                    ]
                    return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
                return httpx.Response(200, json=response)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                provider = make_provider(OpenAIResponsesProvider, http)
                method = provider.chat_stream if stream else provider.chat
                response = await method(messages=[{"role": "user", "content": "hello"}])
                self.assertTrue(response.ok, response.error)
                self.assertEqual(response.reasoning, body_text + summary_text)
                self.assertEqual(response.provider_meta["raw_output"], raw_output)
                messages = stored_reply(provider, response)
                messages[0]["_meta"]["reasoning"] = "EDITED_DISPLAY"
                payload = provider.build_payload(messages)
                self.assertEqual(payload["input"][:2], raw_output)
                self.assertNotIn("EDITED_DISPLAY", json.dumps(payload))

    async def test_signed_provider_data_is_not_rebuilt_from_display_text(self):
        for cls in (AnthropicProvider, GeminiAIStudioProvider, GeminiVertexProvider):
            for stream in (False, True):
                gemini_parts = [
                    {"thought": True, "text": "summary", "thoughtSignature": "original-signature"},
                    {"text": "answer"},
                ]
                anthropic_blocks = [
                    {"type": "thinking", "thinking": "summary", "signature": "original-signature"},
                    {"type": "text", "text": "answer"},
                ]

                def handle(request):
                    if cls is AnthropicProvider:
                        response = {"content": anthropic_blocks, "usage": {}}
                        events = [
                            {"type": "message_start", "message": {"usage": {}}},
                            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "summary"}},
                            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "original-signature"}},
                            {"type": "content_block_stop", "index": 0},
                            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
                            {"type": "message_stop"},
                        ]
                    else:
                        response = {"candidates": [{"content": {"parts": gemini_parts}}]}
                        events = [response]
                    if stream:
                        return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
                    return httpx.Response(200, json=response)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    provider = make_provider(cls, http)
                    method = provider.chat_stream if stream else provider.chat
                    response = await method(messages=[{"role": "user", "content": "hello"}])
                    self.assertTrue(response.ok, response.error)
                    self.assertEqual(response.reasoning, "summary")
                    private = copy.deepcopy(response.provider_meta)
                    messages = stored_reply(provider, response)
                    messages[0]["_meta"]["reasoning"] = "EDITED_DISPLAY"
                    payload = provider.build_payload(messages)
                    if cls is AnthropicProvider:
                        assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
                        self.assertEqual(assistant["content"][:1], anthropic_blocks[:1])
                    else:
                        self.assertEqual(payload["contents"][0]["parts"], gemini_parts)
                    self.assertNotIn("EDITED_DISPLAY", json.dumps(payload))
                    self.assertEqual(response.provider_meta, private)

    async def test_engine_stores_display_and_private_data_without_api_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider()
            state = loop_state(Path(directory), provider, messages=[])
            state.response_provider = SimpleNamespace(name="deepseek")
            state.rctx.response_info = {"provider": "deepseek"}
            response = ProviderResponse(
                ok=True, reasoning="DISPLAY_ONLY", provider_meta={"reasoning_content": "NATIVE_ONLY"},
                tool_calls=[ToolCall(id="finish_1", name="finish", arguments={"text": "answer"})],
            )
            stop = TaskAction(action="finish", node_id="audit", result={"text": "stop"})
            with patch.object(ai_step, "_shadow_write"), patch.object(ai_step.hook_registry, "afire", new=AsyncMock(return_value=HookResult(action=stop))):
                await ai_step._handle_tool_calls(state, response, 0)
            with patch.object(ai_step, "_shadow_write"), patch.object(plaintext, "_persist_ctx", return_value="ctx"):
                plaintext._build_implicit_finish(SimpleNamespace(step=0), state, response, "answer")
            for message in state.messages:
                self.assertNotIn("reasoning_content", message)
                self.assertNotIn("has_reasoning", message["_meta"])
                self.assertNotIn("thought_summary", message["_meta"])
                self.assertEqual(message["_meta"]["reasoning"], "DISPLAY_ONLY")
                self.assertEqual(message["_meta"]["metadata"]["deepseek"]["reasoning_content"], "NATIVE_ONLY")

    async def test_stream_end_reasoning_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider([ProviderResponse(ok=True, text="answer", reasoning="display")])

            async def stream(**kwargs):
                await kwargs["on_thinking"]("display")
                await kwargs["on_text"]("answer")
                return provider.responses[0]

            provider.chat_stream = stream
            state = loop_state(Path(directory), provider, stream=True)
            with patch.object(llm_call.hook_registry, "afire", side_effect=no_hook):
                response = await llm_call._call_llm_with_retry(state, 0)
            self.assertTrue(response.ok)
            endings = [call.args[1] for call in state.rctx.emit_event.await_args_list if call.args[0] == "stream_end"]
            self.assertEqual(len(endings), 1)
            self.assertTrue(endings[0]["has_reasoning"])


if __name__ == "__main__":
    unittest.main()
