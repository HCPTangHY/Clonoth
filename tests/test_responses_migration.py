"""Responses migration regressions; all transport uses local MockTransport.

Run: python3.11 -B tests/test_responses_migration.py
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.openai_responses import OpenAIResponsesProvider


def provider(http=None, **options):
    return OpenAIResponsesProvider(
        http=http, api_key="test-key", base_url="https://example.invalid/v1",
        model="test-model", provider_options=options,
    )


def call(call_id="call_a", name="lookup", arguments=None):
    return {"id": call_id, "name": name, "arguments": arguments or {"value": "same"}}


def output_call(call_id="call_a", name="lookup", arguments=None):
    return {
        "type": "function_call", "id": "fc_" + call_id,
        "call_id": call_id, "name": name,
        "arguments": json.dumps(arguments or {"value": "same"}),
    }


def native_history(parallel=False):
    calls = [call()]
    raw = [
        {"type": "reasoning", "id": "rs_a", "encrypted_content": "ENCRYPTED",
         "content": [{"type": "reasoning_text", "text": "BODY"}],
         "summary": [{"type": "summary_text", "text": "SUMMARY"}]},
        output_call(),
    ]
    if parallel:
        calls.append(call("call_b"))
        raw.append(output_call("call_b"))
    history = [{
        "role": "assistant", "content": "", "tool_calls": calls,
        "_meta": {
            "provider": "openai-responses", "tool_mode": "native",
            "tool_call_ids": [item["id"] for item in calls],
            "reasoning": "DISPLAY_NOT_REPLAYED",
            "metadata": {"openai-responses": {"raw_output": raw}},
        },
    }]
    history.extend({"role": "tool", "tool_call_id": item["id"], "name": item["name"], "content": "result-" + item["id"]} for item in calls)
    return history


def completion(raw=None):
    return {"output": raw if raw is not None else [
        {"type": "reasoning", "id": "rs_new", "encrypted_content": "NEW_ENCRYPTED",
         "content": [{"type": "reasoning_text", "text": "B1B2"}],
         "summary": [{"type": "summary_text", "text": "S1S2"}]},
        {"type": "message", "id": "msg_a", "role": "assistant",
         "content": [{"type": "output_text", "text": "answer"}]},
    ], "usage": {"input_tokens": 10, "output_tokens": 4}}


def sse(events):
    return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))


class ResponsesPayloadTests(unittest.TestCase):
    def test_cache_key_uses_sticky_identity_with_stable_length_boundary(self):
        for raw in (None, "", " \n\t", " session-id ", "x" * 64, "x" * 65, "会话" * 40):
            with self.subTest(raw=raw):
                normalized = str(raw or "").strip()
                expected = (hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
                            if len(normalized) > 64 else normalized)
                options = {"_sticky_session_id": raw, "sticky_session": {"enabled": True}}
                before = copy.deepcopy(options)
                instance = provider(**options)
                self.assertEqual(instance._cache_key(), expected)
                self.assertEqual(provider(**options)._cache_key(), expected)
                for stream in (False, True):
                    payload = instance.build_payload([], stream=stream)
                    self.assertEqual(payload.get("prompt_cache_key"), expected or None)
                    self.assertNotIn("_sticky_session_id", payload)
                    self.assertNotIn("sticky_session", payload)
                self.assertEqual(options, before)
                self.assertEqual(instance._options, before)
        self.assertNotEqual(provider(_sticky_session_id="x" * 65)._cache_key(),
                            provider(_sticky_session_id="x" * 64 + "y")._cache_key())
        self.assertNotIn("prompt_cache_key", provider().build_payload([]))

    def test_legacy_ids_do_not_collide_with_complete_or_future_calls(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "complete", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "complete", "content": "real output"},
            {"role": "user", "content": '[Tool call history record: legacy was executed with args: {}]'},
            {"role": "user", "content": 'Tool result for "legacy":\nlegacy output'},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2", "name": "future", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "call_2", "name": "future", "content": "future output"},
        ]
        before = copy.deepcopy(messages)
        items = provider().build_payload(messages)["input"]
        calls = [item["call_id"] for item in items if item.get("type") == "function_call"]
        results = [item["call_id"] for item in items if item.get("type") == "function_call_output"]
        self.assertEqual(calls, ["call_1", "call_3", "call_2"])
        self.assertEqual(calls, results)
        self.assertEqual(messages, before)

    def test_unmatched_later_call_cannot_modify_an_earlier_same_id_turn(self):
        # Exercise the converter directly: normal history preparation already
        # removes this incomplete native call before conversion.
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "same", "name": "complete", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "same", "name": "complete", "content": "real output"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "same", "name": "later", "arguments": {}}]},
            {"role": "user", "content": "continue"},
        ]
        _, items = OpenAIResponsesProvider._convert_messages(messages)
        self.assertEqual(items[0]["type"], "function_call")
        self.assertEqual(items[0]["name"], "complete")
        self.assertEqual(items[1]["type"], "function_call_output")
        self.assertEqual(items[2]["type"], "message")

    def test_defaults_leave_optional_parameters_unset(self):
        payload = provider().build_payload([{"role": "user", "content": "hello"}])
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])

    def test_maximum_token_aliases_and_invalid_values(self):
        for options, expected in (
            ({"max_tokens": 128}, 128),
            ({"max_completion_tokens": "64"}, 64),
            ({"max_tokens": 128, "max_completion_tokens": 64}, 128),
            ({"max_tokens": None, "max_completion_tokens": 64}, 64),
            ({"max_tokens": "invalid", "max_completion_tokens": 64}, None),
            ({"max_tokens": 0}, None),
            ({"max_tokens": -1}, None),
            ({"max_tokens": float("inf")}, None),
        ):
            with self.subTest(options=options):
                payload = provider(**options).build_payload([])
                self.assertEqual(payload.get("max_output_tokens"), expected)

    def test_summary_aliases_and_nested_options_are_isolated(self):
        for value in (True, "true", "1", "yes", "auto", " AUTO ", "concise", "detailed"):
            with self.subTest(value=value):
                expected = value if value in ("concise", "detailed") else "auto"
                options = {"reasoning": {"effort": "low", "summary": "concise"}, "reasoning_effort": " HIGH ", "thought_summary": value}
                before = copy.deepcopy(options)
                payload = provider(**options).build_payload([])
                self.assertEqual(payload["reasoning"], {"effort": "high", "summary": expected})
                payload["reasoning"]["effort"] = "changed"
                self.assertEqual(options, before)
        for value in (False, None, "false", "off", 0):
            payload = provider(reasoning={"summary": "auto"}, thought_summary=value).build_payload([])
            self.assertNotIn("reasoning", payload)
        for options in ({"reasoning_effort": "NONE", "thought_summary": True}, {"reasoning": {"effort": "NONE", "summary": "auto"}}):
            self.assertNotIn("summary", provider(**options).build_payload([])["reasoning"])
        self.assertEqual(provider(reasoning={"summary": "detailed"}).build_payload([])["reasoning"], {"summary": "detailed"})

    def test_subclass_defaults_are_supported(self):
        class Configured(OpenAIResponsesProvider):
            default_max_tokens = 256
            default_reasoning_effort = "medium"
            default_thought_summary = True

        instance = Configured(http=None, api_key="test", base_url=None, model="test")
        payload = instance.build_payload([])
        self.assertEqual(payload["max_output_tokens"], 256)
        self.assertEqual(payload["reasoning"], {"effort": "medium", "summary": "auto"})

    def test_developer_and_system_messages_become_instructions(self):
        payload = provider().build_payload([
            {"role": "system", "content": "first"},
            {"role": "developer", "content": "second"},
            {"role": "user", "content": "hello"},
        ])
        self.assertEqual(payload["instructions"], "first\n\nsecond")
        self.assertEqual(len(payload["input"]), 1)

    def test_complete_raw_output_uses_call_id_and_preserves_all_data(self):
        for mode in ("native", "fake-native"):
            messages = native_history(parallel=True)
            before = copy.deepcopy(messages)
            payload = provider().build_payload(messages, tool_mode=mode)
            raw = messages[0]["_meta"]["metadata"]["openai-responses"]["raw_output"]
            self.assertEqual(payload["input"][:len(raw)], raw)
            self.assertEqual([item["call_id"] for item in payload["input"] if item["type"] == "function_call_output"], ["call_a", "call_b"])
            self.assertNotIn("DISPLAY_NOT_REPLAYED", json.dumps(payload))
            payload["input"][0]["summary"][0]["text"] = "REQUEST_ONLY_CHANGE"
            self.assertEqual(messages, before)

    def test_incomplete_parallel_raw_turn_never_revives_a_call(self):
        for kept_id in ("call_a", "call_b"):
            for mode in ("native", "fake-native"):
                messages = native_history(parallel=True)
                messages = [messages[0]] + [item for item in messages[1:] if item["tool_call_id"] == kept_id]
                before = copy.deepcopy(messages)
                payload = provider().build_payload(messages, tool_mode=mode)
                self.assertFalse(any(item["type"] in {"reasoning", "function_call", "function_call_output"} for item in payload["input"]))
                encoded = json.dumps(payload)
                self.assertNotIn("ENCRYPTED", encoded)
                self.assertEqual(encoded.count("result-" + kept_id), 1)
                self.assertNotIn("No tool result", encoded)
                self.assertEqual(messages, before)

    def test_stale_and_malformed_raw_calls_are_downgraded(self):
        for damage in ("call_id", "name", "arguments", "argument_types", "invalid_arguments", "missing_call", "malformed_item", "non_list", "stale_ids"):
            with self.subTest(damage=damage):
                messages = native_history()
                raw = messages[0]["_meta"]["metadata"]["openai-responses"]["raw_output"]
                if damage == "call_id":
                    raw[1]["call_id"] = "other_call"
                elif damage == "name":
                    raw[1]["name"] = "other_tool"
                elif damage == "arguments":
                    raw[1]["arguments"] = '{"value":"different"}'
                elif damage == "argument_types":
                    raw[1]["arguments"] = '{"value":0}'
                    messages[0]["tool_calls"][0]["arguments"] = {"value": False}
                elif damage == "invalid_arguments":
                    raw[1]["arguments"] = "{broken"
                elif damage == "missing_call":
                    raw.pop()
                elif damage == "malformed_item":
                    raw.append(None)
                elif damage == "non_list":
                    messages[0]["_meta"]["metadata"]["openai-responses"]["raw_output"] = "broken"
                else:
                    messages[0]["_meta"]["tool_call_ids"] = ["removed_call", "call_a"]
                before = copy.deepcopy(messages)
                payload = provider().build_payload(messages)
                self.assertFalse(any(item["type"] in {"reasoning", "function_call", "function_call_output"} for item in payload["input"]))
                self.assertEqual(json.dumps(payload).count("result-call_a"), 1)
                self.assertEqual(messages, before)

    def test_duplicate_ids_are_not_replayed_with_or_without_cache(self):
        for cache in (True, False):
            messages = native_history()
            messages[0]["tool_calls"].append(copy.deepcopy(messages[0]["tool_calls"][0]))
            messages[0]["_meta"]["tool_call_ids"].append("call_a")
            if cache:
                messages[0]["_meta"]["metadata"]["openai-responses"]["raw_output"].append(output_call())
            else:
                messages[0]["_meta"].pop("metadata")
            before = copy.deepcopy(messages)
            payload = provider().build_payload(messages)
            self.assertFalse(any(item["type"] in {"function_call", "function_call_output"} for item in payload["input"]))
            self.assertEqual(json.dumps(payload).count("result-call_a"), 1)
            self.assertEqual(messages, before)

    def test_damaged_turn_does_not_poison_later_turn_with_reused_ids(self):
        damaged = native_history(parallel=True)
        damaged.pop()
        messages = damaged + native_history()
        payload = provider().build_payload(messages)
        calls = [item for item in payload["input"] if item["type"] == "function_call"]
        outputs = [item for item in payload["input"] if item["type"] == "function_call_output"]
        self.assertEqual([item["call_id"] for item in calls], ["call_a"])
        self.assertEqual([item["call_id"] for item in outputs], ["call_a"])

    def test_converter_rejects_orphan_results_and_never_synthesizes_outputs(self):
        _, items = provider()._convert_messages([
            {"role": "tool", "tool_call_id": "orphan", "content": "orphan"},
            {"role": "tool", "content": "unidentified orphan"},
            {"role": "assistant", "tool_calls": [call()], "content": ""},
            {"role": "tool", "tool_call_id": "wrong", "content": "wrong"},
            {"role": "user", "content": "next"},
        ])
        self.assertFalse(any(item["type"] in {"function_call", "function_call_output"} for item in items))
        self.assertIn("lookup", json.dumps(items))
        self.assertNotIn("orphan", json.dumps(items))
        self.assertNotIn("No tool result", json.dumps(items))

    def test_unmatched_legacy_text_call_remains_text(self):
        messages = [{"role": "user", "content": '[Tool call history record: lookup was executed with args: {"value": 1}]'}, {"role": "user", "content": "next"}]
        payload = provider().build_payload(messages)
        self.assertFalse(any(item["type"] in {"function_call", "function_call_output"} for item in payload["input"]))
        self.assertIn("lookup", json.dumps(payload))

    def test_json_mode_does_not_replay_cached_output(self):
        payload = provider().build_payload(native_history(), tool_mode="json")
        self.assertNotIn("ENCRYPTED", json.dumps(payload))
        self.assertIn("<<<TOOL_CALL>>>", json.dumps(payload))


class ResponsesTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_key_is_in_actual_json_for_both_transports_and_retries(self):
        for stream in (False, True):
            for failure in (None, "http", "stream") if stream else (None, "http"):
                for raw in (None, "node:coder", "parent:bootstrap.coder:" + "context" * 10):
                    with self.subTest(stream=stream, failure=failure, raw=raw):
                        sent = []

                        def handle(request):
                            sent.append(json.loads(request.content))
                            if failure and len(sent) == 1:
                                error = {"message": "encrypted content could not be verified"}
                                if failure == "http":
                                    return httpx.Response(400, json={"error": error})
                                return sse([{"type": "response.failed", "response": {"error": error}}])
                            body = completion()
                            return sse([{"type": "response.completed", "response": body}]) if stream else httpx.Response(200, json=body)

                        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                            instance = provider(http, _sticky_session_id=raw)
                            response = await (instance.chat_stream if stream else instance.chat)(messages=native_history())
                        self.assertTrue(response.ok, response.error)
                        self.assertEqual(len(sent), 2 if failure else 1)
                        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] if raw and len(raw) > 64 else raw
                        for body in sent:
                            self.assertEqual(body.get("prompt_cache_key"), expected)
                            if not raw:
                                self.assertNotIn("prompt_cache_key", body)
                            self.assertEqual(body["stream"], stream)
                            self.assertNotIn("_sticky_session_id", body)

    async def test_nonstream_unifies_body_and_summary_without_trimming_raw_output(self):
        body = completion()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as http:
            response = await provider(http).chat(messages=[])
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.reasoning, "B1B2S1S2")
        self.assertEqual(response.text, "answer")
        self.assertEqual(response.provider_meta["raw_output"], body["output"])
        self.assertFalse(hasattr(response, "thought_summary"))

    async def test_completed_only_fills_display_and_call_id(self):
        body = completion()
        body["output"].append(output_call("call_new"))
        chunks, texts = [], []

        async def thinking(text):
            chunks.append(text)

        async def text(value):
            texts.append(value)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sse([{"type": "response.completed", "response": body}]))) as http:
            response = await provider(http).chat_stream(messages=[], on_text=text, on_thinking=thinking)
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.reasoning, "B1B2S1S2")
        self.assertEqual("".join(chunks), response.reasoning)
        self.assertEqual("".join(texts), "answer")
        self.assertEqual(response.provider_meta["raw_output"], body["output"])
        self.assertEqual(response.tool_calls[0].id, "call_new")
        self.assertNotEqual(response.tool_calls[0].id, body["output"][-1]["id"])

    async def test_interleaved_deltas_keep_arrival_order_and_completed_does_not_duplicate(self):
        body = completion()
        events = [
            {"type": "response.reasoning_text.delta", "delta": "B1"},
            {"type": "response.reasoning_summary_text.delta", "delta": "S1"},
            {"type": "response.reasoning_text.delta", "delta": "B2"},
            {"type": "response.reasoning_summary_text.delta", "delta": "S2"},
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed", "response": body},
        ]
        chunks = []

        async def thinking(text):
            chunks.append(text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sse(events))) as http:
            response = await provider(http).chat_stream(messages=[], on_thinking=thinking)
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.reasoning, "B1S1B2S2")
        self.assertEqual("".join(chunks), response.reasoning)
        self.assertEqual(response.text, "answer")
        self.assertEqual(response.provider_meta["raw_output"], body["output"])

    async def test_completion_fills_missing_channel_and_partial_tail(self):
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": "S1"},
            {"type": "response.output_text.delta", "delta": "ans"},
            {"type": "response.completed", "response": completion()},
        ]
        chunks = []

        async def thinking(text):
            chunks.append(text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sse(events))) as http:
            response = await provider(http).chat_stream(messages=[], on_thinking=thinking)
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.reasoning, "S1B1B2S2")
        self.assertEqual("".join(chunks), response.reasoning)
        self.assertEqual(response.text, "answer")

    async def test_http_signature_retry_preserves_text_and_history(self):
        for stream in (False, True):
            sent = []
            body = completion()

            def handle(request):
                sent.append(json.loads(request.content))
                if len(sent) == 1:
                    return httpx.Response(400, json={"error": {"message": "encrypted content could not be verified"}})
                return sse([{"type": "response.completed", "response": body}]) if stream else httpx.Response(200, json=body)

            history = native_history()
            before = copy.deepcopy(history)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                instance = provider(http)
                method = instance.chat_stream if stream else instance.chat
                response = await method(messages=history)
            self.assertTrue(response.ok, response.error)
            self.assertEqual(len(sent), 2)
            original_reasoning, retried_reasoning = sent[0]["input"][0], sent[1]["input"][0]
            self.assertIn("encrypted_content", original_reasoning)
            self.assertNotIn("encrypted_content", retried_reasoning)
            self.assertEqual(retried_reasoning["content"], original_reasoning["content"])
            self.assertEqual(retried_reasoning["summary"], original_reasoning["summary"])
            self.assertEqual(history, before)

    async def test_retry_is_bounded_for_http_and_stream_failed(self):
        for failure in ("http", "failed"):
            for stream in ((False, True) if failure == "http" else (True,)):
                sent = []

                def handle(request):
                    sent.append(json.loads(request.content))
                    error = {"message": "encrypted_content could not be decrypted"}
                    if failure == "http":
                        return httpx.Response(400, json={"error": error})
                    return sse([{"type": "response.failed", "response": {"error": error}}])

                history = native_history()
                before = copy.deepcopy(history)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    instance = provider(http)
                    response = await (instance.chat_stream if stream else instance.chat)(messages=history)
                self.assertFalse(response.ok)
                self.assertIn("could not be decrypted", response.error)
                self.assertEqual(len(sent), 2)
                self.assertEqual(history, before)

    async def test_failed_event_before_visible_output_can_retry(self):
        sent, chunks = [], []

        def handle(request):
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return sse([{"type": "response.failed", "response": {"error": {"message": "encrypted content could not be verified"}}}])
            return sse([{"type": "response.completed", "response": completion()}])

        async def thinking(text):
            chunks.append(text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            response = await provider(http).chat_stream(messages=native_history(), on_thinking=thinking)
        self.assertTrue(response.ok, response.error)
        self.assertEqual(len(sent), 2)
        self.assertEqual("".join(chunks), "B1B2S1S2")

    async def test_failed_event_after_visible_output_never_retries(self):
        for kind in ("text", "body", "summary", "tool"):
            with self.subTest(kind=kind):
                sent, visible = [], []
                first = {
                    "text": {"type": "response.output_text.delta", "delta": "partial"},
                    "body": {"type": "response.reasoning_text.delta", "delta": "partial"},
                    "summary": {"type": "response.reasoning_summary_text.delta", "delta": "partial"},
                    "tool": {"type": "response.output_item.added", "output_index": 0, "item": output_call()},
                }[kind]

                def handle(request):
                    sent.append(json.loads(request.content))
                    return sse([first, {"type": "response.failed", "response": {"error": {"message": "encrypted content could not be verified"}}}])

                async def callback(value):
                    visible.append(value)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    response = await provider(http).chat_stream(messages=native_history(), on_text=callback, on_thinking=callback, on_tool_delta=callback)
                self.assertFalse(response.ok)
                self.assertEqual(len(sent), 1)
                self.assertEqual(len(visible), 1)
                self.assertEqual(response.tool_calls, [])

    async def test_unrelated_errors_do_not_trigger_signature_retry(self):
        for stream in (False, True):
            sent = []

            def handle(request):
                sent.append(json.loads(request.content))
                return httpx.Response(400, json={"error": {"message": "invalid tool schema"}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                instance = provider(http)
                response = await (instance.chat_stream if stream else instance.chat)(messages=native_history())
            self.assertFalse(response.ok)
            self.assertEqual(len(sent), 1)
            self.assertEqual(response.status_code, 400)

    async def test_incomplete_and_truncated_streams_fail(self):
        for events in (
            [{"type": "response.output_text.delta", "delta": "partial"}],
            [{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}],
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sse(events))) as http:
                response = await provider(http).chat_stream(messages=[])
            self.assertFalse(response.ok)
            self.assertEqual(response.tool_calls, [])

    async def test_output_item_done_and_done_marker_preserve_complete_raw_items(self):
        items = completion()["output"] + [output_call("call_done")]
        wire = "".join("data: " + json.dumps({"type": "response.output_item.done", "output_index": index, "item": item}) + "\n\n" for index, item in enumerate(items)) + "data: [DONE]\n\n"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=wire))) as http:
            response = await provider(http).chat_stream(messages=[])
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.reasoning, "B1B2S1S2")
        self.assertEqual(response.text, "answer")
        self.assertEqual(response.tool_calls[0].id, "call_done")
        self.assertEqual(response.provider_meta["raw_output"], items)

    async def test_failed_and_invalid_nonstream_bodies_return_errors(self):
        for body in (None, [], {"status": "failed", "error": {"message": "failure"}}, {"status": "incomplete"}, {"output": "invalid"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as http:
                response = await provider(http).chat(messages=[])
            self.assertFalse(response.ok)


if __name__ == "__main__":
    unittest.main()
