"""Gemini production migration checks using only local MockTransport."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers import GeminiAIStudioProvider, GeminiVertexProvider
from providers.gemini import _clean_schema, _coalesce_stream_parts, _parse_usage
from test_reasoning_contract import stored_reply

CHANNELS = (GeminiAIStudioProvider, GeminiVertexProvider)


def provider(cls, http=None, **options):
    return cls(http=http, api_key="test-key", base_url="https://example.invalid", model="gemini-3-test", provider_options=options)


def signed_history(cls):
    return [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {}}], "_meta": {
            "provider": cls.provider_name, "tool_mode": "native", "tool_call_ids": ["call_1"],
            "metadata": {cls.provider_name: {"raw_parts": [
                {"thought": True, "text": "summary", "thoughtSignature": "summary-signature"},
                {"functionCall": {"name": "lookup", "args": {}}, "thoughtSignature": "call-signature"},
            ]}},
        }},
        {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "actual output"},
    ]


def model_parts(payload):
    return next(row["parts"] for row in payload["contents"] if row["role"] == "model")


def body(parts=None, **extra):
    return {"candidates": [{"content": {"parts": parts if parts is not None else [{"text": "answer"}]}, "finishReason": "STOP"}], **extra}


def sse(chunks):
    return "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("interrupted stream")

    async def aclose(self):
        self.closed = True


class GeminiPayloadMigrationTests(unittest.TestCase):
    def test_production_defaults_and_channel_specific_settings(self):
        for cls in CHANNELS:
            p = provider(cls)
            payload = p.build_payload([{"role": "developer", "content": "developer"}, {"role": "system", "content": "system"}, {"role": "user", "content": "question"}])
            self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "developer\n\nsystem"}])
            self.assertEqual(payload["generationConfig"], {"maxOutputTokens": 65535, "thinkingConfig": {"includeThoughts": True}})
            self.assertEqual(len(payload["safetySettings"]), 10 if cls is GeminiVertexProvider else 6)
            self.assertTrue(all(item["threshold"] == "OFF" for item in payload["safetySettings"]))
            self.assertEqual(payload["contents"][-1]["role"], "system" if cls is GeminiVertexProvider else "user")

    def test_configuration_precedence_and_input_isolation(self):
        options = {
            "max_output_tokens": 100, "max_tokens": 200, "temperature": 0, "top_p": 0.8, "top_k": 4,
            "reasoning_effort": "low", "thought_summary": False,
            "thinking_config": {"thinkingLevel": "HIGH", "thinkingBudget": 500, "includeThoughts": True},
            "safety_settings": [{"category": "custom", "threshold": "custom"}],
        }
        before = copy.deepcopy(options)
        for cls in CHANNELS:
            payload = provider(cls, **options).build_payload([])
            self.assertEqual(payload["generationConfig"], {
                "maxOutputTokens": 200, "temperature": 0, "topP": 0.8, "topK": 4,
                "thinkingConfig": {"thinkingLevel": "LOW"},
            })
            self.assertEqual(payload["safetySettings"], options["safety_settings"])
            payload["safetySettings"][0]["category"] = "changed"
            self.assertEqual(options, before)

    def test_disabled_summary_none_effort_and_invalid_limits(self):
        for cls in CHANNELS:
            for limit in (None, 0, -1, "invalid", float("inf")):
                payload = provider(cls, thought_summary="off", reasoning_effort="none", max_tokens=limit, safety_settings=[]).build_payload([])
                self.assertEqual(payload["generationConfig"], {"maxOutputTokens": 65535})
                self.assertEqual(payload["safetySettings"], [])
            self.assertEqual(provider(cls, max_output_tokens="128").build_payload([])["generationConfig"]["maxOutputTokens"], 128)

    def test_channel_cannot_be_changed_by_backend_option(self):
        for cls in CHANNELS:
            p = provider(cls, backend="aistudio" if cls is GeminiVertexProvider else "vertex")
            payload = p.build_payload([])
            self.assertEqual(p.is_vertex, cls is GeminiVertexProvider)
            self.assertEqual(len(payload["safetySettings"]), 10 if cls is GeminiVertexProvider else 6)

    def test_schema_unions_keep_types_and_nullable_information(self):
        schema = {"type": "object", "properties": {
            "nullable": {"type": ["string", "null"]},
            "union": {"type": ["string", "integer", "null"], "description": "multiple"},
        }}
        before = copy.deepcopy(schema)
        cleaned = _clean_schema(schema)
        self.assertEqual(cleaned["properties"]["nullable"], {"type": "string", "nullable": True})
        branches = cleaned["properties"]["union"]["anyOf"]
        self.assertEqual([branch["type"] for branch in branches], ["string", "integer", "null"])
        self.assertTrue(all(branch["description"] == "multiple" for branch in branches))
        self.assertEqual(schema, before)

    def test_tuple_schema_keeps_all_item_types_and_reports_relaxation(self):
        schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
        before = copy.deepcopy(schema)
        with self.assertLogs("providers.gemini", level="WARNING"):
            cleaned = _clean_schema(schema)
        self.assertEqual(cleaned["items"], {"anyOf": schema["items"]})
        self.assertEqual(schema, before)

    def test_signed_part_is_never_merged_even_at_end_of_stream(self):
        parts = [{"text": "A"}, {"text": "B", "thoughtSignature": "s1"}, {"text": "C"}, {"thoughtSignature": "s2"}, {"text": "D", "thoughtSignature": "s3"}]
        self.assertEqual(_coalesce_stream_parts(parts, [1] * len(parts)), parts)
        thoughts = [{"thought": True, "text": "A"}, {"thought": True, "text": "", "thoughtSignature": "s"}]
        self.assertEqual(_coalesce_stream_parts(thoughts, [1, 1]), thoughts)

    def test_only_unsigned_single_part_frames_are_coalesced(self):
        parts = [{"thought": True, "text": "A"}, {"thought": True, "text": "B"}, {"text": "C"}, {"text": "D"}]
        before = copy.deepcopy(parts)
        self.assertEqual(_coalesce_stream_parts(parts, [1, 1, 1, 1]), [{"thought": True, "text": "AB"}, {"text": "CD"}])
        for counts in (None, [4], [2, 2], [1], [True, 3]):
            self.assertEqual(_coalesce_stream_parts(parts, counts), parts)
        self.assertEqual(parts, before)

    def test_usage_counts_thoughts_and_limits_duplicate_workaround(self):
        self.assertEqual(_parse_usage({"promptTokenCount": 10, "toolUsePromptTokenCount": 2, "candidatesTokenCount": 4, "thoughtsTokenCount": 3, "cachedContentTokenCount": 1}), {
            "prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19, "cached_prompt_tokens": 1,
        })
        usage = {"promptTokenCount": 80000, "toolUsePromptTokenCount": 82000, "cachedContentTokenCount": 88000}
        for cls in CHANNELS:
            p = provider(cls)
            self.assertEqual(p._usage(usage)["prompt_tokens"], 162000)
            p.model = "gemini-4-argon"
            self.assertEqual(p._usage(usage)["prompt_tokens"], 88000 if cls is GeminiVertexProvider else 162000)


class GeminiTransportMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_signature_retry_is_bounded_and_keeps_stored_parts(self):
        for cls in CHANNELS:
            for stream in (False, True):
                messages = signed_history(cls)
                before = copy.deepcopy(messages)
                requests = []
                first_stream = BytesStream([json.dumps([{"error": {"message": "Invalid thought signature"}}]).encode()])

                def handler(request):
                    requests.append(json.loads(request.content))
                    if len(requests) == 1:
                        return httpx.Response(400, stream=first_stream)
                    self.assertTrue(first_stream.closed)
                    return httpx.Response(200, text=sse([body()])) if stream else httpx.Response(200, json=body())

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    p = provider(cls, http)
                    result = await (p.chat_stream if stream else p.chat)(messages=messages)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(len(requests), 2)
                self.assertEqual(model_parts(requests[0]), before[1]["_meta"]["metadata"][cls.provider_name]["raw_parts"])
                self.assertEqual(model_parts(requests[1]), [{"functionCall": {"name": "lookup", "args": {}}, "thoughtSignature": "skip_thought_signature_validator"}])
                self.assertEqual(messages, before)

    async def test_repeat_signature_errors_and_unrelated_errors_stop(self):
        for status, message, attempts in ((400, "Invalid thoughtSignature", 2), (400, "bad schema", 1), (403, "permission denied", 1)):
            for stream in (False, True):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, json=[{"error": {"message": message}}])

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    p = provider(GeminiAIStudioProvider, http)
                    result = await (p.chat_stream if stream else p.chat)(messages=signed_history(GeminiAIStudioProvider))
                self.assertFalse(result.ok)
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.error, message)
                self.assertEqual(len(requests), attempts)

    async def test_system_role_retry_applies_only_to_vertex(self):
        for cls in CHANNELS:
            for stream in (False, True):
                requests = []

                def handler(request):
                    requests.append(json.loads(request.content))
                    if len(requests) == 1:
                        return httpx.Response(400, json={"error": {"message": "role 'system' is not supported"}})
                    return httpx.Response(200, text=sse([body()])) if stream else httpx.Response(200, json=body())

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    p = provider(cls, http)
                    result = await (p.chat_stream if stream else p.chat)(messages=signed_history(cls))
                self.assertEqual(len(requests), 2 if cls is GeminiVertexProvider else 1)
                self.assertEqual(result.ok, cls is GeminiVertexProvider)
                if result.ok:
                    self.assertTrue(any(row["role"] == "system" for row in requests[0]["contents"]))
                    self.assertFalse(any(row["role"] == "system" for row in requests[1]["contents"]))
                    self.assertEqual(model_parts(requests[0]), model_parts(requests[1]))

    async def test_stream_and_nonstream_preserve_complete_signed_multimodal_parts(self):
        parts = [
            {"thought": True, "text": "summary", "thoughtSignature": "thought"},
            {"text": "answer", "thoughtSignature": "text"},
            {"functionCall": {"name": "lookup", "args": {"x": 1}}, "thoughtSignature": "call"},
            {"functionCall": {"name": "lookup", "args": {"x": 2}}},
            {"inlineData": {"mimeType": "image/png", "data": "fixture"}, "thoughtSignature": "image"},
        ]
        for cls in CHANNELS:
            for stream in (False, True):
                def handler(request):
                    return httpx.Response(200, text=sse([body(parts)])) if stream else httpx.Response(200, json=body(parts))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    p = provider(cls, http)
                    result = await (p.chat_stream if stream else p.chat)(messages=[])
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.reasoning, "summary")
                self.assertEqual(result.text, "answer")
                self.assertEqual(result.provider_meta["raw_parts"], parts)
                messages = stored_reply(p, result)
                messages[0]["_meta"]["reasoning"] = "EDITED_DISPLAY"
                payload = p.build_payload(messages)
                self.assertEqual(model_parts(payload), parts)
                self.assertNotIn("EDITED_DISPLAY", json.dumps(payload))

    async def test_sse_and_json_array_capture_usage_only_frames(self):
        parts = [{"text": "A"}, {"text": "B", "thoughtSignature": "signature"}]
        chunks = [body([part]) for part in parts] + [{"usageMetadata": {"promptTokenCount": 10, "thoughtsTokenCount": 3, "candidatesTokenCount": 2}}]
        for mode in ("sse", "array"):
            text = sse(chunks).rstrip() if mode == "sse" else json.dumps(chunks)
            transport = BytesStream([text.encode()[:15], text.encode()[15:]])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=transport))) as http:
                p = provider(GeminiAIStudioProvider, http)
                callback = AsyncMock()
                result = await p.chat_stream(messages=[], on_text=callback)
            self.assertTrue(result.ok, result.error)
            self.assertTrue(transport.closed)
            self.assertEqual(result.text, "AB")
            self.assertEqual(result.usage["completion_tokens"], 5)
            self.assertEqual(result.provider_meta["raw_parts"], parts)
            self.assertEqual(model_parts(p.build_payload(stored_reply(p, result))), parts)
            self.assertEqual([call.args[0] for call in callback.await_args_list], ["A", "B"])

    async def test_stream_exception_or_error_event_returns_failure_and_closes(self):
        for fail, suffix in ((True, ""), (False, 'data: {"error":{"code":400,"message":"broken"}}\n\n'), (False, 'data: invalid-json\n\n')):
            transport = BytesStream([(sse([body()]) + suffix).encode()], fail=fail)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=transport))) as http:
                result = await provider(GeminiAIStudioProvider, http).chat_stream(messages=[])
            self.assertFalse(result.ok)
            self.assertTrue(transport.closed)
            self.assertEqual(result.tool_calls, [])

    async def test_concurrent_signature_retry_does_not_modify_another_request(self):
        requests = {"retry": [], "normal": []}

        async def handler(request):
            payload = json.loads(request.content)
            tag = payload["contents"][0]["parts"][0]["text"]
            requests[tag].append(payload)
            await asyncio.sleep(0)
            if tag == "retry" and len(requests[tag]) == 1:
                return httpx.Response(400, json={"error": {"message": "Invalid thought signature"}})
            return httpx.Response(200, json=body())

        first = signed_history(GeminiAIStudioProvider)
        second = copy.deepcopy(first)
        first[0]["content"], second[0]["content"] = "retry", "normal"
        before = copy.deepcopy((first, second))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            p = provider(GeminiAIStudioProvider, http)
            results = await asyncio.gather(p.chat(messages=first), p.chat(messages=second))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual([len(requests[key]) for key in ("retry", "normal")], [2, 1])
        self.assertEqual(model_parts(requests["normal"][0]), second[1]["_meta"]["metadata"][p.name]["raw_parts"])
        self.assertEqual((first, second), before)


if __name__ == "__main__":
    unittest.main()
