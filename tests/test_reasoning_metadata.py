"""Reasoning display metadata and historical UI compatibility checks.

Run: python3.11 -B tests/test_reasoning_metadata.py
All conversation files are fixtures in temporary directories.
"""
from __future__ import annotations

import copy
from dataclasses import fields
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.conversation_store import ConversationStore, Message
from engine.inference.message_model import (
    MessageMeta, get_message_meta, get_reasoning_text, set_message_meta,
)
from supervisor.session import SessionMixin


class ReasoningMetadataTests(unittest.TestCase):
    def test_only_one_display_content_field_is_declared(self):
        names = {item.name for item in fields(MessageMeta)}
        self.assertIn("reasoning", names)
        self.assertNotIn("thought_summary", names)
        self.assertNotIn("has_reasoning", names)
        self.assertNotIn("has_thinking", names)

    def test_new_serialization_omits_legacy_and_derived_fields(self):
        private = {"backend": {"raw_parts": [{"text": "raw", "thoughtSignature": "signature"}]}}
        meta = MessageMeta(
            provider="backend", reasoning="display text", metadata=private,
            reasoning_started_at="2026-10-31T00:00:01Z",
            reasoning_ended_at="2026-10-31T00:00:02Z",
        )
        encoded = meta.to_dict()
        self.assertEqual(encoded["reasoning"], "display text")
        self.assertEqual(encoded["metadata"], private)
        self.assertEqual(encoded["reasoning_started_at"], "2026-10-31T00:00:01Z")
        self.assertEqual(encoded["reasoning_ended_at"], "2026-10-31T00:00:02Z")
        for key in ("thought_summary", "thinking_text", "thinking", "has_reasoning", "has_thinking"):
            self.assertNotIn(key, encoded)

    def test_legacy_content_aliases_are_read_without_mutating_input(self):
        for key in ("reasoning", "thought_summary", "thinking_text", "thinking"):
            with self.subTest(key=key):
                data = {
                    key: "  preserved display text\n",
                    "has_reasoning": True, "has_thinking": True,
                    "reasoning_started_at": "2026-10-31T00:00:01Z",
                    "reasoning_ended_at": "2026-10-31T00:00:02Z",
                    "metadata": {"backend": {"opaque": ["unchanged"]}},
                }
                before = copy.deepcopy(data)
                meta = MessageMeta.from_dict(data)
                self.assertEqual(meta.reasoning, "  preserved display text\n")
                self.assertEqual(meta.reasoning_started_at, data["reasoning_started_at"])
                self.assertEqual(meta.reasoning_ended_at, data["reasoning_ended_at"])
                self.assertEqual(meta.metadata, data["metadata"])
                self.assertEqual(get_reasoning_text(data), meta.reasoning)
                self.assertEqual(MessageMeta.from_dict(meta.to_dict()).to_dict(), meta.to_dict())
                self.assertEqual(data, before)

    def test_distinct_reasoning_and_summary_are_both_retained(self):
        for primary in ("reasoning", "thinking_text", "thinking"):
            with self.subTest(primary=primary):
                data = {primary: "  first\n", "thought_summary": "\tsecond\n"}
                before = copy.deepcopy(data)
                self.assertEqual(get_reasoning_text(data), "  first\n\n\n\tsecond\n")
                self.assertEqual(MessageMeta.from_dict(data).reasoning, "  first\n\n\n\tsecond\n")
                self.assertEqual(data, before)

    def test_identical_summary_is_not_duplicated(self):
        for primary in ("reasoning", "thinking_text", "thinking"):
            with self.subTest(primary=primary):
                data = {primary: "same content", "thought_summary": "same content"}
                self.assertEqual(get_reasoning_text(data), "same content")
                self.assertEqual(MessageMeta.from_dict(data).to_dict(), {"reasoning": "same content"})

    def test_current_field_takes_precedence_over_its_legacy_aliases(self):
        data = {
            "reasoning": "current", "thinking_text": "old alias",
            "thinking": "older alias", "thought_summary": "summary",
        }
        self.assertEqual(get_reasoning_text(data), "current\n\nsummary")

    def test_flags_do_not_create_display_text_and_invalid_values_are_ignored(self):
        for data in (
            None, [], "invalid", {},
            {"has_reasoning": True, "has_thinking": True},
            {"reasoning": None, "thought_summary": None},
            {"reasoning": {"opaque": "not display text"}, "thought_summary": ["not text"]},
        ):
            with self.subTest(data=data):
                self.assertEqual(get_reasoning_text(data), "")
                self.assertEqual(MessageMeta.from_dict(data).reasoning, "")
        self.assertEqual(get_reasoning_text({"reasoning": None, "thinking_text": "legacy"}), "legacy")

    def test_message_helpers_normalize_only_the_new_message(self):
        source = {"role": "assistant", "_meta": {"thought_summary": "summary", "has_reasoning": False}}
        before = copy.deepcopy(source)
        recovered = get_message_meta(source)
        target = {"role": "assistant"}
        set_message_meta(target, recovered)
        self.assertEqual(target["_meta"], {"reasoning": "summary"})
        self.assertEqual(source, before)


