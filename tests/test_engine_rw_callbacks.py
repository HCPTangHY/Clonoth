"""Offline regression checks for the supervisor callback routing + turn-summary write-back.

Why: two callback sources (async dispatch / dispatch_origin) now deliver preempt
payloads wrapped in a machine tag, while their inbound path keeps the raw child
text; the async-tool callback mirrors that split with kind="async_tool" and a
message_type marker; turn-summary write-back must land on the newest TaskRecord.
How: bind the real TaskRouterMixin / TaskStoreMixin methods onto a fake
supervisor state, stub only the inbound boundary helpers, and run against a
temp workspace — no LLM, no network, no real supervisor state.
Purpose: prove the preempt/inbound split, raw-body preservation, same/cross
session isolation, append (non-overwrite) semantics, and the newest-record plus
50-char threshold rules of the summary write-back.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B tests/test_engine_rw_callbacks.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.task_record import TaskRecord, load_task_records, write_task_record  # noqa: E402
from supervisor.task_router import TaskRouterMixin  # noqa: E402
from supervisor.task_store import TaskStoreMixin  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _temporary_directory():
    directory = ROOT / 'data/temp'
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='rw-callback-', dir=directory)


def _task(task_id, session_id, status=TaskStatus.running, **input_data):
    ts = datetime.now(timezone.utc)
    return Task(
        task_id=task_id, session_id=session_id, kind=TaskKind.node, status=status,
        input=dict(input_data), created_at=ts, updated_at=ts,
        lease_expires_at=ts + timedelta(seconds=30),
    )


def _session(sid, conv="discord:1"):
    return SimpleNamespace(session_id=sid, conversation_key=conv, channel="discord")


class _EventLog:
    def __init__(self):
        self.events: list[dict] = []

    def append(self, **kwargs):
        self.events.append(kwargs)
        return {"seq": len(self.events)}

    def of_type(self, type_):
        return [e for e in self.events if e.get("type_") == type_]


def _unwrap(text: str):
    """Split a <dispatch_result ...> payload into (kind, meta dict, raw body).

    The dispatch wrapper carries a JSON metadata line; the async-tool wrapper only
    carries the raw tool text, so the meta line is optional.
    """
    lines = text.split("\n")
    assert lines[0].startswith("<dispatch_result"), text
    assert lines[-1] == "</dispatch_result>", text
    head = lines[0][len("<dispatch_result"):].strip()
    kind = head.split('"')[1] if '"' in head else ""
    body_lines = lines[1:-1]
    meta: dict = {}
    if body_lines:
        try:
            parsed = json.loads(body_lines[0])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
            body_lines = body_lines[1:]
    return kind, meta, "\n".join(body_lines)


def _make_state(ws: Path):
    """Fake supervisor state bound to the real mixin methods exercised here."""
    state = SimpleNamespace(
        workspace_root=ws,
        tasks={},
        sessions={},
        eventlog=_EventLog(),
        _cancelled_sessions=set(),
        parent_children={},
        parent_entry_branches={},
        entry_branch_parents={},
        inbound_payloads=[],
    )
    state.lock = threading.RLock()
    state._lock = state.lock
    for name in (
        "_task_terminal", "preempt_task",
        "consume_preempt_message", "is_task_preempted",
    ):
        setattr(state, name, types.MethodType(getattr(TaskStoreMixin, name), state))
    # [2026-11-02] preempt 追加语义已统一到 TaskStoreMixin.preempt_task 内部，
    # 旧的 _append_preempt_locked 静态 helper 已删除；回调路径只依赖 store 的公开
    # 接口（preempt_task / consume_preempt_message），不要再绑定已不存在的 helper。
    for name in (
        "_route_session_id_for_task_locked",
        "_dispatch_carrier_route_matches_locked",
        "_find_dispatch_callback_carrier_locked",
        "_find_entry_task_by_status_locked",
        "_find_running_entry_task_locked",
        "_is_branchless_running_task_locked",
        "_format_dispatch_preempt_text",
        "_append_preempt_to_task_locked",
        "inject_async_result",
        "_inject_async_dispatch_result_locked",
        "_inject_dispatch_result_via_origin_locked",
        "_apply_turn_summary_result_locked",
    ):
        setattr(state, name, types.MethodType(getattr(TaskRouterMixin, name), state))
    state._current_session_generation_locked = lambda sid: 1
    # branch 集合：fake 里没有入口分支索引，显式给出空集即真实语义（无分支）。
    state._entry_branch_ids_for_parent_locked = lambda sid: set()
    # Inbound boundary (session/task_store mixins, out of scope here): record only.
    state._apply_inbound_message = lambda **kwargs: None
    state._advance_inbound_cursor = lambda: None
    state._create_entry_task_for_inbound_locked = (
        lambda **kwargs: state.inbound_payloads.append(kwargs["payload"])
    )
    return state


class _CallbackBase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)
        self.main = "sess_main"
        self.other = "sess_other"
        self.state.sessions[self.main] = _session(self.main, "discord:1")
        self.state.sessions[self.other] = _session(self.other, "discord:2")
        self.runner = _task("t_run", self.main, TaskStatus.running)
        self.state.tasks = {self.runner.task_id: self.runner}

    def tearDown(self) -> None:
        self._td.cleanup()

    def _preempt(self, task_id="t_run"):
        return self.state.is_task_preempted(task_id)


# ── async dispatch callback: preempt vs inbound ──────────────────────────────


class AsyncDispatchCallbackTests(_CallbackBase):
    def test_branchless_running_task_gets_tagged_preempt(self):
        child = _task("child1", self.main, TaskStatus.completed, _caller_node_id="scout")
        child.node_id = "scout"
        child.result = {"action": "finish"}
        child.input["instruction"] = "find the changelog"

        self.state._inject_async_dispatch_result_locked(child, {"text": "child one"})

        kind, meta, body = _unwrap(self._preempt()["message"])
        self.assertEqual(kind, "", "dispatch callbacks use the un-kinded wrapper")
        self.assertEqual(meta["source"], "scout")
        self.assertEqual(meta["task_id"], "child1")
        self.assertEqual(meta["status"], "finish")
        self.assertEqual(meta["instruction"], "find the changelog")
        self.assertEqual(body, "child one", "the child result must survive verbatim")
        self.assertEqual(self.state.inbound_payloads, [], "preempt path must not fork")

        evt = self.state.eventlog.of_type("preempt_requested")[0]["payload"]
        self.assertEqual(evt["source"], "async_dispatch_result")
        self.assertEqual(evt["child_task_id"], "child1")
        self.assertTrue(evt["has_message"])

    def test_no_running_task_falls_back_to_raw_inbound(self):
        self.state.tasks = {}
        child = _task("child2", self.main, TaskStatus.completed, _caller_node_id="scout")
        child.node_id = "scout"

        self.state._inject_async_dispatch_result_locked(child, {"text": "child two"})

        self.assertEqual(len(self.state.inbound_payloads), 1)
        payload = self.state.inbound_payloads[0]
        self.assertEqual(payload["text"], "child two", "inbound body stays raw")
        self.assertEqual(payload["message_type"], "dispatch_result")
        self.assertEqual(payload["child_task_id"], "child2")
        self.assertEqual(payload["child_session_id"], "")
        self.assertNotIn("dispatch_result>", payload["text"])
        self.assertFalse(self._preempt()["preempted"])


class DispatchOriginCallbackTests(_CallbackBase):
    def _child(self, task_id="child_o"):
        child = _task(task_id, self.main, TaskStatus.completed)
        child.node_id = "scout"
        child.result = {"action": "finish"}
        child.input["_dispatch_origin"] = {
            "parent_session_id": self.main, "caller_node_id": "scout",
        }
        return child

    def test_branchless_running_task_gets_tagged_preempt(self):
        self.state._inject_dispatch_result_via_origin_locked(self._child(), {"text": "origin one"})

        kind, meta, body = _unwrap(self._preempt()["message"])
        self.assertEqual(kind, "")
        self.assertEqual(meta["task_id"], "child_o")
        self.assertEqual(body, "origin one")
        self.assertEqual(self.state.inbound_payloads, [])
        self.assertEqual(
            self.state.eventlog.of_type("preempt_requested")[0]["payload"]["source"],
            "dispatch_origin_result",
        )

    def test_no_running_task_falls_back_to_raw_inbound(self):
        self.state.tasks = {}
        self.state._inject_dispatch_result_via_origin_locked(self._child(), {"text": "origin two"})

        payload = self.state.inbound_payloads[0]
        self.assertEqual(payload["text"], "origin two")
        self.assertEqual(payload["message_type"], "dispatch_result")
        self.assertEqual(payload["child_task_id"], "child_o")
        self.assertFalse(self._preempt()["preempted"])

    def test_missing_parent_session_is_skipped(self):
        self.state.tasks = {}
        child = self._child()
        child.input["_dispatch_origin"] = {"caller_node_id": "scout"}
        self.state._inject_dispatch_result_via_origin_locked(child, {"text": "nowhere"})
        self.assertEqual(self.state.inbound_payloads, [])
        self.assertFalse(self._preempt()["preempted"])


# ── async tool callback: tagged preempt, message_type inbound ───────────────


class AsyncToolCallbackTests(_CallbackBase):
    def test_preempt_uses_async_tool_kind_and_keeps_raw_body(self):
        first = self.state.inject_async_result(
            self.main, "first", attachments=["/tmp/a.png"], source_task_id="t_run",
        )
        self.assertEqual(first["strategy"], "preempt")
        self.assertEqual(first["task_id"], "t_run")

        kind, _meta, body = _unwrap(self._preempt()["message"])
        self.assertEqual(kind, "async_tool")
        self.assertEqual(body, "first", "tool output must be preserved verbatim")
        self.assertEqual(self._preempt()["attachments"], [{"path": "/tmp/a.png"}])
        self.assertEqual(
            self.state.eventlog.of_type("preempt_requested")[0]["payload"]["source"],
            "async_tool_result",
        )

    def test_inbound_payload_carries_async_tool_message_type(self):
        self.state.tasks = {}
        result = self.state.inject_async_result(self.main, "tool says hi")

        self.assertEqual(result, {"ok": True, "strategy": "inbound"})
        payload = self.state.inbound_payloads[0]
        self.assertEqual(payload["text"], "tool says hi", "inbound body stays raw")
        self.assertEqual(payload.get("message_type"), "async_tool_result")
        self.assertEqual(payload["message_id"], f"async_tool_result:{self.main}")

    def test_unknown_session_is_rejected(self):
        result = self.state.inject_async_result("sess_nope", "text")
        self.assertEqual(result, {"ok": False, "error": "session not found"})
        self.assertEqual(self.state.inbound_payloads, [])


# ── append semantics + same/cross session isolation ─────────────────────────


class CallbackIsolationTests(_CallbackBase):
    def test_dispatch_callbacks_append_instead_of_overwriting(self):
        for tid, text in (("child1", "child one"), ("child2", "child two")):
            child = _task(tid, self.main, TaskStatus.completed, _caller_node_id="scout")
            self.state._inject_async_dispatch_result_locked(child, {"text": text})

        first, second = self._preempt()["message"].split("\n\n")
        self.assertEqual(_unwrap(first)[2], "child one")
        self.assertEqual(_unwrap(second)[2], "child two")
        events = self.state.eventlog.of_type("preempt_requested")
        self.assertEqual([e["payload"]["child_task_id"] for e in events], ["child1", "child2"])

    def test_async_tool_callbacks_append_and_consume_only_observed_prefix(self):
        self.state.inject_async_result(
            self.main, "first", attachments=["/tmp/a.png"], source_task_id="t_run",
        )
        self.state.inject_async_result(
            self.main, "second", attachments=["/tmp/b.png"], source_task_id="t_run",
        )
        observed = self._preempt()
        self.assertEqual(len(observed["message"].split("\n\n")), 2)
        self.assertEqual(observed["attachments"], [{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])

        # a third callback arriving while the engine still works survives the ack
        self.state.inject_async_result(self.main, "third", source_task_id="t_run")
        consumed = self.state.consume_preempt_message(
            "t_run", observed["message"], len(observed["attachments"]),
        )
        self.assertEqual(consumed["message"], observed["message"])
        self.assertEqual(_unwrap(self._preempt()["message"])[2], "third")

    def test_source_task_from_another_session_is_not_hijacked(self):
        other_task = _task("t_other", self.other, TaskStatus.running)
        self.state.tasks[other_task.task_id] = other_task

        result = self.state.inject_async_result(self.other, "leak", source_task_id="t_run")

        self.assertEqual(result["strategy"], "preempt")
        self.assertEqual(result["task_id"], "t_other")
        self.assertFalse(self._preempt()["preempted"], "cross-session source task stays clean")
        self.assertEqual(self._preempt()["message"], "")
        self.assertEqual(_unwrap(self._preempt("t_other")["message"])[2], "leak")

    def test_cross_session_callback_falls_back_to_its_own_inbound(self):
        result = self.state.inject_async_result(self.other, "leak", source_task_id="t_run")

        self.assertEqual(result["strategy"], "inbound")
        self.assertEqual(self.state.inbound_payloads[0]["text"], "leak")
        self.assertFalse(self._preempt()["preempted"])
        self.assertEqual(self.state.inbound_payloads[0]["conversation_key"], "discord:2")

    def test_branch_task_is_not_preempted_from_the_parent_session(self):
        branch_task = _task(
            "t_branch", self.main, TaskStatus.running, parent_session_id="sess_parent",
            branch_session_id="branch_1",
        )
        self.state.tasks = {branch_task.task_id: branch_task}

        result = self.state.inject_async_result(self.main, "for the branch")

        self.assertEqual(result["strategy"], "inbound", "branch mode keeps fork semantics")
        self.assertFalse(self._preempt("t_branch")["preempted"])
        self.assertEqual(self.state.inbound_payloads[0]["text"], "for the branch")

    def test_suspended_task_is_not_a_preempt_target(self):
        self.runner.status = TaskStatus.suspended
        result = self.state.inject_async_result(self.main, "late", source_task_id="t_run")
        self.assertEqual(result["strategy"], "inbound")
        self.assertFalse(self._preempt()["preempted"])


# ── turn-summary write-back ─────────────────────────────────────────────────


class TurnSummaryWriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)
        self.sid = "sess_sum"
        self.target = "t_target"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _record(self, summary="", node_id="main"):
        rec = TaskRecord(
            task_id=self.target, session_id=self.sid, node_id=node_id, summary=summary,
        )
        write_task_record(self.ws, rec)

    def _summarizer_task(self, text: str):
        task = _task("t_sum", self.sid, TaskStatus.completed)
        task.node_id = "system.turn_summarizer"
        task.input.update({
            "_turn_summary_dispatch": True,
            "_target_task_id": self.target,
            "_target_session_id": self.sid,
        })
        task.result = {"action": "finish", "result": {"text": text}}
        return task

    def test_summary_lands_on_the_newest_record(self):
        self._record("")
        self._record("first write-back")
        summary = "S" * 80

        self.state._apply_turn_summary_result_locked(self._summarizer_task(summary))

        records = load_task_records(self.ws, self.sid)
        self.assertEqual(len(records), 3, "the write-back appends exactly one record")
        self.assertEqual(records[0].summary, "", "the oldest record must stay untouched")
        self.assertEqual(records[1].summary, "first write-back")
        self.assertEqual(records[-1].summary, summary)
        self.assertEqual(records[-1].node_id, "main", "the newest record's fields are kept")

    def test_newest_record_is_selected_even_when_it_carries_other_fields(self):
        self._record("old")
        newest = TaskRecord(
            task_id=self.target, session_id=self.sid, node_id="main",
            action="finish", tool_call_count=7, summary="",
        )
        write_task_record(self.ws, newest)
        summary = "T" * 60

        self.state._apply_turn_summary_result_locked(self._summarizer_task(summary))

        records = load_task_records(self.ws, self.sid)
        self.assertEqual(records[0].summary, "old")
        self.assertEqual(records[-1].summary, summary)
        self.assertEqual(records[-1].tool_call_count, 7)

    def test_summary_under_fifty_chars_is_dropped(self):
        self._record("old")
        self.state._apply_turn_summary_result_locked(self._summarizer_task("S" * 49))

        records = load_task_records(self.ws, self.sid)
        self.assertEqual(len(records), 1, "a too-short summary must not be written")
        self.assertEqual(records[0].summary, "old")

    def test_summary_at_fifty_chars_is_kept(self):
        self._record("old")
        summary = "S" * 50
        self.state._apply_turn_summary_result_locked(self._summarizer_task(summary))
        self.assertEqual(load_task_records(self.ws, self.sid)[-1].summary, summary)

    def test_non_finish_summarizer_writes_nothing(self):
        self._record("old")
        task = self._summarizer_task("S" * 80)
        task.result = {"action": "fail", "error": "boom"}

        self.state._apply_turn_summary_result_locked(task)

        self.assertEqual(len(load_task_records(self.ws, self.sid)), 1)
        notice = self.state.eventlog.of_type("system_notice")
        self.assertEqual(notice[0]["payload"]["subsystem"], "turn_summary")


if __name__ == "__main__":
    unittest.main()
