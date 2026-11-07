"""Offline production-baseline compaction checks; temporary work stays in data/temp.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests -p test_compact_segment_migration.py -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.builtin import compact as builtin_compact
from engine.compact import _format_messages_for_summary, split_task_segments
from engine.compact_flow import CompactPlan
from engine.conversation_store import ConversationStore
from supervisor.task_router import TaskRouterMixin
from test_engine_history_migration import _hist, _make_state, _stored, _task


class SegmentBoundaryTests(unittest.TestCase):
    def test_preempt_splits_same_task_in_live_and_stored_history(self):
        for task_id in ("", "task"):
            with self.subTest(task_id=task_id):
                live = [
                    _hist("u1", "user", "first", task=task_id, mtype="user_input"),
                    _hist("a1", "assistant", "answer", task=task_id),
                    _hist("u2", "user", "follow-up", task=task_id, mtype="user_input", preempt=True),
                    _hist("a2", "assistant", "answer again", task=task_id),
                ]
                stored = [
                    _stored(row["id"], row["role"], row["content"], task="task",
                            mtype=row.get("message_type", ""), meta=row.get("_meta"))
                    for row in live
                ]
                _, live_segments = split_task_segments(live)
                _, stored_segments = split_task_segments(stored)
                self.assertEqual([[m["id"] for m in segment] for segment in live_segments],
                                 [["u1", "a1"], ["u2", "a2"]])
                self.assertEqual([[m.id for m in segment] for segment in stored_segments],
                                 [["u1", "a1"], ["u2", "a2"]])

    def test_rejection_retry_and_tool_attachment_do_not_start_turns(self):
        cases = [
            {"mtype": "output_rejected"}, {"mtype": "retry_hint"},
            {"mtype": "tool_result_attachment"}, {"is_rejection": True},
            {"rejected_draft": True},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                messages = [
                    _hist("u", "user", "first", task="task"),
                    _hist("a", "assistant", "draft", task="task"),
                    _hist("r", "user", "feedback", task="task", preempt=True, **fields),
                    _hist("a2", "assistant", "revised", task="task"),
                ]
                self.assertEqual(len(split_task_segments(messages)[1]), 1)
        for prefix in ("reject by ", "[SYSTEM REJECTED]", "[Execution Warning]",
                       "[Important Reminder]", "The tool executions above produced the following image results:"):
            with self.subTest(prefix=prefix):
                messages = [_hist("a", "assistant", "draft", task="task"),
                            _hist("r", "user", prefix + "detail", task="task", mtype="user_input")]
                self.assertEqual(len(split_task_segments(messages)[1]), 1)

    def test_pending_tool_call_and_its_result_stay_together(self):
        messages = [
            _hist("u", "user", "first", task="task"),
            _hist("a", "assistant", "", task="task",
                  tool_calls=[{"id": "call", "name": "read_file", "arguments": "{}"}]),
            _hist("t", "user", "tool output", task="task", mtype="user_input"),
            _hist("a2", "assistant", "done", task="task"),
            _hist("u2", "user", "next", task="task", preempt=True),
        ]
        self.assertEqual([[m["id"] for m in segment] for segment in split_task_segments(messages)[1]],
                         [["u", "a", "t", "a2"], ["u2"]])

    def test_typed_and_legacy_summary_remain_prefix(self):
        summaries = [
            _hist("typed", "assistant", "summary", task="old", mtype="summary"),
            _hist("legacy", "user", "[以下是之前对话的结构化摘要，原始上下文已被压缩]\nold"),
        ]
        prefix, segments = split_task_segments(summaries + [
            _hist("u", "user", "first", task="task"),
            _hist("a", "assistant", "answer", task="task"),
            _hist("u2", "user", "next", task="task", preempt=True),
        ])
        self.assertEqual([m["id"] for m in prefix], ["typed", "legacy"])
        self.assertEqual(len(segments), 2)

    def test_summary_input_uses_names_and_bounded_delivered_text(self):
        messages = [{"role": "assistant", "content": "visible text", "tool_calls": [
            {"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "ordinary-argument-only"})}},
            {"id": "finish", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"text": "x" * 3100})}},
            {"id": "ask", "name": "ask", "arguments": {"text": "question"}},
            {"id": "broken", "name": "ask", "arguments": "not-json"},
        ]}]
        messages.extend(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": "ok"}
            for call_id, name in (("read", "read_file"), ("finish", "finish"), ("ask", "ask"), ("broken", "ask"))
        )
        before = copy.deepcopy(messages)
        rendered = _format_messages_for_summary(messages)
        self.assertIn("visible text", rendered)
        self.assertIn("[used tool: read_file]", rendered)
        self.assertNotIn("ordinary-argument-only", rendered)
        self.assertIn("[delivered via finish] " + "x" * 3000, rendered)
        self.assertNotIn("x" * 3001, rendered)
        self.assertIn("[delivered via ask] question", rendered)
        self.assertIn("[used tool: ask]", rendered)
        self.assertEqual(messages, before)

    def test_automatic_compact_module_and_node_declaration_are_available(self):
        self.assertEqual(builtin_compact.PLUGIN_META["handler_class"], "CompactChecker")
        self.assertIn(("before_step", "handle"), builtin_compact.PLUGIN_META["hook_points"])
        self.assertEqual(builtin_compact.PLUGIN_META["nodes"], [{"file": "nodes/system.compactor.yaml"}])
        self.assertTrue((ROOT / "engine/builtin/nodes/system.compactor.yaml").is_file())
        self.assertFalse(hasattr(builtin_compact, "microcompact_store"))


class BranchCompactionTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "data/temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="compact-segments-", dir=temp_root)
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.store = ConversationStore(self.workspace / "data/conversations")
        self.state = _make_state(self.workspace)
        self.parent, self.branch = "segment-parent", "segment-branch"

    def seed_branch(self, *, base_rows=2):
        base = [_stored("p-u", "user", "old question", task="old"),
                _stored("p-a", "assistant", "old answer", task="old")][:base_rows]
        self.store.append_batch(self.parent, base)
        tail = [
            _stored("b-u1", "user", "first", task="task", mtype="user_input"),
            _stored("b-a1", "assistant", "answer", task="task"),
            _stored("b-u2", "user", "next", task="task", mtype="user_input", meta={"preempt": True}),
            _stored("b-a2", "assistant", "next answer", task="task"),
        ]
        self.store.append_batch(self.branch, base + tail)
        task = _task("task", self.branch, parent_session_id=self.parent,
                     branch_session_id=self.branch, base_count=len(base), base_last_id=base[-1].id)
        self.state.tasks[task.task_id] = task
        return task

    def apply(self, task):
        return self.state._apply_compact_via_conv_store_locked(
            self.parent, "summary", keep_recent=1, threshold_tokens=0,
            compress_count=1, has_active_branch=True, caller_branch_task=task,
        )

    def ids(self, session):
        return [message.id for message in self.store.load(session)]

    def test_write_failure_does_not_advance_cursor_or_prefill_parent(self):
        task = self.seed_branch()
        original_input = dict(task.input)
        parent = self.store._path(self.parent).read_bytes()
        branch = self.store._path(self.branch).read_bytes()
        with patch("engine.conversation_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply(task)
        self.assertEqual(task.input, original_input)
        self.assertEqual(self.store._path(self.parent).read_bytes(), parent)
        self.assertEqual(self.store._path(self.branch).read_bytes(), branch)

    def test_no_op_does_not_prefill_parent_or_advance_cursor(self):
        task = self.seed_branch(base_rows=1)
        original_input = dict(task.input)
        parent, branch = self.ids(self.parent), self.ids(self.branch)
        result = self.apply(task)
        self.assertEqual(result["after"], result["before"])
        self.assertEqual(task.input, original_input)
        self.assertEqual(self.ids(self.parent), parent)
        self.assertEqual(self.ids(self.branch), branch)

    def test_compact_then_finalize_twice_does_not_duplicate_branch_turns(self):
        task = self.seed_branch()
        result = self.apply(task)
        self.assertLess(result["after"], result["before"])
        self.assertEqual(self.ids(self.parent)[1:], ["b-u1", "b-a1"])
        self.assertEqual(task.input["base_count"], 3)
        self.assertEqual(task.input["base_last_id"], "b-a1")
        self.assertEqual(task.input["orig_base_last_id"], "p-a")
        self.state._finalize_branch_task_locked = types.MethodType(
            TaskRouterMixin._finalize_branch_task_locked, self.state)
        self.state._cleanup_branch_locked = Mock()
        self.state._merge_branch_transcript_locked = Mock(return_value=0)
        self.state._finalize_branch_task_locked(task, merge=True)
        after_first = self.ids(self.parent)
        self.state._finalize_branch_task_locked(task, merge=True)
        self.assertEqual(after_first[1:], ["b-u1", "b-a1", "b-u2", "b-a2"])
        self.assertEqual(self.ids(self.parent), after_first)
        self.state._merge_branch_transcript_locked.assert_called_once()

    def test_branch_sync_failure_keeps_cursor_for_committed_parent(self):
        task = self.seed_branch()
        replace = ConversationStore.replace_all

        def fail_branch(store, session_id, messages):
            if session_id == self.branch:
                raise OSError("branch unavailable")
            return replace(store, session_id, messages)

        with patch.object(ConversationStore, "replace_all", fail_branch):
            self.apply(task)
        self.assertEqual(task.input["base_count"], 4)
        self.assertEqual(task.input["base_last_id"], "b-a1")
        self.assertEqual(task.input["orig_base_last_id"], "p-a")
        self.state._merge_branch_locked(
            self.parent, self.branch, task.input["base_count"],
            base_last_id=task.input["base_last_id"], orig_base_last_id=task.input["orig_base_last_id"],
        )
        self.assertEqual(self.ids(self.parent)[1:], ["b-u1", "b-a1", "b-u2", "b-a2"])

    def test_merge_finds_current_anchor_after_prefix_length_change(self):
        self.store.append_batch(self.parent, [_stored("p1"), _stored("p2")])
        self.store.append_batch(self.branch, [_stored("extra"), _stored("p1"), _stored("p2"), _stored("tail")])
        count = self.state._merge_branch_locked(self.parent, self.branch, 2, base_last_id="p2")
        self.assertEqual(count, 1)
        self.assertEqual(self.ids(self.parent), ["p1", "p2", "tail"])

    def test_merge_uses_original_anchor_if_current_anchor_is_absent(self):
        self.store.append(self.parent, _stored("new-summary", mtype="summary"))
        self.store.append_batch(self.branch, [_stored("p1"), _stored("p2"), _stored("tail")])
        count = self.state._merge_branch_locked(
            self.parent, self.branch, 1, base_last_id="new-summary", orig_base_last_id="p2",
        )
        self.assertEqual(count, 1)
        self.assertEqual(self.ids(self.parent), ["new-summary", "tail"])

    def test_precheck_counts_entry_branch_when_parent_is_empty(self):
        self.seed_branch()
        self.store.delete(self.parent)
        ls = SimpleNamespace(
            compacted=False, compact_threshold=1, last_prompt_tokens=10, compact_keep_recent=1,
            messages=[], node=SimpleNamespace(id="main"),
            rctx=SimpleNamespace(session_id=self.branch, parent_session_id=self.parent,
                                 child_session_id="", conversation_store=self.store, emit_event=AsyncMock()),
        )
        planner = AsyncMock(return_value=CompactPlan(status="skip"))
        with patch.object(builtin_compact, "prepare_compaction", planner):
            asyncio.run(builtin_compact._check_and_compact(SimpleNamespace(step=0), ls))
        planner.assert_awaited_once()
        self.assertEqual(planner.call_args.kwargs["target_session_id"], self.parent)


if __name__ == "__main__":
    unittest.main()
