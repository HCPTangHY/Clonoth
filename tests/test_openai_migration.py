"""Offline checks for OpenAI/DeepSeek production feature migration."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.openai import OpenAIProvider
from providers.deepseek import DeepSeekProvider
from test_provider_history_contract import history
from test_reasoning_contract import stored_reply


def provider(cls=OpenAIProvider, **options):
    return cls(http=options.pop("http", object()), api_key="test-key", base_url="https://example.invalid/v1", model="test-model", **options)


class OpenAIConfigurationTests(unittest.TestCase):
    def test_default_openai_is_standard_despite_saved_or_legacy_reasoning(self):
        messages = history()[:-1]
        messages[1]["_meta"].update(provider="openai", reasoning="DISPLAY")
        messages[1]["_meta"]["metadata"]["openai"] = {"reasoning_content": "RAW"}
        messages[1].update(reasoning_content="OLD", reasoning="ALIAS", thinking="ALIAS")
        before = copy.deepcopy(messages)
        payload = provider().build_payload(messages)
        self.assertTrue(all(key not in payload["messages"][1] for key in ("reasoning_content", "reasoning", "thinking")))
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(messages, before)

    def test_explicit_parameters_override_provider_options(self):
        options = {"reasoning_effort": "low", "max_tokens": 100, "max_completion_tokens": 200, "reasoning_field": "thinking"}
        before = copy.deepcopy(options)
        p = provider(provider_options=options, reasoning_effort="high", max_tokens=300, max_completion_tokens=400, reasoning_field="reasoning_content")
        payload = p.build_payload(history()[:-1], stream=True)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["max_tokens"], 300)
        self.assertEqual(payload["max_completion_tokens"], 400)
        self.assertEqual(payload["messages"][1]["reasoning_content"], "")
        self.assertNotIn("thinking", payload["messages"][1])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(options, before)

    def test_options_and_null_defaults(self):
        payload = provider(provider_options={"reasoning_effort": "medium", "max_tokens": "128", "max_completion_tokens": "256"}).build_payload([])
        self.assertEqual((payload["reasoning_effort"], payload["max_tokens"], payload["max_completion_tokens"]), ("medium", 128, 256))
        p = provider(DeepSeekProvider, provider_options={"reasoning_effort": None, "reasoning_field": None})
        self.assertEqual(p._reasoning_field, "reasoning_content")
        self.assertEqual(p.build_payload([])["reasoning_effort"], "high")

    def test_invalid_token_limits_do_not_produce_invalid_request_values(self):
        for value in (0, -1, "bad", True, False, [], {}, float("inf"), float("nan")):
            with self.subTest(value=value):
                payload = provider(provider_options={"max_tokens": value, "max_completion_tokens": value}).build_payload([])
                self.assertNotIn("max_tokens", payload)
                self.assertNotIn("max_completion_tokens", payload)

    def test_reasoning_field_cannot_overwrite_protocol_or_internal_fields(self):
        for name in ("role", "content", "tool_calls", "tool_call_id", "_meta", "meta", "metadata", "provider", "provider_meta", "thinking_enabled", "not a field"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                provider(reasoning_field=name)

    def test_private_configured_value_is_not_replaced_by_aliases(self):
        messages = history()[:-1]
        messages[1]["_meta"].update(provider="openai", reasoning="DISPLAY")
        messages[1]["_meta"]["metadata"]["openai"] = {"custom_reasoning": "RAW", "reasoning_content": "OTHER"}
        messages[1].update(custom_reasoning="STALE", reasoning_content="OLD")
        before = copy.deepcopy(messages)
        payload = provider(reasoning_field="custom_reasoning").build_payload(messages)
        self.assertEqual(payload["messages"][1]["custom_reasoning"], "RAW")
        self.assertNotIn("reasoning_content", payload["messages"][1])
        for marker in ("DISPLAY", "OTHER", "STALE", "OLD"):
            self.assertNotIn(marker, json.dumps(payload))
        self.assertEqual(messages, before)

    def test_foreign_metadata_and_display_are_not_sources_for_custom_field(self):
        messages = history()[:-1]
        messages[1]["_meta"].update(provider="deepseek", reasoning="FOREIGN")
        messages[1]["_meta"]["metadata"]["deepseek"] = {"custom_reasoning": "FOREIGN_RAW"}
        messages[1]["custom_reasoning"] = "FOREIGN_LEGACY"
        payload = provider(reasoning_field="custom_reasoning").build_payload(messages)
        self.assertEqual(payload["messages"][1]["custom_reasoning"], "")
        self.assertNotIn("FOREIGN", json.dumps(payload))

    def test_custom_field_and_padding_are_disabled_for_text_tool_modes(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            p = provider(cls, reasoning_field="custom_reasoning")
            messages = history()[:-1]
            messages[1]["_meta"]["metadata"][p.name] = {"custom_reasoning": "RAW"}
            for mode in ("fake-native", "fake_native", "json"):
                self.assertNotIn("custom_reasoning", json.dumps(p.build_payload(messages, tool_mode=mode)))

    def test_deepseek_official_defaults_and_shared_transport(self):
        p = DeepSeekProvider(http=object(), api_key="test-key")
        self.assertEqual(p._base_url, "https://api.deepseek.com")
        for blank in (None, "", " ", "\t\n"):
            self.assertEqual(DeepSeekProvider(http=object(), api_key="test-key", base_url=blank)._base_url, "https://api.deepseek.com")
        self.assertEqual(p.model, "deepseek-v4-pro")
        self.assertIs(DeepSeekProvider.chat, OpenAIProvider.chat)
        self.assertIs(DeepSeekProvider.chat_stream, OpenAIProvider.chat_stream)
        payload = p.build_payload([{"role": "assistant", "content": "prior"}, {"role": "user", "content": "next"}])
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["messages"][0]["reasoning_content"], "")

    def test_deepseek_none_disables_replay_even_with_explicit_field(self):
        for options in (
            {"reasoning_effort": " NoNe ", "reasoning_field": "custom_reasoning"},
            {"provider_options": {"reasoning_effort": "none", "reasoning_field": "reasoning_content"}},
            {"thinking": False},
        ):
            p = provider(DeepSeekProvider, **options)
            messages = history()[:-1]
            messages[1]["_meta"].update(provider="deepseek", reasoning="DISPLAY")
            messages[1]["_meta"]["metadata"]["deepseek"] = {"reasoning_content": "RAW", "custom_reasoning": "RAW"}
            payload = p.build_payload(messages)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", payload)
            self.assertNotIn("RAW", json.dumps(payload))
            self.assertNotIn("reasoning_content", payload["messages"][1])

    def test_engine_and_fallback_factories_forward_new_options(self):
        from engine.runner import _create_provider_from_registry
        from engine.builtin.fallback_provider import _create_fallback_provider

        options = {"reasoning_effort": "high", "reasoning_field": "reasoning_content", "max_tokens": 256}
        before = copy.deepcopy(options)
        http = object()
        for name in ("openai", "deepseek"):
            resolved = SimpleNamespace(provider_type=name, api_key="test-key", base_url="https://example.invalid/v1", model="test-model")
            main = _create_provider_from_registry(resolved, llm_http=http, api_key="test-key", base_url="", provider_options=options)
            fallback = _create_fallback_provider(provider_type=name, base_url=resolved.base_url, api_key="test-key", model=resolved.model, http=http, provider_options=options)
            for p in (main, fallback):
                self.assertIs(p._http, http)
                payload = p.build_payload(history()[:-1])
                self.assertEqual(payload["max_tokens"], 256)
                self.assertEqual(payload["reasoning_effort"], "high")
                self.assertEqual(payload["messages"][1]["reasoning_content"], "")
        self.assertEqual(options, before)

    def test_deepseek_token_limit_mapping(self):
        for options, expected in (({"max_completion_tokens": 400}, 400), ({"max_tokens": 300, "max_completion_tokens": 400}, 300)):
            payload = provider(DeepSeekProvider, provider_options=options).build_payload([])
            self.assertEqual(payload["max_tokens"], expected)
            self.assertNotIn("max_completion_tokens", payload)


class OpenAITransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_field_round_trips_without_using_display(self):
        for cls in (OpenAIProvider, DeepSeekProvider):
            for stream in (False, True):
                for field in ("reasoning_content", "custom_reasoning", "thinking"):
                    with self.subTest(provider=cls.provider_name, stream=stream, field=field):
                        requests = []

                        def handle(request):
                            requests.append(json.loads(request.content))
                            reply = {"content": "answer", field: "RAW\n", "reasoning": "OTHER_DISPLAY"}
                            if stream:
                                events = [
                                    {"choices": [{"delta": reply}]},
                                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "prompt_cache_hit_tokens": 4}},
                                ]
                                return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")
                            return httpx.Response(200, json={"choices": [{"message": reply}]})

                        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                            p = provider(cls, http=http, reasoning_field=field, max_tokens=256)
                            callback = AsyncMock()
                            if stream:
                                response = await p.chat_stream(messages=[{"role": "user", "content": "question"}], on_thinking=callback)
                                callback.assert_awaited_once_with("RAW\n")
                                self.assertEqual(response.usage["cached_prompt_tokens"], 4)
                            else:
                                response = await p.chat(messages=[{"role": "user", "content": "question"}])
                        self.assertTrue(response.ok, response.error)
                        self.assertEqual(response.reasoning, "RAW\n")
                        if not stream:
                            self.assertEqual(response.raw["choices"][0]["message"][field], "RAW\n")
                        self.assertEqual(response.provider_meta[field], "RAW\n")
                        if cls is DeepSeekProvider:
                            self.assertEqual(response.provider_meta["provider"], "deepseek")
                            self.assertIs(response.provider_meta["thinking_enabled"], True)
                        messages = stored_reply(p, response)
                        messages[0]["_meta"]["reasoning"] = "EDITED_DISPLAY"
                        payload = p.build_payload(messages)
                        self.assertEqual(payload["messages"][0][field], "RAW\n")
                        self.assertNotIn("EDITED_DISPLAY", json.dumps(payload))
                        self.assertEqual(requests[0]["max_tokens"], 256)

    async def test_passive_fields_remain_visible_with_standard_openai_requests(self):
        for stream in (False, True):
            for field in ("reasoning_content", "reasoning", "thinking"):
                def handle(request):
                    reply = {"content": "answer", field: "DISPLAY"}
                    if stream:
                        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": reply}]}) + "\n\ndata: [DONE]\n\n")
                    return httpx.Response(200, json={"choices": [{"message": reply}]})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    p = provider(http=http)
                    response = await (p.chat_stream if stream else p.chat)(messages=[{"role": "user", "content": "question"}])
                    self.assertTrue(response.ok, response.error)
                    self.assertEqual(response.reasoning, "DISPLAY")
                    payload = p.build_payload(stored_reply(p, response))
                    self.assertNotIn(field, payload["messages"][0])

    async def test_deepseek_inherits_stream_error_and_finish_reason_detection(self):
        for event in (
            {"error": {"message": "upstream failed"}},
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
        ):
            def handle(request):
                return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                response = await provider(DeepSeekProvider, http=http).chat_stream(messages=[])
            self.assertFalse(response.ok)
            self.assertTrue(response.error)

    async def test_deepseek_closes_only_owned_clients(self):
        shared = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        p = provider(DeepSeekProvider, http=shared)
        await p.aclose()
        self.assertFalse(shared.is_closed)
        await shared.aclose()
        owned = DeepSeekProvider(api_key="test-key")
        await owned.aclose()
        self.assertTrue(owned._http.is_closed)


if __name__ == "__main__":
    unittest.main()
