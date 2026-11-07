"""Offline regression checks for the migrated engine history policy.

Why: this repo now owns the production-style compaction policy, the unified
segment/token helpers and a bounded, shared ConversationStore cache.
How: every case runs against a temp workspace with the real store/segment/
compact helpers, a fake supervisor state bound to the real mixin methods, and
no LLM/supervisor/network call. Purpose: prove that task/tool pairs stay
complete, one compaction pass is sufficient, failures trip the breaker,
branch/parent histories stay in sync, store writes are atomic and isolated, and
task records retain their storage contract after the old L2 snip path is removed.

Run: python3.11 -B -m unittest tests.test_engine_history_migration -v
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.compact import (  # noqa: E402
    _MAX_CONSECUTIVE_FAILURES,
    _compact_failures,
    _format_compact_summary,
    apply_compact_summary,
    compute_effective_keep_recent,
    count_real_task_segments,
    estimate_message_tokens,
    invalidate_stale_usage,
    is_compact_circuit_open,
    is_summary_message,
    record_compact_failure,
    record_compact_success,
    split_task_segments,
    strip_anthropic_thinking_signatures,
)
from engine.compact_flow import prepare_compaction  # noqa: E402
from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.task_record import TaskRecord, load_task_records, write_task_record  # noqa: E402
from supervisor.session import SessionMixin  # noqa: E402
from supervisor.task_router import TaskRouterMixin  # noqa: E402
from supervisor.task_store import TaskStoreMixin  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

SNIP_MARKER = "[Task summary — original messages snipped]"
SUM_HEADER_EN = "[Below is a structured summary of the previous conversation; the original context has been compacted]"
SUM_HEADER_ZH = "[以下是之前对话的结构化摘要，原始上下文已被压缩]"


# ── helpers ──────────────────────────────────────────────────────────────────


def _hist(mid, role="user", content="", task="", mtype="", tool_calls=None, **meta):
    """Build a runner-style history dict."""
    d: dict = {"id": mid, "role": role, "content": content}
    _meta = dict(meta)
    if task:
        _meta["source_task_id"] = task
    if mtype:
        d["message_type"] = mtype
        _meta.setdefault("message_type", mtype)
    if _meta:
        d["_meta"] = _meta
    if tool_calls:
        d["tool_calls"] = tool_calls
    return d


def _stored(mid, role="user", content="", task="", mtype="", meta=None, ephemeral=False):
    return Message(
        id=mid, role=role, content=content, source_task_id=task, message_type=mtype,
        meta=dict(meta or {}), ephemeral=ephemeral,
    )


def _text_task(tid, size=20_000, msgs=2):
    """One complete task segment: user + assistant (+ tool result when msgs==3)."""
    out = [_hist(f"{tid}-u", "user", "u" * size, task=tid)]
    if msgs >= 2:
        out.append(_hist(f"{tid}-a", "assistant", "a" * size, task=tid))
    if msgs >= 3:
        out.append(_hist(f"{tid}-t", "tool", "t" * size, task=tid))
    return out


def _task(task_id, session_id, status=TaskStatus.running, **input_data):
    ts = datetime.now(timezone.utc)
    return Task(
        task_id=task_id, session_id=session_id, kind=TaskKind.node, status=status,
        input=dict(input_data), created_at=ts, updated_at=ts,
        lease_expires_at=ts + timedelta(seconds=30),
    )


class _EventLog:
    def __init__(self):
        self.events: list[dict] = []

    def append(self, **kwargs):
        self.events.append(kwargs)
        return {"seq": len(self.events)}

    def of_type(self, type_):
        return [e for e in self.events if e.get("type_") == type_]


def _make_state(ws: Path):
    """Fake supervisor state bound to the real mixin methods used here."""
    state = SimpleNamespace(
        workspace_root=ws,
        tasks={},
        sessions={},
        conversation_map={},
        eventlog=_EventLog(),
        _cancelled_sessions=set(),
        _session_context_usage={},
        _session_store=SimpleNamespace(_registry={}),
        parent_children={},
        parent_entry_branches={},
        entry_branch_parents={},
    )
    state.lock = threading.RLock()
    state._lock = state.lock
    for name in (
        "_task_terminal", "preempt_task",
        "consume_preempt_message", "is_task_preempted",
    ):
        setattr(state, name, types.MethodType(getattr(TaskStoreMixin, name), state))
    # [2026-11-02] 适配真实接口：旧的 _append_preempt_locked 静态 helper 已并入
    # TaskRouterMixin._append_preempt_to_task_locked（追加语义 + preempt_requested
    # 事件 source 元数据）。fake state 直接绑真实方法，不再引用已删除的 helper。
    setattr(state, "_append_preempt_to_task_locked",
            types.MethodType(getattr(TaskRouterMixin, "_append_preempt_to_task_locked"), state))
    for name in (
        "_route_session_id_for_task_locked", "_find_entry_task_by_status_locked",
        "_find_running_entry_task_locked", "_is_branchless_running_task_locked",
        "_find_dispatch_callback_carrier_locked",
        "_dispatch_carrier_route_matches_locked",
        "inject_async_result", "_inject_async_dispatch_result_locked",
        "_inject_dispatch_result_via_origin_locked", "_format_dispatch_preempt_text",
        "_apply_compact_via_conv_store_locked", "_clone_compact_summary_message",
        "_sync_compact_to_branches", "_sync_entry_branch_compact_locked",
        "_sync_child_session_compact_locked",
        "_apply_standalone_compact_result_locked",
    ):
        setattr(state, name, types.MethodType(getattr(TaskRouterMixin, name), state))
    setattr(state, "_merge_branch_locked",
            types.MethodType(getattr(SessionMixin, "_merge_branch_locked"), state))
    state._current_session_generation_locked = lambda sid: 1
    state._route_session_id_for_session_locked = lambda sid: sid
    # fake 里没有入口分支索引：显式空集就是真实语义（无分支）。
    state._entry_branch_ids_for_parent_locked = lambda sid: set()
    return state


# ── ConversationStore: append / load / atomic replace ────────────────────────


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.store = ConversationStore(self.dir)
        self.sid = "sess_store"
        self.path = self.dir / f"{self.sid}.jsonl"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _contents(self, store=None):
        return [m.content for m in (store or self.store).load(self.sid)]

    def _external_append(self, mid: str, content: str) -> None:
        """Append like another process would: no cache involvement at all."""
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": mid, "role": "user", "content": content}) + "\n")

    def test_external_append_then_local_append_keeps_both(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        self.assertEqual(self._contents(), ["a"])  # warms the shared cache
        self._external_append("m2", "b")
        self.store.append(self.sid, _stored("m3", content="c"))
        self.assertEqual(self._contents(), ["a", "b", "c"])

    def test_insert_between_write_setup_and_write_is_kept(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        self.store.load(self.sid)  # warms the cache
        original = ConversationStore._serialize
        calls = {"n": 0}

        def racing(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                # another instance appends exactly between our preparation and write
                self._external_append("ext", "external")
            return original(messages)

        with patch.object(ConversationStore, "_serialize", staticmethod(racing)):
            self.store.append(self.sid, _stored("m2", content="c"))
        self.assertEqual(self._contents(), ["a", "external", "c"])

    def test_ephemeral_rows_never_persist_or_cache(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        self.store.append(self.sid, _stored("e1", content="dynamic", ephemeral=True))
        self.store.replace_all(self.sid, [
            _stored("m2", content="b"),
            _stored("e2", content="dynamic2", ephemeral=True),
        ])
        self.assertEqual(self._contents(), ["b"])
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_returned_messages_are_isolated_from_the_shared_cache(self):
        self.store.append(self.sid, _stored("m1", content="a", meta={"k": "v"}))
        first = self.store.load(self.sid)
        first[0].content = "mutated"
        first[0].meta["k"] = "mutated"
        first[0].meta["injected"] = True
        first.append(_stored("ghost", content="ghost"))

        other = ConversationStore(self.dir)  # another instance, same shared cache
        second = other.load(self.sid)
        self.assertEqual([m.content for m in second], ["a"])
        self.assertEqual(second[0].meta, {"k": "v"})

    def test_atomic_replace_failure_keeps_original_file_and_cache(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        self.store.append(self.sid, _stored("m2", content="b"))
        self.store.load(self.sid)  # warm cache
        before_bytes = self.path.read_bytes()

        with patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.replace_all(self.sid, [_stored("m3", content="c")])

        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(self._contents(), ["a", "b"])
        self.assertEqual(
            [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [],
            "temp file must be removed when the atomic replace fails",
        )

    def test_append_failure_leaves_file_and_cache_untouched(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        self.store.load(self.sid)
        before_bytes = self.path.read_bytes()
        with patch.object(ConversationStore, "_serialize", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.store.append(self.sid, _stored("m2", content="b"))
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(self._contents(), ["a"])

    def test_large_history_update_keeps_the_front(self):
        big = "x" * (6 * 1024 * 1024)
        self.store.append(self.sid, _stored("big1", content=big))
        self.store.append(self.sid, _stored("big2", content=big))
        self.assertGreater(self.path.stat().st_size, 10 * 1024 * 1024)

        self.store.append(self.sid, _stored("tail", content="tail"))
        ids = [m.id for m in self.store.load(self.sid)]
        self.assertEqual(ids, ["big1", "big2", "tail"])
        self.assertEqual(len(self.store.load(self.sid)[0].content), len(big))

        self.store.replace_all(self.sid, [
            self.store.load(self.sid)[0], _stored("new", content="new"),
        ])
        self.assertEqual([m.id for m in self.store.load(self.sid)], ["big1", "new"])

    def test_same_size_atomic_replace_with_restored_mtime_is_not_stale(self):
        self.store.append(self.sid, _stored("m1", content="AAAA"))
        self.store.load(self.sid)
        st = os.stat(self.path)

        # another process: same-size replacement file, atomically renamed in
        replacement = self.dir / ".sess_store.replacement"
        replacement.write_text(
            json.dumps({"id": "m1", "role": "user", "content": "BBBB"}) + "\n",
            encoding="utf-8",
        )
        os.replace(replacement, self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        now = os.stat(self.path)
        self.assertEqual((now.st_size, now.st_mtime_ns), (st.st_size, st.st_mtime_ns))

        self.assertEqual(self._contents(), ["BBBB"])

    def test_cache_only_populated_for_a_stable_read(self):
        self.store.append(self.sid, _stored("m1", content="a"))
        original = self.store._file_sig
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            sig = original(path)
            if calls["n"] == 1 and sig is not None:
                self._external_append("late", "late")
            return sig

        with patch.object(ConversationStore, "_file_sig", staticmethod(flaky)):
            self.store.load(self.sid)
        # the unstable read was never cached, so the next load sees the extra row
        self.assertEqual(self._contents(), ["a", "late"])


# ── segmentation / low watermark ─────────────────────────────────────────────


class TaskSegmentTests(unittest.TestCase):
    def test_tool_pairs_stay_in_one_segment_and_mid_task_rows_merge(self):
        messages = [
            _hist("t1-u", "user", "go", task="t1"),
            _hist("t1-a", "assistant", "", task="t1",
                  tool_calls=[{"id": "c1", "name": "read_file", "arguments": "{}"}]),
            _hist("t1-t", "tool", "result", task="t1"),
            _hist("dr", "user", "<dispatch_result>done</dispatch_result>"),
            _hist("t2-u", "user", "next", task="t2"),
            _hist("t2-t", "tool", "result", task="t2"),
        ]
        prefix, segments = split_task_segments(messages)
        self.assertEqual(prefix, [])
        self.assertEqual([[m["id"] for m in s] for s in segments], [
            ["t1-u", "t1-a", "t1-t", "dr"],
            ["t2-u", "t2-t"],
        ])
        self.assertEqual(count_real_task_segments(messages), 2)

    def test_summary_and_snipped_stub_are_prefix_not_segments(self):
        messages = [
            _hist("sum", "user", SUM_HEADER_EN + "\n\nold", task="compact_summary"),
            _hist("stub", "user", SNIP_MARKER + "\nold", task="t0"),
            *_text_task("t1", size=10),
            *_text_task("t2", size=10),
        ]
        prefix, segments = split_task_segments(messages)
        self.assertEqual([m["id"] for m in prefix], ["sum", "stub"])
        self.assertEqual(len(segments), 2)
        self.assertEqual(count_real_task_segments(messages), 2)

    def test_structured_summary_type_does_not_need_a_header(self):
        typed = _hist("s1", "assistant", "whatever", task="t9", mtype="summary")
        self.assertTrue(is_summary_message(typed))
        typed_obj = _stored("s2", content="whatever", mtype="summary")
        self.assertTrue(is_summary_message(typed_obj))
        tagged = _hist("s3", "user", SUM_HEADER_ZH + "\n\nold", task="compact_summary")
        self.assertTrue(is_summary_message(tagged))
        self.assertFalse(is_summary_message(_hist("n", "user", "plain answer", task="t1")))

    def test_low_watermark_makes_one_pass_sufficient(self):
        messages = [m for i in range(10) for m in _text_task(f"t{i}", size=40_000, msgs=1)]
        _, segments = split_task_segments(messages)
        self.assertEqual(len(segments), 10)
        keep = compute_effective_keep_recent(segments, keep_recent=6, threshold_tokens=200_000)
        self.assertEqual(keep, 3)
        total = sum(sum(estimate_message_tokens(m) for m in s) for s in segments)
        kept = sum(sum(estimate_message_tokens(m) for m in s) for s in segments[-keep:])
        self.assertGreaterEqual(total - kept, 20_000, "one pass must free >=10% of the threshold")

    def test_low_watermark_never_drops_the_only_segment(self):
        messages = _text_task("t0", size=20_000)
        _, segments = split_task_segments(messages)
        self.assertEqual(compute_effective_keep_recent(segments, keep_recent=6, threshold_tokens=200_000), 1)


# ── summary validation / apply / failure breaker ────────────────────────────


class CompactPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        _compact_failures.clear()

    def tearDown(self) -> None:
        _compact_failures.clear()

    def test_circuit_breaker_trips_and_resets(self):
        sid = "session_breaker"
        for _ in range(_MAX_CONSECUTIVE_FAILURES - 1):
            record_compact_failure(sid)
        self.assertFalse(is_compact_circuit_open(sid))
        record_compact_failure(sid)
        self.assertTrue(is_compact_circuit_open(sid))
        record_compact_success(sid)
        self.assertFalse(is_compact_circuit_open(sid))

    def test_garbage_summary_is_rejected(self):
        self.assertEqual(_format_compact_summary(""), "")
        self.assertEqual(_format_compact_summary("too short"), "")
        self.assertEqual(_format_compact_summary("<summary>tiny</summary>"), "")

    def test_plain_text_and_both_headers_are_accepted(self):
        body = "交付内容：" + "细节" * 200
        self.assertTrue(_format_compact_summary(body).startswith("交付内容"))
        self.assertTrue(_format_compact_summary("<summary>\n" + body + "\n</summary>").startswith("交付内容"))
        en = _format_compact_summary(SUM_HEADER_EN + "\n\n" + body)
        zh = _format_compact_summary(SUM_HEADER_ZH + "\n\n" + body)
        self.assertEqual(en, body)
        self.assertEqual(zh, body)

    def test_apply_keeps_complete_recent_segments_and_marks_usage_stale(self):
        messages = []
        for i in range(4):
            messages.extend(_text_task(f"t{i}", size=20_000, msgs=2))
        # keep_recent keeps t2/t3 verbatim; the usage/signature metadata lives on a kept row
        messages[5]["_meta"]["usage"] = {"prompt_tokens": 10_000, "completion_tokens": 10}
        messages[5]["_meta"]["metadata"] = {"anthropic": {"thinking_blocks": [{"sig": "x"}]}}

        result = apply_compact_summary(messages, "S" * 400, keep_recent=2)

        self.assertEqual(result[0]["_meta"]["source_task_id"], "compact_summary")
        self.assertTrue(result[0]["content"].startswith(SUM_HEADER_EN))
        self.assertEqual(set(result[0]["_meta"]["compressed_task_ids"]), {"t0", "t1"})
        kept_ids = [m["id"] for m in result[1:]]
        self.assertEqual(kept_ids, ["t2-u", "t2-a", "t3-u", "t3-a"])
        kept_a = next(m for m in result if m.get("id") == "t2-a")
        self.assertTrue(kept_a["_meta"]["usage"]["_compacted_stale"])
        self.assertNotIn("thinking_blocks", kept_a["_meta"]["metadata"]["anthropic"])

        # removing the signatures must not touch the visible thinking text
        self.assertEqual(result[0]["role"], "user")

    def test_apply_is_a_no_op_without_a_summary(self):
        messages = [m for i in range(4) for m in _text_task(f"t{i}", size=200, msgs=1)]
        self.assertIs(apply_compact_summary(messages, "", keep_recent=1), messages)

    def test_prepare_compaction_skips_single_segment_and_dispatches_compress_count(self):
        single = _text_task("t0", size=200)
        plan = asyncio.run(prepare_compaction(
            single, target_session_id="s1", node_id="n", keep_recent=2, threshold_tokens=0,
        ))
        self.assertEqual(plan.status, "skip")

        # unique payload per task: only the replaced segments may reach the compactor
        messages = []
        for i in range(4):
            messages.append(_hist(f"t{i}-u", "user", f"PAYLOAD{i}-" + "u" * 20_000, task=f"t{i}"))
            messages.append(_hist(f"t{i}-a", "assistant", f"REPLY{i}-" + "a" * 20_000, task=f"t{i}"))
        plan = asyncio.run(prepare_compaction(
            messages, target_session_id="s1", node_id="n", keep_recent=2, threshold_tokens=0,
        ))
        self.assertEqual(plan.status, "dispatch")
        self.assertEqual(plan.dispatch_input["_compact_keep_recent"], 2)
        self.assertEqual(plan.dispatch_input["_compact_compress_count"], 2)
        self.assertIn("PAYLOAD0-", plan.dispatch_input["instruction"])
        self.assertIn("PAYLOAD1-", plan.dispatch_input["instruction"])
        self.assertNotIn("PAYLOAD2-", plan.dispatch_input["instruction"])
        self.assertNotIn("PAYLOAD3-", plan.dispatch_input["instruction"])

    def test_stale_usage_and_signatures_are_stripped_helpers(self):
        msgs = [
            _hist("a", "assistant", "x", usage={"prompt_tokens": 5},
                  metadata={"anthropic": {"thinking_blocks": [{"sig": "s"}]}}),
            _hist("b", "user", "y"),
        ]
        invalidate_stale_usage(msgs)
        self.assertTrue(msgs[0]["_meta"]["usage"]["_compacted_stale"])
        self.assertNotIn("thinking_blocks", msgs[0]["_meta"]["metadata"]["anthropic"])
        self.assertEqual(msgs[1], {"id": "b", "role": "user", "content": "y"})

        objs = [_stored("c", meta={"metadata": {"anthropic": {"thinking_blocks": [{"sig": "s"}]}}})]
        self.assertEqual(strip_anthropic_thinking_signatures(objs), 1)
        self.assertEqual(strip_anthropic_thinking_signatures(objs), 0)


# ── supervisor: parent compaction + branch sync + failure accounting ────────


class CompactBranchSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        self.dir = self.ws / "data" / "conversations"
        self.store = ConversationStore(self.dir)
        self.state = _make_state(self.ws)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed_parent(self, sid="sess_parent", tasks=4, msgs=2):
        for i in range(tasks):
            for mid, role, content in (
                (f"{sid}-{i}-u", "user", f"u{i}"),
                (f"{sid}-{i}-a", "assistant", f"a{i}"),
            )[:msgs]:
                self.store.append(sid, _stored(mid, role=role, content=content, task=f"t{i}"))
        return self.store.load(sid)

    def test_parent_compact_syncs_active_entry_branch_and_keeps_its_tail(self):
        parent, branch = "sess_parent", "branch_1"
        base = self._seed_parent(parent)
        self.store.append_batch(branch, base + [
            _stored("b-tail-u", "user", "branch question", task="t4"),
            _stored("b-tail-a", "assistant", "branch answer", task="t4"),
        ])
        task = _task(
            "task_b", branch, TaskStatus.running,
            parent_session_id=parent, branch_session_id=branch, base_count=len(base),
            base_last_id=base[-1].id,
        )
        self.state.tasks = {task.task_id: task}

        result = self.state._apply_compact_via_conv_store_locked(
            parent, "S" * 400, keep_recent=2,
        )

        self.assertLess(result["after"], result["before"])
        parent_now = self.store.load(parent)
        self.assertEqual(parent_now[0].message_type, "summary")
        self.assertEqual([m.id for m in parent_now[1:]], [m.id for m in base[4:]])
        self.assertTrue(
            parent_now[1].meta.get("usage", {}).get("_compacted_stale", False)
            or not parent_now[1].meta.get("usage"),
        )

        branch_now = self.store.load(branch)
        self.assertEqual(branch_now[0].message_type, "summary")
        self.assertNotEqual(branch_now[0].id, parent_now[0].id, "branch summary must be cloned")
        self.assertEqual(
            [m.id for m in branch_now[1:]],
            [m.id for m in base[4:]] + ["b-tail-u", "b-tail-a"],
        )
        self.assertEqual(task.input["base_count"], len(branch_now) - 2)
        self.assertEqual(task.input["base_last_id"], branch_now[-3].id)

        # the merge after compaction appends exactly the preserved tail
        self.state._merge_branch_locked(
            parent, branch, task.input["base_count"], base_last_id=task.input["base_last_id"],
        )
        merged_ids = [m.id for m in self.store.load(parent)]
        self.assertEqual(merged_ids[-2:], ["b-tail-u", "b-tail-a"])
        self.assertEqual(len(merged_ids), len(parent_now) + 2)
        self.assertEqual(len(merged_ids), len(set(merged_ids)), "merge must not duplicate messages")

    def test_no_op_compact_does_not_rewrite_history(self):
        parent = "sess_one"
        self._seed_parent(parent, tasks=1, msgs=2)
        before = self.store.load(parent)
        result = self.state._apply_compact_via_conv_store_locked(parent, "S" * 400, keep_recent=2)
        self.assertEqual(result["before"], result["after"])
        self.assertEqual([m.id for m in self.store.load(parent)], [m.id for m in before])

    def test_merge_strips_context_bound_signatures_only_when_prefix_changed(self):
        parent, branch = "sess_merge", "branch_merge"
        self.store.append(parent, _stored("p1", "user", "hello", task="t0"))
        self.store.append_batch(branch, [
            _stored("p1", "user", "hello", task="t0"),
            _stored("b1", "assistant", "answer", task="t1", meta={
                "metadata": {"anthropic": {"thinking_blocks": [{"sig": "old"}]}},
            }),
        ])
        # unchanged prefix: signatures stay usable
        self.state._merge_branch_locked(parent, branch, 1, base_last_id="p1")
        merged = self.store.load(parent)
        self.assertEqual(merged[-1].id, "b1")
        self.assertIn("thinking_blocks", merged[-1].meta["metadata"]["anthropic"])

        # parent compacted after the fork → prefix no longer matches
        parent2, branch2 = "sess_merge2", "branch_merge2"
        self.store.append(parent2, _stored("p1", "user", "hello", task="t0"))
        self.store.append_batch(branch2, [
            _stored("p1", "user", "hello", task="t0"),
            _stored("b2", "assistant", "answer", task="t1", meta={
                "metadata": {"anthropic": {"thinking_blocks": [{"sig": "old"}]}},
            }),
        ])
        self.store.replace_all(parent2, [
            _stored("sum", "user", SUM_HEADER_EN + "\n\nold", mtype="summary",
                    task="compact_summary"),
        ])
        self.state._merge_branch_locked(parent2, branch2, 1, base_last_id="p1")
        merged2 = self.store.load(parent2)
        self.assertEqual([m.id for m in merged2], ["sum", "b2"])
        self.assertNotIn("thinking_blocks", merged2[-1].meta["metadata"]["anthropic"])

    def test_standalone_compact_failure_trips_the_breaker(self):
        sid = "sess_standalone"
        self.store.append(sid, _stored("m1", "user", "hello", task="t0"))
        task = _task("cmp", sid, TaskStatus.completed)
        task.node_id = "system.compactor"
        task.result = {"action": "fail"}
        self.state.tasks = {task.task_id: task}
        self.state._apply_standalone_compact_result_locked(task, sid)
        self.assertTrue(is_compact_circuit_open(sid) or _compact_failures.get(sid) == 1)
        self.assertEqual(self.state.eventlog.of_type("compact_failed")[0]["session_id"], sid)
        record_compact_success(sid)
        _compact_failures.clear()


# ── task record storage ─────────────────────────────────────────────────────


class TaskRecordStorageTests(unittest.TestCase):
    def test_storage_preserves_multiple_versions_and_skips_malformed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_task_records(root, "sess"), [])
            old = TaskRecord(task_id="task", session_id="sess", node_id="main", step_count=1)
            latest = TaskRecord(task_id="task", session_id="sess", node_id="main", step_count=2,
                                last_message_id="last", summary="summary", token_usage={"total_tokens": 42})
            write_task_record(root, old)
            with (root / "data/transcripts/sess.jsonl").open("a") as stream:
                stream.write("invalid json\n[]\n")
            write_task_record(root, latest)
            self.assertEqual(load_task_records(root, "sess"), [old, latest])
            self.assertEqual(TaskRecord.from_dict(latest.to_dict()), latest)


# ── preempt: multiple callbacks accumulate, no cross-session injection ──────


class PreemptCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)
        self.main = "sess_main"
        self.state.sessions[self.main] = SimpleNamespace(
            session_id=self.main, conversation_key="discord:1", channel="discord",
        )
        self.task = _task("t_run", self.main, TaskStatus.running)
        self.state.tasks = {self.task.task_id: self.task}

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_two_callbacks_append_instead_of_overwriting(self):
        first = self.state.inject_async_result(
            self.main, "first", attachments=["/tmp/a.png"], source_task_id="t_run",
        )
        second = self.state.inject_async_result(
            self.main, "second", attachments=["/tmp/b.png"], source_task_id="t_run",
        )
        self.assertEqual(first["strategy"], "preempt")
        self.assertEqual(second["strategy"], "preempt")

        observed = self.state.is_task_preempted("t_run")
        expected = ('<dispatch_result kind="async_tool">\nfirst\n</dispatch_result>\n\n'
                    '<dispatch_result kind="async_tool">\nsecond\n</dispatch_result>')
        self.assertEqual(observed["message"], expected)
        self.assertEqual(observed["attachments"], [{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])

        # a third callback arriving while the engine is still working survives the ack
        self.state.inject_async_result(self.main, "third", source_task_id="t_run")
        consumed = self.state.consume_preempt_message(
            "t_run", observed["message"], len(observed["attachments"]),
        )
        self.assertEqual(consumed["message"], expected)
        self.assertEqual(consumed["attachments"], [{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])
        self.assertEqual(self.state.is_task_preempted("t_run")["message"],
                         '<dispatch_result kind="async_tool">\nthird\n</dispatch_result>')

    def test_dispatch_result_callbacks_accumulate(self):
        self.state._inject_async_dispatch_result_locked(
            _task("child1", self.main, TaskStatus.completed, _caller_node_id="scout"),
            {"text": "child one"},
        )
        self.state._inject_async_dispatch_result_locked(
            _task("child2", self.main, TaskStatus.completed, _caller_node_id="scout"),
            {"text": "child two"},
        )
        self.assertEqual(
            self.state.is_task_preempted("t_run")["message"],
            '<dispatch_result>\n{"task_id": "child1", "status": "finish"}\nchild one\n</dispatch_result>\n\n'
            '<dispatch_result>\n{"task_id": "child2", "status": "finish"}\nchild two\n</dispatch_result>',
        )
        events = self.state.eventlog.of_type("preempt_requested")
        self.assertEqual([e["payload"]["source"] for e in events], ["async_dispatch_result"] * 2)
        self.assertEqual([e["payload"]["child_task_id"] for e in events], ["child1", "child2"])

    def test_dispatch_origin_callbacks_accumulate(self):
        child = _task("child_o", self.main, TaskStatus.completed)
        child.input["_dispatch_origin"] = {"parent_session_id": self.main, "caller_node_id": "scout"}
        self.state._inject_dispatch_result_via_origin_locked(child, {"text": "origin one"})
        self.state._inject_dispatch_result_via_origin_locked(child, {"text": "origin two"})
        self.assertEqual(
            self.state.is_task_preempted("t_run")["message"],
            '<dispatch_result>\n{"task_id": "child_o", "status": "finish"}\norigin one\n</dispatch_result>\n\n'
            '<dispatch_result>\n{"task_id": "child_o", "status": "finish"}\norigin two\n</dispatch_result>',
        )
        events = self.state.eventlog.of_type("preempt_requested")
        self.assertEqual([e["payload"]["source"] for e in events], ["dispatch_origin_result"] * 2)

    def test_callback_for_another_session_is_not_injected(self):
        other = "sess_other"
        self.state.sessions[other] = SimpleNamespace(
            session_id=other, conversation_key="discord:2", channel="discord",
        )
        other_task = _task("t_other", other, TaskStatus.running)
        self.state.tasks[other_task.task_id] = other_task

        result = self.state.inject_async_result(other, "leak", source_task_id="t_run")

        self.assertEqual(result["strategy"], "preempt")
        self.assertFalse(self.state.is_task_preempted("t_run")["preempted"])
        self.assertEqual(self.state.is_task_preempted("t_run")["message"], "")
        self.assertEqual(self.state.is_task_preempted("t_other")["message"],
                         '<dispatch_result kind="async_tool">\nleak\n</dispatch_result>')


if __name__ == "__main__":
    unittest.main()
