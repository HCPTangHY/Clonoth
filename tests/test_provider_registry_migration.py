"""Provider registration and keyed-disposer migration checks.

Run: python3.11 -B tests/test_provider_registry_migration.py
All registries created by these tests are local; no API calls are made.
"""
from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.context import Contributions
from engine.faces.prompt_sections import PromptSectionRegistry
from engine.hooks.registry import HookRegistry
from engine.registry_core import DisposalLedger
import providers
from providers import BaseProvider, ProviderRegistry, ProviderResponse
from providers.deepseek import DeepSeekProvider
from providers.gemini_aistudio import GeminiAIStudioProvider
from providers.gemini_vertex import GeminiVertexProvider


class FirstProvider(BaseProvider):
    provider_name = "first-test-provider"

    async def chat(self, *, messages, tools=None, tool_mode="native"):
        return ProviderResponse(ok=True, text="test")

    async def chat_stream(
        self, *, messages, tools=None, tool_mode="native",
        on_text=None, on_thinking=None, on_tool_delta=None,
    ):
        return ProviderResponse(ok=True, text="test")


class SecondProvider(FirstProvider):
    provider_name = "second-test-provider"


class DisposalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = DisposalLedger()
        self.calls = []

    def callback(self, name):
        return lambda: self.calls.append(name)

    def test_unkeyed_record_and_add_remain_compatible(self):
        repeated = self.callback("repeat")
        self.ledger.record(self.callback("not-collected"))
        with self.ledger.collecting(" plugin "):
            self.ledger.record(repeated)
            self.ledger.record(repeated)
        self.ledger.add("plugin", self.callback("external"))
        self.assertEqual(self.ledger.owned(" plugin "), 3)
        self.assertEqual(self.ledger.counts(), {"plugin": 3})
        self.assertEqual(self.ledger.unload(" plugin "), {
            "plugin": "plugin", "disposed": 3, "errors": [],
        })
        self.assertEqual(self.calls, ["external", "repeat", "repeat"])
        self.assertEqual(self.ledger.counts(), {})
        self.assertEqual(self.ledger.unload("plugin")["disposed"], 0)

    def test_same_owner_key_replacement_moves_to_latest_unload_position(self):
        with self.ledger.collecting("plugin"):
            self.ledger.record(self.callback("old"), key="provider")
            self.ledger.record(self.callback("unkeyed"))
            self.ledger.record(self.callback("other"), key="other")
            self.ledger.record(self.callback("replacement"), key="provider")
        self.assertEqual(self.calls, [], "Replacement must not execute the previous disposer")
        self.assertEqual(self.ledger.counts(), {"plugin": 3})
        self.ledger.unload("plugin")
        self.assertEqual(self.calls, ["replacement", "other", "unkeyed"])

    def test_keyed_add_transfers_ownership_without_disposing(self):
        self.ledger.add("old", self.callback("old"), key=("surface", "entry"))
        self.ledger.add("new", self.callback("new"), key=("surface", "entry"))
        self.assertEqual(self.ledger.counts(), {"new": 1})
        self.assertEqual(self.ledger.unload("old")["disposed"], 0)
        self.assertEqual(self.calls, [])
        self.ledger.unload("new")
        self.assertEqual(self.calls, ["new"])

    def test_explicit_disposal_then_forget_does_not_run_again(self):
        dispose = self.callback("manual")
        self.ledger.add("first", dispose)
        self.ledger.add("second", dispose, key="resource")
        self.ledger.add("second", self.callback("remaining"))
        dispose()
        self.ledger.forget(dispose)
        self.ledger.forget(dispose)
        self.assertEqual(self.ledger.counts(), {"second": 1})
        self.assertEqual(self.ledger.owned("first"), 0)
        self.ledger.unload("first")
        self.ledger.unload("second")
        self.assertEqual(self.calls, ["manual", "remaining"])

    def test_forget_matches_identity_not_equality(self):
        class EqualCallbacks:
            def __init__(self, callback):
                self.callback = callback

            def __eq__(self, other):
                return isinstance(other, EqualCallbacks)

            def __call__(self):
                self.callback()

        first = EqualCallbacks(self.callback("first"))
        second = EqualCallbacks(self.callback("second"))
        self.ledger.add("plugin", first)
        self.ledger.add("plugin", second)
        self.ledger.forget(first)
        self.assertEqual(self.ledger.owned("plugin"), 1)
        self.ledger.unload("plugin")
        self.assertEqual(self.calls, ["second"])

    def test_counts_are_detached_metadata(self):
        self.ledger.add("plugin", self.callback("resource"))
        counts = self.ledger.counts()
        self.assertEqual(counts, {"plugin": 1})
        counts["plugin"] = 999
        counts["injected"] = 1
        self.assertEqual(self.ledger.counts(), {"plugin": 1})

    def test_failure_does_not_interrupt_reverse_order_unload(self):
        def failing():
            self.calls.append("failed")
            raise RuntimeError("test disposal failure")

        with self.ledger.collecting("plugin"):
            self.ledger.record(self.callback("first"))
            self.ledger.record(failing, key="bad")
            self.ledger.record(self.callback("last"))
        with self.assertLogs("engine.registry_core", level="WARNING"):
            result = self.ledger.unload("plugin")
        self.assertEqual(self.calls, ["last", "failed", "first"])
        self.assertEqual(result, {
            "plugin": "plugin", "disposed": 3, "errors": ["test disposal failure"],
        })
        self.assertEqual(self.ledger.counts(), {})

    def test_nested_collectors_restore_owner_after_exception(self):
        self.assertIsNone(self.ledger.current_owner())
        with self.ledger.collecting("outer"):
            self.assertEqual(self.ledger.current_owner(), "outer")
            with self.assertRaisesRegex(ValueError, "failed load"):
                with self.ledger.collecting("inner"):
                    self.assertEqual(self.ledger.current_owner(), "inner")
                    self.ledger.record(self.callback("inner"))
                    raise ValueError("failed load")
            self.assertEqual(self.ledger.current_owner(), "outer")
            with self.ledger.collecting(""):
                self.ledger.record(self.callback("outer"))
            self.assertEqual(self.ledger.current_owner(), "outer")
        self.assertIsNone(self.ledger.current_owner())
        self.assertEqual(self.ledger.counts(), {"inner": 1, "outer": 1})

    def test_existing_unkeyed_registry_callers_share_ledger(self):
        hooks = HookRegistry()
        contributions = Contributions()
        contributions.set_ledger(hooks._ledger)
        sections = PromptSectionRegistry()
        registry = ProviderRegistry()
        registry.set_disposal_ledger(hooks._ledger)
        with hooks.collecting("plugin"):
            contributions.mount("sections", sections)
            sections.register_section("test-section", lambda ctx: "test")
            registry.register("test-provider", FirstProvider)
        hooks.add_plugin_disposer("plugin", self.callback("external"))
        self.assertEqual(hooks._ledger.counts(), {"plugin": 4})
        self.assertIs(contributions.get("sections"), sections)
        result = hooks.unload_plugin("plugin")
        self.assertEqual(result["disposed"], 4)
        self.assertEqual(result["errors"], [])
        self.assertEqual(sections.list_sections(), [])
        self.assertIsNone(contributions.get("sections"))
        self.assertIsNone(registry.get("test-provider"))
        self.assertEqual(self.calls, ["external"])


class ProviderRegistryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = ProviderRegistry()
        self.ledger = DisposalLedger()
        self.registry.set_disposal_ledger(self.ledger)

    def test_same_owner_registration_replaces_one_key(self):
        with self.ledger.collecting("plugin"):
            self.registry.register(" Test ", FirstProvider)
            self.registry.register("test", SecondProvider)
            self.registry.register("TEST", SecondProvider)
        self.assertEqual(self.registry.list(), ["test"])
        self.assertIs(self.registry.get(" test "), SecondProvider)
        self.assertEqual(self.ledger.counts(), {"plugin": 1})
        self.assertEqual(self.ledger.unload("plugin")["disposed"], 1)
        self.assertIsNone(self.registry.get("test"))

    def test_cross_owner_replacement_preserves_new_provider_on_old_unload(self):
        # Reusing the same class is intentional: a class identity check alone
        # cannot prevent the old owner's disposer from removing this entry.
        for replacement in (FirstProvider, SecondProvider):
            with self.subTest(replacement=replacement.__name__):
                with self.ledger.collecting("old"):
                    self.registry.register("shared", FirstProvider)
                with self.ledger.collecting("new"):
                    self.registry.register("shared", replacement)
                self.assertEqual(self.ledger.counts(), {"new": 1})
                self.assertEqual(self.ledger.unload("old")["disposed"], 0)
                self.assertIs(self.registry.get("shared"), replacement)
                self.assertEqual(self.ledger.unload("new")["disposed"], 1)
                self.assertIsNone(self.registry.get("shared"))

    def test_core_replacement_clears_old_plugin_ownership(self):
        with self.ledger.collecting("plugin"):
            self.registry.register("shared", FirstProvider)
        self.registry.register("shared", FirstProvider)
        self.assertEqual(self.ledger.counts(), {})
        self.assertEqual(self.ledger.unload("plugin")["disposed"], 0)
        self.assertIs(self.registry.get("shared"), FirstProvider)

    def test_registry_instances_do_not_share_disposal_keys(self):
        other = ProviderRegistry()
        other.set_disposal_ledger(self.ledger)
        with self.ledger.collecting("plugin"):
            self.registry.register("shared", FirstProvider)
            other.register("shared", SecondProvider)
        self.assertEqual(self.ledger.counts(), {"plugin": 2})
        self.ledger.unload("plugin")
        self.assertIsNone(self.registry.get("shared"))
        self.assertIsNone(other.get("shared"))

    def test_explicit_provider_dispose_and_forget_remove_registration_once(self):
        with self.ledger.collecting("plugin"):
            dispose = self.registry.register("test", FirstProvider)
        dispose()
        self.ledger.forget(dispose)
        self.assertIsNone(self.registry.get("test"))
        self.assertEqual(self.ledger.counts(), {})
        self.assertEqual(self.ledger.unload("plugin")["disposed"], 0)

    def test_manual_disposer_cannot_remove_different_replacement_class(self):
        dispose = self.registry.register("test", FirstProvider)
        self.registry.register("test", SecondProvider)
        dispose()
        self.assertIs(self.registry.get("test"), SecondProvider)

    def test_stale_manual_disposer_cannot_remove_same_class_registration(self):
        with self.ledger.collecting("old"):
            old_dispose = self.registry.register("test", FirstProvider)
        with self.ledger.collecting("new"):
            new_dispose = self.registry.register("test", FirstProvider)
        old_dispose()
        self.assertIs(self.registry.get("test"), FirstProvider)
        self.assertEqual(self.ledger.counts(), {"new": 1})
        new_dispose()
        self.ledger.forget(new_dispose)
        self.assertIsNone(self.registry.get("test"))
        self.assertEqual(self.ledger.counts(), {})
        self.assertEqual(self.registry._registration_tokens, {})

    def test_discovery_skips_abstract_imported_and_unnamed_classes(self):
        module = ModuleType("providers.registry_test_fixture")
        abstract = type("AbstractProvider", (BaseProvider,), {
            "__module__": module.__name__, "provider_name": "abstract-test",
        })
        concrete = type("ConcreteProvider", (FirstProvider,), {
            "__module__": module.__name__, "provider_name": "concrete-test",
        })
        unnamed = type("SharedProvider", (FirstProvider,), {
            "__module__": module.__name__, "provider_name": "",
        })
        module.AbstractProvider = abstract
        module.ConcreteProvider = concrete
        module.SharedProvider = unnamed
        module.ImportedProvider = FirstProvider
        module.BaseProvider = BaseProvider
        global_registry = providers.registry
        original_entries = {name: global_registry.get(name) for name in global_registry.list()}
        entries = [
            SimpleNamespace(ispkg=False, name="base"),
            SimpleNamespace(ispkg=True, name="not_a_module"),
            SimpleNamespace(ispkg=False, name="registry_test_fixture"),
        ]
        with patch.object(providers, "registry", self.registry), \
             patch.object(providers.pkgutil, "iter_modules", return_value=entries), \
             patch.object(providers.importlib, "import_module", return_value=module) as import_module:
            result = providers.auto_discover()
        self.assertIs(result, self.registry)
        self.assertEqual(self.registry.list(), ["concrete-test"])
        self.assertIs(self.registry.get("concrete-test"), concrete)
        import_module.assert_called_once_with(module.__name__)
        self.assertIs(providers.registry, global_registry)
        self.assertEqual({name: global_registry.get(name) for name in global_registry.list()}, original_entries)

    def test_real_discovery_keeps_two_gemini_channels_and_exports_deepseek(self):
        global_registry = providers.registry
        original_entries = {name: global_registry.get(name) for name in global_registry.list()}
        with patch.object(providers, "registry", self.registry):
            providers.auto_discover()
            providers.auto_discover()
        self.assertEqual(
            {name for name in self.registry.list() if name.startswith("gemini")},
            {"gemini-aistudio", "gemini-vertex"},
        )
        self.assertIsNone(self.registry.get("gemini"))
        self.assertIs(self.registry.get("gemini-aistudio"), GeminiAIStudioProvider)
        self.assertIs(self.registry.get("gemini-vertex"), GeminiVertexProvider)
        self.assertIs(self.registry.get("deepseek"), DeepSeekProvider)
        self.assertIs(providers.DeepSeekProvider, DeepSeekProvider)
        self.assertIn("DeepSeekProvider", providers.__all__)
        self.assertNotIn("GeminiProvider", providers.__all__)
        self.assertIs(providers.registry, global_registry)
        self.assertEqual({name: global_registry.get(name) for name in global_registry.list()}, original_entries)


if __name__ == "__main__":
    unittest.main()
