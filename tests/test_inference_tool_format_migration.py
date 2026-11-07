"""Formatter migration checks; all fixtures are local and use no network.

Run: python3.11 -B -m unittest discover -s tests -p 'test_inference_tool_format_migration.py' -v
"""
from __future__ import annotations

import ast
import copy
from importlib.util import resolve_name
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers.tool_format import (
    JsonToolFormatter,
    NativeToolFormatter,
    build_llm_messages,
    prepare_raw_history,
    repair_tool_result_pairing,
)


class FormatterLocationTests(unittest.TestCase):
    def test_provider_is_the_only_formatter_location(self):
        self.assertTrue((ROOT / "providers/tool_format.py").is_file())
        self.assertFalse((ROOT / "engine/inference/tool_format.py").exists())
        old_module = "engine.inference.tool_format"
        for folder in ("engine", "providers", "supervisor", "tests"):
            for path in (ROOT / folder).rglob("*.py"):
                package = ".".join(path.relative_to(ROOT).parts[:-1])
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        if node.level:
                            module = resolve_name("." * node.level + module, package)
                        imports = [module, *(module + "." + alias.name for alias in node.names)]
                    else:
                        continue
                    self.assertNotIn(old_module, imports, f"{path}:{node.lineno}")
                    if folder == "providers":
                        self.assertFalse(
                            any(name == "engine" or name.startswith("engine.") for name in imports),
                            f"{path}:{node.lineno}",
                        )


class JsonInstructionTests(unittest.TestCase):
    def test_finish_instruction_matches_visible_tools(self):
        formatter = JsonToolFormatter()
        lookup = {"name": "lookup", "input_schema": {"type": "object"}}
        prompt, tools = formatter.inject_tool_definitions([lookup], "system")
        self.assertIsNone(tools)
        self.assertIn("直接输出最终回复正文即可", prompt)
        self.assertNotIn("必须调用 finish", prompt)
        self.assertIn("使用 intermediate_reply 工具", prompt)
        self.assertNotIn("使用 reply 工具", prompt)
        for finish in (
            {"name": "finish", "input_schema": {"type": "object"}},
            {"type": "function", "function": {"name": "finish", "parameters": {"type": "object"}}},
        ):
            with self.subTest(finish=finish):
                prompt, tools = formatter.inject_tool_definitions([lookup, finish], "system")
                self.assertIsNone(tools)
                self.assertIn("必须调用 finish", prompt)
                self.assertNotIn("无需调用 finish", prompt)


class PairingMigrationTests(unittest.TestCase):
    @staticmethod
    def assistant():
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "name": "first", "arguments": {}},
                {"id": "b", "name": "second", "arguments": {}},
            ],
            "_meta": {"tool_mode": "native", "metadata": {"example": {"value": "original"}}},
        }

    def test_missing_ids_and_names_are_recovered(self):
        for unnamed in ({"content": "first output"}, {"tool_call_id": "", "name": "", "content": "first output"}):
            with self.subTest(unnamed=unnamed):
                messages = [
                    self.assistant(),
                    {"role": "tool", "name": "second", "content": "second output"},
                    {"role": "tool", **unnamed},
                ]
                before = copy.deepcopy(messages)
                repaired = repair_tool_result_pairing(messages)
                self.assertEqual(
                    [(row["tool_call_id"], row["name"], row["content"]) for row in repaired[1:]],
                    [("a", "first", "first output"), ("b", "second", "second output")],
                )
                self.assertEqual(messages, before)

    def test_wrong_id_does_not_match_by_name(self):
        messages = [
            self.assistant(),
            {"role": "tool", "tool_call_id": "wrong", "name": "first", "content": "wrong output"},
            {"role": "tool", "tool_call_id": "b", "name": "second", "content": "valid output"},
        ]
        before = copy.deepcopy(messages)
        repaired = prepare_raw_history(messages)
        self.assertEqual([call["id"] for call in repaired[0]["tool_calls"]], ["b"])
        self.assertEqual(repaired[0]["_meta"]["tool_call_ids"], ["a", "b"])
        self.assertEqual([row["tool_call_id"] for row in repaired[1:]], ["b"])
        self.assertNotIn("wrong output", json.dumps(repaired))
        self.assertEqual(messages, before)

    def test_results_follow_call_order_and_input_remains_isolated(self):
        messages = [
            self.assistant(),
            {"role": "tool", "tool_call_id": "b", "name": "stale", "content": "  second\n"},
            {"role": "tool", "tool_call_id": "a", "name": "", "content": "\tfirst\n"},
        ]
        before = copy.deepcopy(messages)
        repaired = prepare_raw_history(messages)
        self.assertEqual([row["tool_call_id"] for row in repaired[1:]], ["a", "b"])
        self.assertEqual([row["name"] for row in repaired[1:]], ["first", "second"])
        self.assertEqual([row["content"] for row in repaired[1:]], ["\tfirst\n", "  second\n"])
        self.assertEqual(prepare_raw_history(repaired), repaired)
        repaired[0]["_meta"]["metadata"]["example"]["value"] = "changed"
        self.assertEqual(messages, before)

    def test_native_output_accepts_storage_and_api_tool_calls(self):
        for call in (
            {"id": "a", "name": "lookup", "arguments": {"query": "example"}},
            {"id": "a", "type": "function", "function": {"name": "lookup", "arguments": '{"query":"example"}'}},
        ):
            with self.subTest(call=call):
                messages = [
                    {"role": "assistant", "content": "", "tool_calls": [call], "_meta": {"metadata": {"example": {"signed": True}}}},
                    {"role": "tool", "tool_call_id": "a", "content": "found"},
                ]
                before = copy.deepcopy(messages)
                rendered = build_llm_messages(messages, NativeToolFormatter(), preserve_metadata=True)
                self.assertEqual(rendered[0]["tool_calls"][0]["function"]["name"], "lookup")
                self.assertEqual(json.loads(rendered[0]["tool_calls"][0]["function"]["arguments"]), {"query": "example"})
                self.assertEqual(rendered[0]["_meta"]["tool_call_ids"], ["a"])
                self.assertEqual(rendered[0]["_meta"]["metadata"], messages[0]["_meta"]["metadata"])
                self.assertEqual(rendered[1]["name"], "lookup")
                without_meta = build_llm_messages(messages, NativeToolFormatter())
                self.assertNotIn("_meta", without_meta[0])
                self.assertEqual(messages, before)


if __name__ == "__main__":
    unittest.main()