class StructuredReasoningHistoryTests(unittest.TestCase):
    def read_history(self, metadata):
        """Exercise the actual structured-history reader against fixture JSONL."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "data" / "conversations"
            store = ConversationStore(directory)
            session_id = "reasoning-fixture"
            for index, meta in enumerate(metadata):
                store.append(session_id, Message(
                    id=f"m{index}", role="assistant", content="answer",
                    message_type="assistant", created_at="2026-10-31T00:00:00Z",
                    meta=copy.deepcopy(meta), source_node_id="test-node", source_task_id="test-task",
                ))
            files = list(directory.glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            before = files[0].read_bytes()
            state = SimpleNamespace(
                workspace_root=root, _lock=threading.RLock(),
                _entry_branch_ids_for_parent_locked=lambda session_id: set(),
            )
            result = SessionMixin.session_history_structured(state, session_id=session_id)
            self.assertEqual(files[0].read_bytes(), before, "History display must not rewrite the stored file")
            return result

    def test_legacy_summary_is_projected_into_existing_thinking_fields(self):
        meta = {
            "thought_summary": "saved summary", "has_reasoning": False,
            "reasoning_started_at": "2026-10-31T00:00:01Z",
            "reasoning_ended_at": "2026-10-31T00:00:03Z",
            "llm_request_id": "request", "provider": "backend",
            "metadata": {"backend": {"raw_parts": [{"thoughtSignature": "signature"}]}},
        }
        before = copy.deepcopy(meta)
        row = self.read_history([meta])[0]
        block = {"text": "saved summary", "started_at": meta["reasoning_started_at"], "ended_at": meta["reasoning_ended_at"]}
        self.assertEqual(row["thinking"], block)
        self.assertEqual(row["thinking_blocks"], [block])
        self.assertEqual(row["thinking_text"], "saved summary")
        self.assertEqual(row["reasoning_started_at"], block["started_at"])
        self.assertEqual(row["reasoning_ended_at"], block["ended_at"])
        self.assertEqual(row["provider_metadata"], meta["metadata"])
        self.assertEqual(row["llm_request_id"], "request")
        self.assertNotIn("thought_summary", row)
        self.assertNotIn("has_reasoning", row)
        self.assertEqual(meta, before)

    def test_both_forms_and_duplicates_follow_the_shared_display_rule(self):
        metadata = [
            {"reasoning": "body", "thought_summary": "summary"},
            {"reasoning": "same", "thought_summary": "same"},
            {"thinking_text": "old body", "thought_summary": "old summary"},
        ]
        before = copy.deepcopy(metadata)
        rows = self.read_history(metadata)
        self.assertEqual([row["thinking_text"] for row in rows], ["body\n\nsummary", "same", "old body\n\nold summary"])
        for row, meta in zip(rows, metadata):
            self.assertEqual(row["thinking_blocks"][0]["text"], get_reasoning_text(meta))
            self.assertEqual(row["thinking_blocks"][0]["started_at"], row["created_at"])
            self.assertEqual(row["thinking_blocks"][0]["ended_at"], row["created_at"])
            self.assertNotIn("thought_summary", row)
            self.assertNotIn("has_reasoning", row)
        self.assertEqual(metadata, before)

    def test_legacy_timestamps_are_preserved_and_flags_alone_are_not_displayed(self):
        rows = self.read_history([
            {"thinking": "legacy", "thinking_started_at": "2026-10-31T00:00:01Z", "thinking_ended_at": "2026-10-31T00:00:04Z"},
            {"has_reasoning": True, "has_thinking": True},
        ])
        self.assertEqual(rows[0]["thinking_blocks"], [{
            "text": "legacy", "started_at": "2026-10-31T00:00:01Z", "ended_at": "2026-10-31T00:00:04Z",
        }])
        self.assertEqual(rows[0]["reasoning_started_at"], "2026-10-31T00:00:01Z")
        self.assertEqual(rows[0]["reasoning_ended_at"], "2026-10-31T00:00:04Z")
        for key in ("thinking", "thinking_text", "thinking_blocks", "has_reasoning", "has_thinking", "thought_summary"):
            self.assertNotIn(key, rows[1])


if __name__ == "__main__":
    unittest.main()
