"""Anthropic production migration regression tests; no real HTTP requests.

Run: python3.11 -B tests/test_anthropic_migration.py
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.anthropic import AnthropicProvider, _convert_messages, _parse_response_content, _parse_usage
from providers.tool_format import NativeToolFormatter


THINKING = {"type": "thinking", "thinking": "  original reasoning\n", "signature": "signed-content", "extension": {"kept": True}}
REDACTED = {"type": "redacted_thinking", "data": " opaque data ", "extension": [1, 2]}
USAGE = {"input_tokens": 11, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30, "output_tokens": 7}
EXPECTED_USAGE = {"prompt_tokens": 61, "completion_tokens": 7, "total_tokens": 68, "cached_prompt_tokens": 20, "cache_write_prompt_tokens": 30}


def provider(http=None, *, options=None, model="model-a"):
    return AnthropicProvider(http=http, api_key="test-key", base_url="https://api.example", model=model, provider_options=options)


def history(*, model="model-a", last="continue"):
    private = {"thinking_blocks": copy.deepcopy([THINKING, REDACTED])}
    if model is not None:
        private["model"] = model
    return [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer", "_meta": {
            "provider": "anthropic", "tool_mode": "native", "reasoning": "DISPLAY_ONLY",
            "metadata": {"anthropic": private},
        }},
        {"role": "user", "content": last},
    ]


def thinking_blocks(payload):
    return [
        block for row in payload["messages"] for block in row["content"]
        if block.get("type") in {"thinking", "redacted_thinking"}
    ]


def success_response(stream=False):
    if not stream:
        return httpx.Response(200, json={
            "content": [copy.deepcopy(THINKING), copy.deepcopy(REDACTED), {"type": "text", "text": "answer"}],
            "usage": USAGE,
        })
    initial = copy.deepcopy(THINKING)
    initial["thinking"] = "  original "
    initial["signature"] = "signed-"
    events = [
        {"type": "message_start", "message": {"usage": dict(USAGE, output_tokens=0)}},
        {"type": "content_block_start", "index": 0, "content_block": initial},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning\n"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "con"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "tent"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": copy.deepcopy(REDACTED)},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_delta", "usage": {"output_tokens": 7, "input_tokens": 0, "cache_read_input_tokens": 0}},
        {"type": "message_stop"},
    ]
    return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))


class AnthropicPayloadMigrationTests(unittest.TestCase):
    def test_production_defaults_and_token_aliases(self):
        for options, expected in (
            ({}, 64000),
            ({"max_tokens": 123}, 123),
            ({"max_completion_tokens": "321"}, 321),
            ({"max_tokens": 123, "max_completion_tokens": 321}, 123),
            ({"max_tokens": "invalid"}, 64000),
            ({"max_tokens": -1}, 64000),
        ):
            with self.subTest(options=options):
                payload = provider(options=options).build_payload(history())
                self.assertEqual(payload["max_tokens"], expected)
                self.assertEqual(payload["thinking"], {"type": "adaptive", "display": "summarized"})
                self.assertNotIn("max_completion_tokens", payload)

    def test_reasoning_effort_and_output_config_do_not_mutate_options(self):
        options = {"reasoning_effort": " HIGH ", "thought_summary": "off", "output_config": {"format": {"type": "text"}}, "thinking": {"budget_tokens": 1024}}
        before = copy.deepcopy(options)
        instance = provider(options=options)
        payload = instance._build_payload(instance.prepare_history(history()), temperature=0.5)
        self.assertEqual(payload["thinking"], {"type": "adaptive", "display": "omitted"})
        self.assertEqual(payload["output_config"], {"format": {"type": "text"}, "effort": "high"})
        self.assertNotIn("temperature", payload)
        payload["output_config"]["format"]["type"] = "changed"
        self.assertEqual(options, before)
        self.assertEqual(instance._options, before)

    def test_none_effort_disables_thinking_without_losing_other_output_options(self):
        instance = provider(options={
            "reasoning_effort": "None", "thought_summary": True,
            "thinking": {"type": "enabled", "budget_tokens": 1024, "display": "summarized"},
            "output_config": {"effort": "high", "format": {"type": "text"}},
        })
        payload = instance.build_payload(history())
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["output_config"], {"format": {"type": "text"}})

    def test_explicit_enabled_budget_is_not_removed(self):
        options = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        payload = provider(options=options).build_payload(history())
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 2048, "display": "summarized"})
        self.assertEqual(options, {"thinking": {"type": "enabled", "budget_tokens": 2048}})

    def test_model_boundaries_keep_private_data_unchanged(self):
        for saved_model in ("model-a", "model-b", None):
            messages = history(model=saved_model)
            before = copy.deepcopy(messages)
            payload = provider().build_payload(messages)
            self.assertEqual(thinking_blocks(payload), [] if saved_model == "model-b" else [THINKING, REDACTED])
            self.assertNotIn("DISPLAY_ONLY", json.dumps(payload))
            self.assertEqual(messages, before)

    def test_plaintext_modes_do_not_replay_native_thinking(self):
        for mode in ("json", "fake-native"):
            payload = provider().build_payload(history(), tool_mode=mode)
            self.assertEqual(thinking_blocks(payload), [])

    def test_consecutive_signed_assistants_keep_separate_block_boundaries(self):
        messages = history()[:2]
        second = copy.deepcopy(messages[1])
        second["_meta"]["metadata"]["anthropic"]["thinking_blocks"][0]["signature"] = "second-signature"
        messages.extend([second, {"role": "user", "content": "continue"}])
        before = copy.deepcopy(messages)
        payload = provider().build_payload(messages)
        self.assertEqual([row["role"] for row in payload["messages"]], ["user", "assistant", "user", "assistant", "user"])
        assistants = [row for row in payload["messages"] if row["role"] == "assistant"]
        self.assertEqual(assistants[0]["content"][0], THINKING)
        self.assertEqual(assistants[1]["content"][0]["signature"], "second-signature")
        self.assertEqual(assistants[1]["content"][0]["thinking"], THINKING["thinking"])
        self.assertEqual(messages, before)

    def test_tool_results_precede_text_in_merged_user_message(self):
        messages = history()[:2]
        messages[1]["tool_calls"] = [{"id": "call-1", "name": "lookup", "arguments": {}}]
        messages.extend([
            {"role": "tool", "tool_call_id": "call-1", "name": "lookup", "content": "result"},
            {"role": "user", "content": "follow-up"},
        ])
        before = copy.deepcopy(messages)
        payload = provider().build_payload(messages)
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual([block["type"] for block in payload["messages"][-1]["content"]], ["tool_result", "text"])
        self.assertEqual(messages, before)

    def test_conversion_does_not_interleave_results_after_text(self):
        messages = [
            {"role": "tool", "tool_call_id": "first", "content": "result one"},
            {"role": "user", "content": "text"},
            {"role": "tool", "tool_call_id": "second", "content": "result two"},
        ]
        _, converted = _convert_messages(messages)
        self.assertEqual([row["role"] for row in converted], ["user", "assistant", "user"])
        self.assertEqual([block["type"] for block in converted[0]["content"]], ["tool_result", "text"])
        self.assertEqual(converted[2]["content"][0]["tool_use_id"], "second")

    def test_orphan_call_is_not_recreated_or_given_fabricated_result(self):
        messages = history()
        messages[1]["tool_calls"] = [{"id": "removed", "name": "lookup", "arguments": {}}]
        payload = provider().build_payload(messages)
        blocks = [block for row in payload["messages"] for block in row["content"]]
        self.assertFalse(any(block.get("type") in {"tool_use", "tool_result"} for block in blocks))
        self.assertNotIn("no tool output recorded", json.dumps(payload))

    def test_parse_usage_includes_both_cache_counts(self):
        self.assertEqual(_parse_usage({"usage": USAGE}), EXPECTED_USAGE)
        self.assertEqual(_parse_usage({"usage": {"input_tokens": None, "cache_read_input_tokens": "20", "output_tokens": -1}}), {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })

    def test_response_parser_retains_raw_blocks_independently_of_source(self):
        content = copy.deepcopy([THINKING, REDACTED])
        _, reasoning, _, private = _parse_response_content(content)
        self.assertEqual(reasoning, THINKING["thinking"])
        self.assertEqual(private, [THINKING, REDACTED])
        content[0]["extension"]["kept"] = False
        self.assertEqual(private, [THINKING, REDACTED])


class AnthropicTransportMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_nonstream_bodies_are_errors(self):
        for body in ("not JSON", "[]", '{"content": null}', '{"content": [null]}'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as http:
                response = await provider(http).chat(messages=[])
            self.assertFalse(response.ok)
            self.assertIn("Invalid Anthropic response", response.error)

    async def test_truncated_or_unfinished_signed_stream_does_not_succeed(self):
        for events in (
            [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}],
            [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "partial"}}, {"type": "message_stop"}],
            [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "partial", "name": "lookup"}}, {"type": "message_stop"}],
        ):
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as http:
                response = await provider(http).chat_stream(messages=[])
            self.assertFalse(response.ok)
            self.assertEqual(response.tool_calls, [])

    async def test_stream_and_nonstream_keep_complete_signed_blocks_and_usage(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: success_response(stream))) as http:
                    instance = provider(http)
                    chunks = []

                    async def on_thinking(text):
                        chunks.append(text)

                    if stream:
                        response = await instance.chat_stream(messages=history(), on_thinking=on_thinking)
                        self.assertEqual("".join(chunks), THINKING["thinking"])
                    else:
                        response = await instance.chat(messages=history())
                    self.assertTrue(response.ok, response.error)
                    self.assertEqual(response.reasoning, THINKING["thinking"])
                    self.assertEqual(response.provider_meta, {"thinking_blocks": [THINKING, REDACTED], "model": "model-a"})
                    self.assertEqual(response.usage, EXPECTED_USAGE)
                    self.assertFalse(hasattr(response, "thought_summary"))
                    saved = NativeToolFormatter().build_assistant_message(response, response.text, [])
                    saved["_meta"] = {"reasoning": "CHANGED_DISPLAY", "metadata": {"anthropic": response.provider_meta}}
                    before = copy.deepcopy(saved)
                    payload = instance.build_payload([saved, {"role": "user", "content": "continue"}])
                    self.assertEqual(thinking_blocks(payload), [THINKING, REDACTED])
                    self.assertNotIn("CHANGED_DISPLAY", json.dumps(payload))
                    self.assertEqual(saved, before)

    async def test_signature_retry_only_changes_second_request_copy(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                sent = []

                def handle(request):
                    sent.append(json.loads(request.content))
                    if len(sent) == 1:
                        return httpx.Response(400, json={"error": {"message": "Invalid signature in thinking block"}})
                    return success_response(stream)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    instance = provider(http)
                    messages = history()
                    before = copy.deepcopy(messages)
                    with patch.object(instance, "_dump_error_payload") as dump, patch.object(instance, "build_payload", wraps=instance.build_payload) as builder:
                        method = instance.chat_stream if stream else instance.chat
                        response = await method(messages=messages, tool_mode="native")
                    self.assertTrue(response.ok, response.error)
                    self.assertEqual(len(sent), 2)
                    self.assertEqual(builder.call_count, 1)
                    self.assertEqual(thinking_blocks(sent[0]), [THINKING, REDACTED])
                    self.assertEqual(thinking_blocks(sent[1]), [])
                    self.assertEqual(thinking_blocks(dump.call_args.args[2]), [THINKING, REDACTED])
                    self.assertEqual(messages, before)
                    self.assertEqual(sent[0]["thinking"], sent[1]["thinking"])
                    self.assertFalse(hasattr(instance, "_in_strip_thinking_retry"))

    async def test_retry_limit_and_unrelated_errors(self):
        for stream in (False, True):
            for status, message, expected_requests in (
                (400, "thinking.signature is invalid", 2),
                (400, "max_tokens must be positive", 1),
                (500, "invalid signature", 1),
            ):
                with self.subTest(stream=stream, status=status, message=message):
                    sent = []

                    def handle(request):
                        sent.append(json.loads(request.content))
                        return httpx.Response(status, json={"error": {"message": message}})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                        instance = provider(http)
                        with patch.object(instance, "_dump_error_payload"):
                            method = instance.chat_stream if stream else instance.chat
                            response = await method(messages=history())
                    self.assertFalse(response.ok)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.error, message)
                    self.assertEqual(len(sent), expected_requests)

    async def test_signature_retry_does_not_change_concurrent_request(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                retry_entered = asyncio.Event()
                release_retry = asyncio.Event()
                requests = {"A": [], "B": []}

                async def handle(request):
                    payload = json.loads(request.content)
                    label = payload["messages"][-1]["content"][-1]["text"]
                    requests[label].append(payload)
                    if label == "A" and len(requests[label]) == 1:
                        return httpx.Response(400, json={"error": {"message": "invalid signature"}})
                    if label == "A":
                        retry_entered.set()
                        await asyncio.wait_for(release_retry.wait(), timeout=2)
                    return success_response(stream)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    instance = provider(http)
                    first_history, second_history = history(last="A"), history(last="B")
                    before = copy.deepcopy([first_history, second_history])
                    method = instance.chat_stream if stream else instance.chat
                    with patch.object(instance, "_dump_error_payload"):
                        first = asyncio.create_task(method(messages=first_history))
                        await asyncio.wait_for(retry_entered.wait(), timeout=2)
                        try:
                            second = await method(messages=second_history)
                        finally:
                            release_retry.set()
                        first = await first
                    self.assertTrue(first.ok, first.error)
                    self.assertTrue(second.ok, second.error)
                    self.assertEqual([len(thinking_blocks(body)) for body in requests["A"]], [2, 0])
                    self.assertEqual([len(thinking_blocks(body)) for body in requests["B"]], [2])
                    self.assertEqual([first_history, second_history], before)

    async def test_stream_error_event_does_not_restart_after_output(self):
        sent = []

        def handle(request):
            sent.append(request)
            events = [
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "partial"}},
                {"type": "error", "error": {"message": "invalid signature"}},
            ]
            return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            chunks = []

            async def on_thinking(text):
                chunks.append(text)

            response = await provider(http).chat_stream(messages=history(), on_thinking=on_thinking)
        self.assertFalse(response.ok)
        self.assertEqual(chunks, ["partial"])
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
