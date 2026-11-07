"""Gemini channel split checks; no external API calls.

Run: python3.11 -B tests/test_gemini_provider_split.py
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.model import ResolvedProvider, resolve_provider
from engine.node import Node, _node_from_dict
from engine.runner import _create_provider_from_registry
from providers import GeminiAIStudioProvider, GeminiVertexProvider, auto_discover, registry
from providers.gemini import GeminiProvider

CHANNELS = (GeminiAIStudioProvider, GeminiVertexProvider)


def make_provider(cls, http=None, **kwargs):
    return cls(http=http, api_key="test-key", base_url="https://gateway.example", model="test-model", **kwargs)


class GeminiChannelTests(unittest.TestCase):
    def test_registry_has_only_two_gemini_channels(self):
        for _ in range(2):
            auto_discover()
            self.assertEqual(
                {name for name in registry.list() if name.startswith("gemini")},
                {"gemini-aistudio", "gemini-vertex"},
            )
        self.assertIsNone(registry.get("gemini"))
        for cls in CHANNELS:
            self.assertIs(registry.get(cls.provider_name), cls)
            self.assertTrue(issubclass(cls, GeminiProvider))

    def test_shared_implementation_cannot_be_instantiated(self):
        self.assertEqual(GeminiProvider.provider_name, "")
        with self.assertRaisesRegex(TypeError, "gemini-vertex or gemini-aistudio"):
            make_provider(GeminiProvider)

    def test_explicit_endpoint_defaults(self):
        ais = GeminiAIStudioProvider(http=None, api_key="test-key", base_url=None, model="test-model")
        self.assertEqual(ais.name, "gemini-aistudio")
        self.assertEqual(ais._endpoint(), "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent")
        self.assertEqual(ais._headers()["x-goog-api-key"], "test-key")
        with self.assertRaisesRegex(ValueError, "explicit base_url"):
            GeminiVertexProvider(http=None, api_key="test-key", base_url=None, model="test-model")
        vertex = make_provider(GeminiVertexProvider)
        self.assertEqual(vertex.name, "gemini-vertex")
        self.assertEqual(vertex._headers()["Authorization"], "Bearer test-key")
        self.assertEqual(vertex._endpoint(), "https://gateway.example/v1beta/models/test-model:generateContent")

    def test_node_config_rejects_retired_and_unknown_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("gemini", "misspelled-provider"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unknown provider"):
                    _node_from_dict(root, "audit", {"provider": name})
            for name in ("", "gemini-aistudio", "gemini-vertex"):
                node = _node_from_dict(root, "audit", {"provider": name})
                self.assertEqual(node.provider, name)

    def test_global_and_session_config_cannot_silently_fall_back(self):
        for override in (None, {"provider": "gemini"}):
            rp = resolve_provider(
                Path("."), Node(id="audit", type="ai"), "test-model",
                default_provider_type="gemini" if override is None else "openai",
                session_override=override,
            )
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "Unknown provider 'gemini'"):
                _create_provider_from_registry(
                    rp, llm_http=None, api_key="test-key", base_url="https://gateway.example", provider_options={},
                )
        # Unspecified provider still uses the documented default.
        provider = _create_provider_from_registry(
            ResolvedProvider(model="test-model"), llm_http=None, api_key="test-key",
            base_url="https://gateway.example/v1", provider_options={},
        )
        self.assertEqual(provider.name, "openai")

    def test_environment_variables_are_backend_specific(self):
        values = {
            "GEMINI_AISTUDIO_API_KEY": "ais-key", "GEMINI_AISTUDIO_BASE_URL": "https://ais.example",
            "GEMINI_VERTEX_API_KEY": "vertex-key", "GEMINI_VERTEX_BASE_URL": "https://vertex.example",
            "GEMINI_API_KEY": "legacy-key", "GEMINI_BASE_URL": "https://legacy.example",
        }
        with patch.dict(os.environ, values, clear=True):
            for cls, prefix in ((GeminiAIStudioProvider, "ais"), (GeminiVertexProvider, "vertex")):
                provider = _create_provider_from_registry(
                    ResolvedProvider(model="test-model", provider_type=cls.provider_name),
                    llm_http=None, api_key="global-key", base_url="", provider_options={},
                )
                self.assertEqual(provider._api_key, prefix + "-key")
                self.assertEqual(provider._base_url, "https://" + prefix + ".example")

    def test_native_history_preserves_matching_metadata(self):
        for cls in CHANNELS:
            parts = [{"text": "signed answer", "thoughtSignature": "test-signature"}]
            messages = [{
                "role": "assistant", "content": "plain answer",
                "_meta": {"tool_mode": "native", "metadata": {cls.provider_name: {"raw_parts": parts}}},
            }]
            original = copy.deepcopy(messages)
            provider = make_provider(cls)
            view = provider.prepare_history(messages, tool_mode="native")
            self.assertIn("_meta", view[0])
            body = provider.build_payload(messages, tool_mode="native")
            self.assertEqual(body["contents"][0]["parts"], parts)
            self.assertEqual(messages, original)

    def test_other_backend_and_retired_metadata_are_not_replayed(self):
        for cls in CHANNELS:
            other = "gemini-vertex" if cls is GeminiAIStudioProvider else "gemini-aistudio"
            for source in (other, "gemini"):
                messages = [{
                    "role": "assistant", "content": "plain answer",
                    "_meta": {"metadata": {source: {"raw_parts": [
                        {"text": "foreign answer", "thoughtSignature": "foreign-signature"},
                    ]}}},
                }]
                body = make_provider(cls)._build_body(messages, None)
                self.assertEqual(body["contents"][0]["parts"], [{"text": "plain answer"}])

    def test_thinking_defaults_and_approved_channel_tails_do_not_mutate_config(self):
        thinking = {"thinkingBudget": 8192, "includeThoughts": False}
        for cls in CHANNELS:
            options = {"thinking_config": dict(thinking)}
            provider = make_provider(cls, provider_options=options)
            body = provider._build_body([{"role": "user", "content": "hello"}], None)
            self.assertEqual(body["generationConfig"]["thinkingConfig"], {"includeThoughts": True})
            self.assertEqual(body["generationConfig"]["maxOutputTokens"], 65535)
            vertex = cls is GeminiVertexProvider
            self.assertEqual(len(body["safetySettings"]), 10 if vertex else 6)
            self.assertEqual(
                [message["role"] for message in body["contents"]],
                ["user", "system"] if vertex else ["user", "model", "user"],
            )
            self.assertEqual(options, {"thinking_config": thinking})


class GeminiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_channels_round_trip_nonstream_and_stream_parts(self):
        parts = [{
            "functionCall": {"name": "lookup", "args": {"query": "example"}},
            "thoughtSignature": "test-signature",
        }]
        response = {"candidates": [{"content": {"parts": parts}}]}
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            if "streamGenerateContent" in str(request.url):
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: " + json.dumps(response) + "\n\n")
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            for cls in CHANNELS:
                provider = make_provider(cls, http=http)
                for method in (provider.chat, provider.chat_stream):
                    reply = await method(messages=[{"role": "user", "content": "lookup"}])
                    self.assertTrue(reply.ok, reply.error)
                    self.assertEqual(reply.provider_meta["raw_parts"], parts)
                    messages = [
                        {
                            "role": "assistant", "content": "",
                            "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in reply.tool_calls],
                            "_meta": {"tool_mode": "native", "metadata": {provider.name: reply.provider_meta}},
                        },
                        {"role": "tool", "tool_call_id": reply.tool_calls[0].id, "content": "found"},
                    ]
                    body = provider.build_payload(messages, tool_mode="native")
                    self.assertEqual(body["contents"][0]["parts"], parts)
                    self.assertEqual(body["contents"][1]["parts"][0]["functionResponse"]["name"], "lookup")
        self.assertEqual(len(requests), 4)


if __name__ == "__main__":
    unittest.main()
