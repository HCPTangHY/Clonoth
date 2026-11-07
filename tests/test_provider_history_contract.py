"""Provider-owned history tests. Only local fixtures and mock HTTP are used.

Run: python3.11 -B -m unittest discover -s tests -p 'test_provider*.py' -v
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.attachments import prepare_messages_for_llm
from engine.builtin import fallback_provider, plaintext
from engine.builtin.error_snapshot import ErrorSnapshotHandler
from engine.hooks.types import HookContext, HookResult
from engine.inference import ai_step, llm_call
from engine.inference.loop_state import _LoopState
from engine.node import Node
from engine.protocol import TaskAction
from providers import AnthropicProvider, GeminiAIStudioProvider, GeminiVertexProvider, OpenAIProvider, OpenAIResponsesProvider
from providers.base import BaseProvider, ProviderResponse, ToolCall
from providers.deepseek import DeepSeekProvider
from providers.tool_format import (
    NativeToolFormatter, create_tool_formatter,
    prepare_raw_history, sanitize_control_tool_history,
)

PROVIDERS = (OpenAIProvider, DeepSeekProvider, AnthropicProvider, OpenAIResponsesProvider, GeminiAIStudioProvider, GeminiVertexProvider)
ROOT = Path(__file__).resolve().parents[1]


def history():
    gemini_parts = [{"functionCall": {"name": "lookup", "args": {"query": "example"}}, "thoughtSignature": "gemini-signature"}]
    return [
        {"role": "user", "content": "question", "response_info": {"notice": "UI_ONLY"}},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"query": "example"}}],
         "_meta": {"tool_mode": "native", "metadata": {
             "anthropic": {"thinking_blocks": [{"type": "thinking", "thinking": "summary", "signature": "anthropic-signature"}]},
             "openai-responses": {"raw_output": [
                 {"type": "reasoning", "summary": [], "encrypted_content": "encrypted-test"},
                 {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"query":"example"}'},
             ]},
             "gemini-aistudio": {"raw_parts": copy.deepcopy(gemini_parts)},
             "gemini-vertex": {"raw_parts": copy.deepcopy(gemini_parts)},
         }}},
        {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "found"},
        {"role": "user", "content": "next"},
        {"role": "user", "content": "EPHEMERAL_ONLY", "_ephemeral": True},
    ]


def make_provider(cls, http=None):
    return cls(http=http, api_key="test-key", base_url="https://api.example", model="test-model")


async def no_hook(*args, **kwargs):
    return HookResult()


def loop_state(root, provider, messages=None, stream=False):
    rctx = SimpleNamespace(
        workspace_root=root, workspace=None, task_id="task", session_id="session",
        begin_llm_request=lambda: "request", emit_event=AsyncMock(),
        check_cancelled=AsyncMock(return_value=False), check_preempted=AsyncMock(return_value={}),
        tool_call_log=[], current_llm_request_id="request", llm_http=object(),
    )
    return _LoopState(
        registry=SimpleNamespace(), run_id="test", context_ref="", runtime_cfg={},
        streaming=stream, system_prompt=[], is_block_mode=False, history=[],
        collected_attachments=[], tool_produced_attachments=[],
        provider=provider, response_provider=None, rctx=rctx,
        node=Node(id="audit", type="ai", tool_mode="native", output_mode="tool_only"),
        messages=history() if messages is None else messages, formatter=NativeToolFormatter(),
        openai_tools=[], use_stream=stream, retry_max=0, retry_initial_delay=0,
        retry_max_delay=0, retry_backoff=1, preempt_after_step=False,
        preempt_inject_info=None, request_history=None, last_usage={},
        compact_threshold=100_000, compacted=False,
        allowed_pseudo_tools={"finish"}, allowed_real_tools=set(),
    )


class RecordingProvider(BaseProvider):
    provider_name = "unlisted-history-test"

    def __init__(self, responses=None):
        super().__init__(model="test-model")
        self.calls = []
        self.responses = list(responses or [ProviderResponse(ok=True, text="ok")])
        self._http = object()

    def prepare_history(self, *args, **kwargs):
        raise AssertionError("Engine must not select or prepare a provider history representation")

    async def chat(self, *, messages, tools=None, tool_mode="native"):
        self.calls.append((copy.deepcopy(messages), tool_mode))
        messages[0]["content"] = "PROVIDER_MUTATION"
        if len(messages) > 1 and "_meta" in messages[1]:
            messages[1]["_meta"]["mutated"] = True
        return self.responses.pop(0)

    async def chat_stream(self, *, messages, tools=None, tool_mode="native", on_text=None, on_thinking=None, on_tool_delta=None):
        result = await self.chat(messages=messages, tools=tools, tool_mode=tool_mode)
        if on_text and result.text:
            await on_text(result.text)
        return result


class ProviderPayloadTests(unittest.TestCase):
    def test_each_provider_owns_native_json_and_fake_native_conversion(self):
        for cls in PROVIDERS:
            provider = make_provider(cls, http=object())
            for mode in ("native", "fake-native", "json"):
                with self.subTest(provider=cls.provider_name, mode=mode):
                    messages = history()
                    before = copy.deepcopy(messages)
                    payload = provider.build_payload(messages, tool_mode=mode)
                    encoded = json.dumps(payload)
                    self.assertNotIn('"_meta"', encoded)
                    self.assertNotIn("UI_ONLY", encoded)
                    self.assertNotIn("EPHEMERAL_ONLY", encoded)
                    self.assertEqual(messages, before)
                    if mode == "json":
                        self.assertIn("<<<TOOL_CALL>>>", encoded)
                        self.assertNotIn("anthropic-signature", encoded)
                        self.assertNotIn("gemini-signature", encoded)
                        self.assertNotIn("encrypted-test", encoded)
                    elif mode == "native":
                        expected = {
                            "anthropic": "anthropic-signature",
                            "openai-responses": "encrypted-test",
                            "gemini-vertex": "gemini-signature",
                            "gemini-aistudio": "gemini-signature",
                        }.get(cls.provider_name)
                        if expected:
                            self.assertIn(expected, encoded)
                        self.assertIn("lookup", encoded)

    def test_native_conversion_accepts_existing_api_tool_calls(self):
        messages = history()
        messages[1]["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"query":"example"}'}}]
        payload = make_provider(OpenAIProvider, http=object()).build_payload(messages)
        call = payload["messages"][1]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"query": "example"})
        for mode in ("json", "fake-native"):
            encoded = json.dumps(make_provider(OpenAIProvider, http=object()).build_payload(messages, tool_mode=mode))
            self.assertIn("lookup", encoded)
            self.assertIn("example", encoded)

    def test_assistant_images_are_not_removed_by_engine(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "answer"}, image]}]
        before = copy.deepcopy(messages)
        prepared = prepare_messages_for_llm(messages, ROOT)
        self.assertEqual(prepared[0]["content"], messages[0]["content"])
        payload = make_provider(AnthropicProvider, http=object()).build_payload(prepared)
        self.assertFalse(any(block.get("type") == "image" for message in payload["messages"] if message["role"] == "assistant" for block in message["content"]))
        oai_payload = make_provider(OpenAIProvider, http=object()).build_payload(prepared)
        self.assertNotIn('"image_url"', json.dumps(oai_payload))
        self.assertEqual(messages, before)

    def test_deepseek_preserves_user_images(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        provider = make_provider(DeepSeekProvider, http=object())
        for content in ([image], [{"type": "text", "text": "Describe this image."}, image]):
            for mode in ("native", "fake-native", "json"):
                for stream in (False, True):
                    with self.subTest(content=content, mode=mode, stream=stream):
                        messages = [{"role": "user", "content": content}]
                        before = copy.deepcopy(messages)
                        payload = provider.build_payload(messages, tool_mode=mode, stream=stream)
                        self.assertEqual(payload["messages"][0]["content"], content)
                        self.assertEqual(messages, before)

    def test_provider_layer_does_not_import_engine(self):
        for path in (ROOT / "providers").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("engine"), str(path))
                elif isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.startswith("engine") for alias in node.names), str(path))
        source = (ROOT / "engine/inference/llm_call.py").read_text(encoding="utf-8")
        self.assertNotIn("_build_messages_for_provider", source)
        self.assertNotIn("gemini-aistudio", source)
        self.assertNotIn("requires_raw_history", source)


class HistoryPairingTests(unittest.TestCase):
    @staticmethod
    def legacy_history(stored_mode="fake-native", output="found"):
        messages = history()[:-1]
        if stored_mode is None:
            messages[1]["_meta"].pop("tool_mode")
        else:
            messages[1]["_meta"]["tool_mode"] = stored_mode
        messages[2] = {"role": "user", "content": f'Tool result for "lookup":\n{output}'}
        return messages

    @staticmethod
    def parallel_history(cls, kept_id="call_2"):
        # Identical arguments test call identity, not just matching tool names.
        parts = [
            {"thought": True, "text": "summary", "thoughtSignature": "thought-signature"},
            {"functionCall": {"name": "lookup", "args": {"query": "same"}}, "thoughtSignature": "first-call-signature"},
            {"functionCall": {"name": "lookup", "args": {"query": "same"}}},
        ]
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "name": "lookup", "arguments": {"query": "same"}},
                {"id": "call_2", "name": "lookup", "arguments": {"query": "same"}},
            ], "_meta": {"tool_mode": "native", "tool_call_ids": ["call_1", "call_2"], "metadata": {cls.provider_name: {"raw_parts": parts}}}},
            {"role": "tool", "tool_call_id": kept_id, "content": "KNOWN_RESULT"},
        ]
        return messages, parts

    def test_legacy_results_are_native_for_every_provider(self):
        output = "  first line\n\tsecond line\n"
        for cls in PROVIDERS:
            for stored_mode in (None, "fake-native", "fake_native", "json", "native"):
                with self.subTest(provider=cls.provider_name, stored_mode=stored_mode):
                    messages = self.legacy_history(stored_mode, output)
                    before = copy.deepcopy(messages)
                    provider = make_provider(cls, http=object())
                    prepared = provider.prepare_history(messages, tool_mode="native")
                    self.assertEqual(prepared[2]["role"], "tool")
                    self.assertEqual(prepared[2]["tool_call_id"], "call_1")
                    self.assertEqual(prepared[2]["content"], output)
                    payload = provider.build_payload(messages, tool_mode="native")
                    if cls in (GeminiAIStudioProvider, GeminiVertexProvider):
                        results = [part["functionResponse"] for row in payload["contents"] for part in row["parts"] if "functionResponse" in part]
                        self.assertEqual(results, [{"name": "lookup", "response": {"result": output}}])
                    elif cls is AnthropicProvider:
                        results = [block for row in payload["messages"] for block in row["content"] if block.get("type") == "tool_result"]
                        self.assertEqual([(item["tool_use_id"], item["content"]) for item in results], [("call_1", output)])
                    elif cls is OpenAIResponsesProvider:
                        results = [item for item in payload["input"] if item.get("type") == "function_call_output"]
                        self.assertEqual([(item["call_id"], item["output"]) for item in results], [("call_1", output)])
                    else:
                        results = [row for row in payload["messages"] if row["role"] == "tool"]
                        self.assertEqual([(row["tool_call_id"], row["content"]) for row in results], [("call_1", output)])
                    self.assertEqual(messages, before)

    def test_text_modes_keep_text_results_and_raw_providers_keep_native_pairs(self):
        for cls in PROVIDERS:
            for mode in ("json", "fake-native"):
                with self.subTest(provider=cls.provider_name, mode=mode):
                    prepared = make_provider(cls, http=object()).prepare_history(self.legacy_history(), tool_mode=mode)
                    uses_native = mode == "fake-native" and cls in (GeminiAIStudioProvider, GeminiVertexProvider, OpenAIResponsesProvider)
                    if uses_native:
                        self.assertEqual(prepared[2]["role"], "tool")
                        self.assertEqual(prepared[2]["tool_call_id"], "call_1")
                    else:
                        self.assertFalse(any(row["role"] == "tool" or row.get("tool_calls") for row in prepared))
                        self.assertEqual(prepared[2]["content"], 'Tool result for "lookup":\nfound')

    def test_legacy_pairing_aligns_declared_ids_and_preserves_output_whitespace(self):
        for explicit_ids in (False, True):
            messages = [{"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "name": "lookup", "arguments": {}},
                {"id": "b", "name": "lookup", "arguments": {}},
            ]}]
            ids = ["b", "a"] if explicit_ids else ["a", "b"]
            outputs = ["  first\n", "\tsecond\n"]
            for call_id, output in zip(ids, outputs):
                row = {"role": "user", "content": 'Tool result for "lookup": ' + output}
                if explicit_ids:
                    row["tool_call_id"] = call_id
                messages.append(row)
            before = copy.deepcopy(messages)
            prepared = prepare_raw_history(messages)
            self.assertEqual([row["tool_call_id"] for row in prepared[1:]], ["a", "b"])
            by_id = dict(zip(ids, outputs))
            self.assertEqual([row["content"] for row in prepared[1:]], [by_id["a"], by_id["b"]])
            self.assertEqual(messages, before)
            self.assertEqual(prepare_raw_history(prepared), prepared)

    def test_unmatched_text_is_not_promoted_to_a_tool_result(self):
        messages = [{"role": "user", "content": 'Tool result for "lookup":\nnot a paired result'}]
        self.assertEqual(prepare_raw_history(messages), messages)
        for cls in PROVIDERS:
            prepared = make_provider(cls, http=object()).prepare_history(messages, tool_mode="native")
            self.assertEqual(prepared, messages)

    def test_storage_cleaner_does_not_change_legacy_representation(self):
        messages = self.legacy_history()
        before = copy.deepcopy(messages)
        stored = sanitize_control_tool_history(messages)
        self.assertEqual(stored[2], messages[2])
        prepared = prepare_raw_history(stored)
        self.assertEqual(prepared[2]["role"], "tool")
        self.assertEqual(messages, before)
        self.assertEqual(stored, before)

    def test_gemini_orphan_calls_cannot_return_from_raw_metadata(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            for mode in ("native", "fake-native"):
                for already_cleaned in (False, True):
                    with self.subTest(provider=cls.provider_name, mode=mode, already_cleaned=already_cleaned):
                        messages = history()[:-1]
                        del messages[2]
                        if already_cleaned:
                            messages = sanitize_control_tool_history(messages)
                        before = copy.deepcopy(messages)
                        payload = make_provider(cls, http=object()).build_payload(messages, tool_mode=mode)
                        encoded = json.dumps(payload)
                        self.assertNotIn('"functionCall"', encoded)
                        self.assertNotIn('"functionResponse"', encoded)
                        self.assertNotIn("gemini-signature", encoded)
                        self.assertIn("lookup", encoded)
                        self.assertEqual(messages, before)

    def test_incomplete_parallel_turn_keeps_observed_results_as_text(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            for kept_id in ("call_1", "call_2"):
                for mode in ("native", "fake-native"):
                    with self.subTest(provider=cls.provider_name, kept_id=kept_id, mode=mode):
                        messages, parts = self.parallel_history(cls, kept_id)
                        before = copy.deepcopy(messages)
                        payload = make_provider(cls, http=object()).build_payload(messages, tool_mode=mode)
                        encoded = json.dumps(payload)
                        self.assertNotIn('"functionCall"', encoded)
                        self.assertNotIn('"functionResponse"', encoded)
                        self.assertNotIn("thoughtSignature", encoded)
                        self.assertEqual(encoded.count("KNOWN_RESULT"), 1)
                        self.assertIn("lookup", encoded)
                        self.assertEqual(messages, before)
                        self.assertEqual(messages[0]["_meta"]["metadata"][cls.provider_name]["raw_parts"], parts)

    def test_truncated_cache_cannot_borrow_another_calls_signature(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            for saved_ids in (False, True):
                messages, parts = self.parallel_history(cls)
                # The cache lost call_2, while the only surviving result belongs
                # to call_2. Names and arguments alone cannot identify the call.
                parts.pop()
                if saved_ids:
                    messages = sanitize_control_tool_history(messages)
                else:
                    messages[0]["_meta"].pop("tool_call_ids")
                before = copy.deepcopy(messages)
                payload = make_provider(cls, http=object()).build_payload(messages)
                encoded = json.dumps(payload)
                self.assertNotIn("first-call-signature", encoded)
                self.assertNotIn('"functionCall"', encoded)
                self.assertNotIn('"functionResponse"', encoded)
                self.assertEqual(encoded.count("KNOWN_RESULT"), 1)
                self.assertEqual(messages, before)

    def test_result_id_takes_precedence_over_a_stale_result_name(self):
        for structured in (False, True):
            messages = self.legacy_history()
            if structured:
                messages[2] = {"role": "tool", "tool_call_id": "call_1", "name": "old_name", "content": "found"}
            else:
                messages[2] = {"role": "user", "tool_call_id": "call_1", "content": 'Tool result for "old_name":\nfound'}
            before = copy.deepcopy(messages)
            prepared = prepare_raw_history(messages)
            self.assertEqual(prepared[2]["tool_call_id"], "call_1")
            self.assertEqual(prepared[2]["name"], "lookup")
            self.assertEqual(prepared[2]["content"], "found")
            self.assertEqual(sanitize_control_tool_history(messages)[2], messages[2])
            for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
                payload = make_provider(cls, http=object()).build_payload(messages)
                results = [part["functionResponse"] for row in payload["contents"] for part in row["parts"] if "functionResponse" in part]
                self.assertEqual(results, [{"name": "lookup", "response": {"result": "found"}}])
            self.assertEqual(messages, before)

    def test_complete_parallel_response_is_replayed_without_modification(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            messages, parts = self.parallel_history(cls, "call_1")
            messages.append({"role": "tool", "tool_call_id": "call_2", "content": "SECOND_RESULT"})
            before = copy.deepcopy(messages)
            payload = make_provider(cls, http=object()).build_payload(messages)
            self.assertEqual(payload["contents"][0]["parts"], parts)
            self.assertEqual(len(payload["contents"][1]["parts"]), 2)
            self.assertEqual(messages, before)

    def test_damaged_turn_does_not_change_later_turn_with_reused_ids(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            messages, _ = self.parallel_history(cls)
            healthy = history()[1:3]
            healthy[1]["content"] = "LATER_RESULT"
            messages.extend(healthy)
            before = copy.deepcopy(messages)
            payload = make_provider(cls, http=object()).build_payload(messages)
            encoded = json.dumps(payload)
            self.assertEqual(encoded.count('"functionCall"'), 1)
            self.assertEqual(encoded.count('"functionResponse"'), 1)
            self.assertIn("gemini-signature", encoded)
            self.assertNotIn("first-call-signature", encoded)
            self.assertEqual(encoded.count("KNOWN_RESULT"), 1)
            self.assertEqual(encoded.count("LATER_RESULT"), 1)
            self.assertEqual(messages, before)

    def test_stale_or_malformed_cache_does_not_reuse_signatures(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            for damage in ("arguments", "parts", "argument_types"):
                messages = history()[:-1]
                if damage == "parts":
                    messages[1]["_meta"]["metadata"][cls.provider_name]["raw_parts"].append(None)
                elif damage == "argument_types":
                    messages[1]["tool_calls"][0]["arguments"] = {"query": False}
                    messages[1]["_meta"]["metadata"][cls.provider_name]["raw_parts"][0]["functionCall"]["args"] = {"query": 0}
                else:
                    messages[1]["tool_calls"][0]["arguments"] = {"query": "updated"}
                before = copy.deepcopy(messages)
                payload = make_provider(cls, http=object()).build_payload(messages)
                encoded = json.dumps(payload)
                self.assertNotIn("gemini-signature", encoded)
                self.assertNotIn('"functionCall"', encoded)
                self.assertNotIn('"functionResponse"', encoded)
                self.assertIn("found", encoded)
                self.assertEqual(messages, before)


class ProviderRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_nonstream_use_public_payload_builder(self):
        def handle(request):
            body = json.loads(request.content)
            stream = body.get("stream") or "streamGenerateContent" in str(request.url)
            path = request.url.path
            if path.endswith("/responses"):
                response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], "usage": {}}
                events = [{"type": "response.output_text.delta", "delta": "ok"}, {"type": "response.completed", "response": response}]
            elif path.endswith("/messages"):
                response = {"content": [{"type": "text", "text": "ok"}], "usage": {}}
                events = [{"type": "message_start", "message": {"usage": {}}}, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}, {"type": "message_stop"}]
            elif "generatecontent" in path.lower():
                response = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
                events = [response]
            else:
                response = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
                events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
            if stream:
                return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events), headers={"Content-Type": "text/event-stream"})
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            for cls in PROVIDERS:
                provider = make_provider(cls, http)
                for mode in ("native", "fake-native", "json"):
                    for method in (provider.chat, provider.chat_stream):
                        with self.subTest(provider=cls.provider_name, mode=mode, method=method.__name__), patch.object(provider, "build_payload", wraps=provider.build_payload) as builder:
                            messages = history()
                            before = copy.deepcopy(messages)
                            reply = await method(messages=messages, tool_mode=mode)
                            self.assertTrue(reply.ok, reply.error)
                            self.assertEqual(reply.text, "ok")
                            self.assertEqual(builder.call_count, 1)
                            self.assertEqual(builder.call_args.kwargs["tool_mode"], mode)
                            self.assertEqual(messages, before)

    async def test_gemini_history_repairs_reach_stream_and_nonstream_requests(self):
        sent = []

        def handle(request):
            sent.append(json.loads(request.content))
            response = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            if "streamGenerateContent" in str(request.url):
                return httpx.Response(200, text="data: " + json.dumps(response) + "\n\n")
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
                provider = make_provider(cls, http)
                for mode in ("native", "fake-native"):
                    for method in (provider.chat, provider.chat_stream):
                        for damaged in (False, True):
                            messages = HistoryPairingTests.legacy_history()
                            if damaged:
                                messages[1]["_meta"]["metadata"][cls.provider_name]["raw_parts"].append({
                                    "functionCall": {"name": "orphan", "args": {}},
                                })
                            before = copy.deepcopy(messages)
                            reply = await method(messages=messages, tool_mode=mode)
                            self.assertTrue(reply.ok, reply.error)
                            encoded = json.dumps(sent[-1])
                            self.assertIn("found", encoded)
                            self.assertEqual(encoded.count('"functionCall"'), 0 if damaged else 1)
                            self.assertEqual(encoded.count('"functionResponse"'), 0 if damaged else 1)
                            self.assertEqual("gemini-signature" in encoded, not damaged)
                            self.assertNotIn('"_meta"', encoded)
                            self.assertEqual(messages, before)
        self.assertEqual(len(sent), 16)

    async def test_responses_signature_retry_still_uses_an_isolated_payload(self):
        for stream in (False, True):
            sent = []

            def handle(request):
                sent.append(json.loads(request.content))
                if len(sent) == 1:
                    return httpx.Response(400, json={"error": {"message": "encrypted content could not be verified"}})
                response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], "usage": {}}
                if stream:
                    events = [{"type": "response.output_text.delta", "delta": "ok"}, {"type": "response.completed", "response": response}]
                    return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
                return httpx.Response(200, json=response)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                provider = make_provider(OpenAIResponsesProvider, http)
                messages = history()
                before = copy.deepcopy(messages)
                method = provider.chat_stream if stream else provider.chat
                reply = await method(messages=messages, tool_mode="native")
                self.assertTrue(reply.ok, reply.error)
                self.assertEqual(len(sent), 2)
                self.assertIn("encrypted-test", json.dumps(sent[0]))
                self.assertNotIn("encrypted-test", json.dumps(sent[1]))
                self.assertEqual(messages, before)

    async def test_unlisted_provider_receives_internal_history_and_hook_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stream in (False, True):
                provider = RecordingProvider()
                ls = loop_state(Path(tmp), provider, stream=stream)
                before = copy.deepcopy(ls.messages)

                async def hook(name, ctx):
                    self.assertEqual(name, "before_llm_call")
                    self.assertIs(ctx.provider, provider)
                    ctx.messages[0]["content"] += " [expanded]"
                    ctx.messages[1]["_meta"]["hook_marker"] = True
                    return HookResult()

                with patch.object(llm_call.hook_registry, "afire", side_effect=hook):
                    reply = await llm_call._call_llm_with_retry(ls, 0)
                self.assertTrue(reply.ok)
                received, mode = provider.calls[0]
                self.assertEqual(mode, "native")
                self.assertIn("_meta", received[1])
                self.assertTrue(received[1]["_meta"]["hook_marker"])
                self.assertEqual(received[1]["tool_calls"][0]["name"], "lookup")
                self.assertEqual(received[0]["content"], "question [expanded]")
                self.assertEqual(ls.messages, before)
                self.assertEqual(ls.request_history[0]["content"], "question [expanded]")

    async def test_normal_retry_does_not_reuse_mutated_provider_input(self):
        provider = RecordingProvider([ProviderResponse(ok=False, error="busy", status_code=503), ProviderResponse(ok=True, text="ok")])
        with tempfile.TemporaryDirectory() as tmp:
            ls = loop_state(Path(tmp), provider)
            ls.retry_max = 1
            with patch.object(llm_call.hook_registry, "afire", side_effect=no_hook):
                reply = await llm_call._call_llm_with_retry(ls, 0)
            self.assertTrue(reply.ok)
            self.assertEqual(provider.calls[0][0], provider.calls[1][0])
            self.assertNotIn("mutated", provider.calls[1][0][1]["_meta"])

    async def test_compacted_retry_reapplies_hook_and_propagates_action(self):
        for stop in (False, True):
            provider = RecordingProvider([ProviderResponse(ok=False, error="context too long", status_code=413), ProviderResponse(ok=True, text="ok")])
            action = TaskAction(action="finish", node_id="audit", result={"text": "stop"})
            count = 0

            async def hook(name, ctx):
                nonlocal count
                count += 1
                ctx.messages[0]["content"] += " [hook]"
                return HookResult(action=action if stop and count == 2 else None)

            def compact(messages, **kwargs):
                messages[0]["content"] = "compacted"
                return messages, 1

            with tempfile.TemporaryDirectory() as tmp:
                ls = loop_state(Path(tmp), provider)
                with patch.object(llm_call.hook_registry, "afire", side_effect=hook), patch("engine.compact.microcompact_messages", side_effect=compact):
                    reply = await llm_call._call_llm_with_retry(ls, 0)
                self.assertEqual(count, 2)
                if stop:
                    self.assertIs(reply, action)
                    self.assertEqual(len(provider.calls), 1)
                else:
                    self.assertTrue(reply.ok)
                    self.assertEqual(provider.calls[1][0][0]["content"], "compacted [hook]")

    async def test_fallback_constructor_reuses_client_for_all_providers(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as http:
            for cls in PROVIDERS:
                provider = fallback_provider._create_fallback_provider(
                    provider_type=cls.provider_name, model="test-model", api_key="test-key",
                    base_url="https://api.example", http=http,
                )
                self.assertIsInstance(provider, cls)
                self.assertIs(provider._http, http)

    async def test_fallback_attempts_get_fresh_history_hooks_and_actual_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = RecordingProvider()
            first = RecordingProvider([ProviderResponse(ok=False, error="busy", status_code=503)])
            second = RecordingProvider([ProviderResponse(ok=True, text="ok", provider_meta={"raw_parts": [{"text": "ok"}]})])
            first.name, second.name = "attempt-1", "attempt-2"
            ls = loop_state(Path(tmp), primary)
            image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
            ls.messages[-2]["content"] = [image]
            before = copy.deepcopy(ls.messages)
            ctx = HookContext(messages=ls.messages, tools=[], node=ls.node, provider=primary, rctx=ls.rctx, response=ProviderResponse(ok=False, error="busy", status_code=503), extra={"loop_state": ls, "call_provider": lambda provider, **kwargs: llm_call.call_provider_once(ls, 0, provider, **kwargs)})
            config = {"fallbacks": [{"provider": "test", "base_url": "https://api.example", "api_key": "test-key"}] * 2}
            seen = []

            async def hook(name, request):
                seen.append(request.provider.name)
                self.assertEqual(request.messages[0]["content"], "question")
                self.assertNotIn("mutated", request.messages[1]["_meta"])
                request.messages[0]["content"] += " [hook]"
                return HookResult()

            with patch.object(fallback_provider, "_load_config", return_value=config), patch.object(fallback_provider, "_create_fallback_provider", side_effect=[first, second]), patch.object(llm_call.hook_registry, "afire", side_effect=hook):
                result = await fallback_provider.FallbackProviderHandler().handle(ctx)
            self.assertTrue(result.modified)
            notice = result.channels["response_notices"][0]
            self.assertEqual(notice["code"], "fallback_provider.used")
            self.assertNotIn("test-key", json.dumps(notice))
            self.assertEqual(seen, ["attempt-1", "attempt-2"])
            self.assertEqual(first.calls[0][0][-1]["content"], [image])
            self.assertEqual(second.calls[0][0][-1]["content"], [image])
            self.assertIs(ctx.provider, second)
            self.assertIs(ls.provider, primary)
            self.assertEqual(ls.messages, before)
            self.assertTrue(ctx.response.ok)

    async def test_fallback_hook_action_stops_without_calling_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary, backup = RecordingProvider(), RecordingProvider()
            ls = loop_state(Path(tmp), primary)
            ctx = HookContext(messages=ls.messages, tools=[], node=ls.node, provider=primary, rctx=ls.rctx, response=ProviderResponse(ok=False, error="busy", status_code=503), extra={"loop_state": ls, "call_provider": lambda provider, **kwargs: llm_call.call_provider_once(ls, 0, provider, **kwargs)})
            config = {"fallbacks": [{"provider": "test", "base_url": "https://api.example", "api_key": "test-key"}]}
            action = TaskAction(action="finish", node_id="audit", result={"text": "stop"})
            with patch.object(fallback_provider, "_load_config", return_value=config), patch.object(fallback_provider, "_create_fallback_provider", return_value=backup), patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult(action=action))):
                result = await fallback_provider.FallbackProviderHandler().handle(ctx)
            self.assertIs(result.action, action)
            self.assertEqual(backup.calls, [])

    async def test_actual_response_source_is_saved_for_tools_and_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            ls = loop_state(Path(tmp), RecordingProvider(), messages=[])
            ls.response_provider = SimpleNamespace(name="actual-backup")
            ls.rctx.response_info = {"provider": "actual-backup"}
            response = ProviderResponse(ok=True, tool_calls=[ToolCall(id="f1", name="finish", arguments={"text": "ok"})], provider_meta={"signed": "value"}, reasoning="summary")
            stop = TaskAction(action="finish", node_id="audit", result={"text": "stop"})
            with patch.object(ai_step, "_shadow_write"), patch.object(ai_step.hook_registry, "afire", new=AsyncMock(return_value=HookResult(action=stop))) as hook:
                await ai_step._handle_tool_calls(ls, response, 0)
            self.assertIs(hook.call_args.args[1].provider, ls.response_provider)
            self.assertEqual(ls.messages[0]["_meta"]["provider"], "actual-backup")
            self.assertEqual(ls.messages[0]["_meta"]["metadata"], {"actual-backup": {"signed": "value"}})
            with patch.object(ai_step, "_shadow_write"), patch.object(plaintext, "_persist_ctx", return_value="ctx"):
                plaintext._build_implicit_finish(SimpleNamespace(step=0), ls, response, "answer")
            self.assertEqual(ls.messages[-1]["_meta"]["metadata"], {"actual-backup": {"signed": "value"}})
            self.assertEqual(ls.messages[-1]["_meta"]["reasoning"], "summary")

    async def test_error_snapshot_uses_public_builder_with_prepared_internal_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(OpenAIResponsesProvider, http=object())
            ls = loop_state(Path(tmp), provider)
            ls.request_history = history()
            ls.request_history[0]["content"] = "hook-expanded"
            ctx = HookContext(messages=ls.messages, tools=[], node=ls.node, provider=provider, rctx=ls.rctx, response=ProviderResponse(ok=False, error="test", status_code=400), extra={"loop_state": ls})
            with patch.object(provider, "build_payload", wraps=provider.build_payload) as builder:
                await ErrorSnapshotHandler().handle(ctx)
            self.assertEqual(builder.call_count, 1)
            files = list((Path(tmp) / "data/llm_error_snapshots").glob("*.json"))
            self.assertEqual(len(files), 1)
            snapshot = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertTrue(snapshot["converted_payload_reconstructed"])
            self.assertIn("input", snapshot["converted_payload"])
            self.assertIn("hook-expanded", json.dumps(snapshot["converted_payload"]))


if __name__ == "__main__":
    unittest.main()
