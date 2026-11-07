"""Forward provider migration checks; no live network or filesystem fixtures.

Run: python3.11 -B tests/test_provider_forward_migration.py
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.anthropic import AnthropicProvider
from providers.gemini_aistudio import GeminiAIStudioProvider
from providers.gemini_vertex import GeminiVertexProvider


def provider(cls=AnthropicProvider, options=None):
    return cls(http=None, api_key="test", base_url="https://example.invalid", model="test-model", provider_options=options)


HISTORY = [
    {"role": "system", "content": "static"},
    {"role": "developer", "content": "fixed"},
    {"role": "user", "content": "question"},
    {"role": "assistant", "content": "answer"},
    {"role": "system", "content": "dynamic"},
    {"role": "developer", "content": [{"type": "text", "text": "latest"}]},
]


class ProviderForwardMigrationTests(unittest.TestCase):
    def test_anthropic_default_cache_has_equal_independent_breakpoints(self):
        for stream in (False, True):
            payload = provider().build_payload(HISTORY, stream=stream)
            expected = {"type": "ephemeral", "ttl": "1h"}
            self.assertEqual(payload["cache_control"], expected)
            self.assertEqual(payload["system"], [{"type": "text", "text": "static\n\nfixed", "cache_control": expected}])
            payload["cache_control"]["ttl"] = "5m"
            self.assertEqual(payload["system"][0]["cache_control"]["ttl"], "1h")

    def test_anthropic_explicit_cache_options_are_not_overridden(self):
        for option, expected in (
            ("5m", {"type": "ephemeral", "ttl": "5m"}),
            (" 1h ", {"type": "ephemeral", "ttl": "1h"}),
            ("ephemeral", {"type": "ephemeral"}),
            ({"type": "ephemeral", "ttl": "5m"}, {"type": "ephemeral", "ttl": "5m"}),
        ):
            with self.subTest(option=option):
                options = {"cache_control": deepcopy(option)}
                before = deepcopy(options)
                payload = provider(options=options).build_payload(HISTORY)
                self.assertEqual(payload["cache_control"], expected)
                self.assertEqual(payload["system"][0]["cache_control"], expected)
                payload["system"][0]["cache_control"]["ttl"] = "changed"
                self.assertEqual(options, before)

    def test_anthropic_disabled_or_empty_cache_keeps_plain_system(self):
        for option in (False, None, {}, "", "  "):
            payload = provider(options={"cache_control": option}).build_payload(HISTORY)
            self.assertNotIn("cache_control", payload)
            self.assertEqual(payload["system"], "static\n\nfixed")

    def test_anthropic_without_system_does_not_invent_system_breakpoint(self):
        payload = provider().build_payload([{"role": "user", "content": "hello"}])
        self.assertIn("cache_control", payload)
        self.assertNotIn("system", payload)

    def test_anthropic_mid_history_instructions_keep_order_without_mutation(self):
        messages = deepcopy(HISTORY)
        payload = provider().build_payload(messages)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant", "user"])
        self.assertEqual(payload["messages"][-1]["content"], [
            {"type": "text", "text": "dynamic"}, {"type": "text", "text": "latest"},
        ])
        payload["messages"][-1]["content"][0]["text"] = "changed"
        self.assertEqual(messages, HISTORY)

    def test_gemini_backends_only_hoist_leading_instructions(self):
        for cls in (GeminiAIStudioProvider, GeminiVertexProvider):
            with self.subTest(backend=cls.provider_name):
                messages = deepcopy(HISTORY)
                payload = provider(cls).build_payload(messages)
                self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "static\n\nfixed"}])
                self.assertEqual(payload["contents"][2:4], [
                    {"role": "user", "parts": [{"text": "dynamic"}]},
                    {"role": "user", "parts": [{"text": "latest"}]},
                ])
                payload["contents"][2]["parts"][0]["text"] = "changed"
                self.assertEqual(messages, HISTORY)

    def test_mid_history_instruction_changes_do_not_change_system_prefix(self):
        changed = deepcopy(HISTORY)
        changed[4]["content"] = "new state"
        for cls, field in ((AnthropicProvider, "system"), (GeminiAIStudioProvider, "systemInstruction"), (GeminiVertexProvider, "systemInstruction")):
            with self.subTest(backend=cls.provider_name):
                instance = provider(cls)
                self.assertEqual(instance.build_payload(HISTORY)[field], instance.build_payload(changed)[field])


if __name__ == "__main__":
    unittest.main()
