"""Supervisor 任务/会话/调度审计修复的离线回归自检。

覆盖审计确认项：
1. task_router 使用 providers.tool_format（不再引用 engine.inference.tool_format）。
2. dispatch 回调承接第一层 origin task 必须属于 target_session 的规范化路由
   （拒绝跨会话，保留 branch / suspended+waiting 合法承接）。
3. TTL sweep 保护所有非终态 task 引用的 session，并统一清理 JSONL/transcripts/
   cache/registry/conversation_map（完成态 fresh/fork 保留至 TTL）。
4. scheduler catchup 在同一份加载快照里 once 只触发一次。
5. preempt ACK 严格校验（坏参数不清队列，只消费已观察前缀）；reset 保护调用
   任务正在使用的 child/dispatch/branch 历史。
6. compaction 写盘成功后才推进分支游标。

Why: 这些行为由 supervisor 与 engine 的半公开契约组成，只能靠 fake state 精准复现。
How: 全部使用临时目录 + SimpleNamespace 绑定真实 mixin 方法，不启动服务、不建真实 session。
Purpose: 单一文件守住审计修复点，避免后续生产覆盖再次静默回退。

Run: PYTHONPATH=$PWD TMPDIR=$PWD/data/temp python3.11 -B tests/test_supervisor_audit_fixes.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from supervisor.scheduler import SchedulerThread  # noqa: E402
from supervisor.session import SessionMixin  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_router import TaskRouterMixin  # noqa: E402
from supervisor.task_store import TaskStoreMixin  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402


def _temporary_directory() -> tempfile.TemporaryDirectory:
    temp_root = ROOT / "data" / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="supervisor-audit-", dir=temp_root)


def _task(
    task_id: str, session_id: str, status: TaskStatus = TaskStatus.running, **input_data,
) -> Task:
    ts = datetime.now(timezone.utc)
    task = Task(
        task_id=task_id, session_id=session_id, kind=TaskKind.node, status=status,
        input=dict(input_data), created_at=ts, updated_at=ts,
        lease_expires_at=ts + timedelta(seconds=30),
    )
    task.node_id = "_worker"
    return task


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, **kwargs) -> dict:
        self.events.append(kwargs)
        return {"seq": len(self.events)}

    def of_type(self, type_: str) -> list[dict]:
        return [event for event in self.events if event.get("type_") == type_]


class _SessionStore:
    """sessions.json 的最小 fake（只覆盖生产实际调用的接口）。"""

    def __init__(self) -> None:
        self._registry: dict[str, dict] = {}
        self.resets: list[str] = []

    def _flush(self) -> None:
        pass

    def remove_session(self, session_id: str) -> None:
        self._registry.pop(session_id, None)

    def remove_sessions(self, session_ids) -> int:
        removed = 0
        for sid in list(session_ids or ()):
            if self._registry.pop(str(sid), None) is not None:
                removed += 1
        return removed

    def on_session_reset(self, session_id: str) -> None:
        self.resets.append(session_id)


# ── 1. tool_format 迁移 ──────────────────────────────────────────────────────


class ToolFormatImportTests(unittest.TestCase):
    def test_turn_summary_sanitizer_uses_the_providers_implementation(self) -> None:
        source = (ROOT / "supervisor" / "task_router.py").read_text(encoding="utf-8")
        self.assertNotIn("from engine.inference.tool_format import", source)
        self.assertIn("from providers.tool_format import sanitize_control_tool_history", source)
        from providers.tool_format import sanitize_control_tool_history
        self.assertTrue(callable(sanitize_control_tool_history))

    def test_turn_summary_formatter_runs_with_the_new_import(self) -> None:
        from supervisor.task_router import _format_task_messages_for_turn_summary
        rendered = _format_task_messages_for_turn_summary([
            {"role": "assistant", "content": "delivered", "tool_calls": [
                {"name": "finish", "arguments": '{"text": "answer"}'},
            ]},
        ])
        self.assertIn("delivered", rendered)
        self.assertIn("[delivered via finish] answer", rendered)


# ── 2. dispatch 回调承接的跨会话守卫 ────────────────────────────────────────


class DispatchCarrierRouteTests(unittest.TestCase):
    def _state(self, tasks: dict, branches: set[str] | None = None) -> SimpleNamespace:
        state = SimpleNamespace(tasks=tasks, _lock=threading.RLock())
        for name in ("_route_session_id_for_task_locked", "_find_dispatch_callback_carrier_locked",
                     "_dispatch_carrier_route_matches_locked"):
            setattr(state, name, types.MethodType(getattr(TaskRouterMixin, name), state))
        state._route_session_id_for_session_locked = lambda sid: sid
        state._entry_branch_ids_for_parent_locked = lambda sid: set(branches or set())
        return state

    def test_origin_in_another_session_is_not_a_carrier(self) -> None:
        origin = _task("origin", "sess_a")
        state = self._state({"origin": origin})
        self.assertIsNone(state._find_dispatch_callback_carrier_locked("origin", "sess_b"))

    def test_running_origin_in_the_same_session_is_the_carrier(self) -> None:
        origin = _task("origin", "sess_a")
        state = self._state({"origin": origin})
        self.assertIs(state._find_dispatch_callback_carrier_locked("origin", "sess_a"), origin)

    def test_suspended_origin_waiting_on_child_still_carries(self) -> None:
        origin = _task("origin", "sess_a", TaskStatus.suspended)
        origin.waiting_for_task_id = "child"
        state = self._state({"origin": origin})
        self.assertIs(state._find_dispatch_callback_carrier_locked("origin", "sess_a"), origin)

    def test_suspended_origin_without_waiter_is_not_a_carrier(self) -> None:
        origin = _task("origin", "sess_a", TaskStatus.suspended)
        state = self._state({"origin": origin})
        self.assertIsNone(state._find_dispatch_callback_carrier_locked("origin", "sess_a"))

    def test_branch_task_of_the_target_session_still_carries(self) -> None:
        origin = _task(
            "origin", "branch_1", parent_session_id="sess_a", branch_session_id="branch_1",
        )
        state = self._state({"origin": origin}, branches={"branch_1"})
        self.assertIs(state._find_dispatch_callback_carrier_locked("origin", "sess_a"), origin)

    def test_forked_carrier_marked_for_the_origin_is_still_found(self) -> None:
        origin = _task("origin", "sess_a", TaskStatus.completed)
        carrier = _task("forked", "sess_a", _dispatch_owner_root_task_id="origin")
        state = self._state({"origin": origin, "forked": carrier})
        self.assertIs(state._find_dispatch_callback_carrier_locked("origin", "sess_a"), carrier)


# ── 3. TTL sweep 的活跃保护与统一清理 ───────────────────────────────────────


class StaleSessionSweepTests(unittest.TestCase):
    _BOUND = (
        "_cleanup_stale_sessions_locked",
        "_task_referenced_session_ids_locked",
        "_task_terminal",
        "_purge_context_usage_for_session",
        "_remove_child_mapping_for_session_locked",
        "_delete_conversation_session_locked",
    )

    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.state = SimpleNamespace(
            workspace_root=self.ws,
            sessions={},
            session_generations={},
            tasks={},
            conversation_map={},
            child_session_map={},
            parent_children={},
            parent_entry_branches={},
            entry_branch_parents={},
            _cancelled_sessions=set(),
            _session_context_usage={},
            _session_store=_SessionStore(),
        )
        for name in self._BOUND:
            setattr(self.state, name, types.MethodType(getattr(SupervisorState, name), self.state))

    def tearDown(self) -> None:
        self._td.cleanup()

    def _add_dispatch_session(self, sid: str, *, hours_ago: float) -> str:
        conv_key = f"agent:_worker:web:{sid}"
        created = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        self.state._session_store._registry[sid] = {
            "session_id": sid, "channel": "web", "conversation_key": conv_key,
            "created_at": created, "last_active_at": created, "reset": False,
            "parent_session_id": "sess_parent", "context_mode": "fresh", "is_child": True,
        }
        self.state.conversation_map[conv_key] = sid
        self.state.sessions[sid] = SimpleNamespace(session_id=sid)
        self.state.session_generations[sid] = 1
        self.state.child_session_map[("sess_parent", "_worker", "")] = sid
        self.state.parent_children.setdefault("sess_parent", set()).add(sid)
        for kind in ("conversations", "transcripts"):
            path = self.ws / "data" / kind / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"type": "message", "role": "assistant", "content": "ok"}\n', encoding="utf-8")
        return conv_key

    def test_expired_session_drops_registry_files_map_and_transcript(self) -> None:
        conv_key = self._add_dispatch_session("sess_stale", hours_ago=30)
        self.state._cleanup_stale_sessions_locked()

        self.assertNotIn("sess_stale", self.state._session_store._registry)
        self.assertNotIn("sess_stale", self.state.sessions)
        self.assertNotIn("sess_stale", self.state.session_generations)
        self.assertNotIn(conv_key, self.state.conversation_map)
        self.assertNotIn("sess_stale", self.state.child_session_map.values())
        self.assertFalse((self.ws / "data" / "conversations" / "sess_stale.jsonl").exists())
        self.assertFalse((self.ws / "data" / "transcripts" / "sess_stale.jsonl").exists())

    def test_session_referenced_by_a_non_terminal_task_is_protected(self) -> None:
        # dispatch task 把 child_session_id 放在 input 里，sweep 必须据此保护该会话。
        held = self._add_dispatch_session("sess_held", hours_ago=48)
        idle = self._add_dispatch_session("sess_idle", hours_ago=48)
        self.state.tasks = {
            "t_dispatch": _task(
                "t_dispatch", "sess_parent", TaskStatus.pending,
                child_session_id="sess_held", parent_session_id="sess_other_parent",
            ),
        }
        self.state._cleanup_stale_sessions_locked()

        self.assertIn("sess_held", self.state._session_store._registry)
        self.assertIn(held, self.state.conversation_map)
        self.assertTrue((self.ws / "data" / "conversations" / "sess_held.jsonl").exists())
        self.assertNotIn("sess_idle", self.state._session_store._registry)
        self.assertFalse((self.ws / "data" / "conversations" / "sess_idle.jsonl").exists())

    def test_inbound_dispatch_callback_target_and_ancestors_are_protected(self) -> None:
        self._add_dispatch_session("parent", hours_ago=48)
        self._add_dispatch_session("grandparent", hours_ago=48)
        self.state._session_store._registry["parent"]["parent_session_id"] = "grandparent"
        self.state.tasks = {
            "descendant": _task("descendant", "worker_session",
                                _dispatch_origin={"parent_session_id": "parent"}),
        }
        self.state._cleanup_stale_sessions_locked()
        for sid in ("parent", "grandparent"):
            self.assertIn(sid, self.state._session_store._registry)
            self.assertTrue((self.ws / "data" / "conversations" / f"{sid}.jsonl").exists())
        self.state.tasks["descendant"].status = TaskStatus.completed
        self.state._cleanup_stale_sessions_locked()
        self.assertNotIn("parent", self.state._session_store._registry)
        self.assertNotIn("grandparent", self.state._session_store._registry)

    def test_output_chain_callback_target_is_protected(self) -> None:
        self._add_dispatch_session("chain_source", hours_ago=48)
        task = _task("chain", "chain_session")
        task.output_chain_source_session_id = "chain_source"
        self.state.tasks = {"chain": task}
        self.state._cleanup_stale_sessions_locked()
        self.assertIn("chain_source", self.state._session_store._registry)

    def test_completed_fresh_child_is_retained_by_the_router_path(self) -> None:
        self._add_dispatch_session("sess_done", hours_ago=1)
        task = _task(
            "t_done", "sess_parent", TaskStatus.completed,
            child_session_id="sess_done", context_mode="fresh",
        )
        self.state._cleanup_task_child_session_if_needed = types.MethodType(
            TaskRouterMixin._cleanup_task_child_session_if_needed, self.state,
        )
        self.state._cleanup_task_child_session_if_needed(task)

        self.assertIn("sess_done", self.state._session_store._registry)
        self.assertTrue((self.ws / "data" / "conversations" / "sess_done.jsonl").exists())


# ── 4. scheduler catchup 的 once 去重 ────────────────────────────────────────


class SchedulerCatchupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.state = SimpleNamespace(
            _lock=threading.RLock(),
            hook_registry=SimpleNamespace(fire=lambda *args, **kwargs: None),
            _build_supervisor_hook_ctx=lambda **kwargs: None,
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_once_schedule_fires_only_once_during_a_catchup_tick(self) -> None:
        thread = SchedulerThread(state=self.state, workspace_root=self.ws)
        fired: list[str] = []
        thread._inject_inbound = lambda sid, *args, **kwargs: fired.append(sid)
        thread._remove_schedule = lambda sid: None
        # 断点 5 分钟：旧实现会在同一份 snapshot 上把 "* * * * *" 的 once 打 5 次。
        thread._last_tick_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
        schedules = [{
            "id": "once_job", "cron": "* * * * *", "enabled": True, "once": True,
            "text": "ping", "conversation_key": "discord:9",
        }]
        with patch("supervisor.scheduler.load_schedules", return_value=schedules):
            thread._tick()
        self.assertEqual(fired, ["once_job"], "catchup 快照内 once 只能触发一次")

    def test_repeating_schedule_still_fires_each_catchup_minute(self) -> None:
        thread = SchedulerThread(state=self.state, workspace_root=self.ws)
        fired: list[str] = []
        thread._inject_inbound = lambda sid, *args, **kwargs: fired.append(sid)
        thread._last_tick_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=2)
        schedules = [{
            "id": "every_min", "cron": "* * * * *", "enabled": True,
            "text": "ping", "conversation_key": "discord:9",
        }]
        with patch("supervisor.scheduler.load_schedules", return_value=schedules):
            thread._tick()
        self.assertEqual(len(fired), 2, "非 once 仍应补齐每个错过的分钟")


# ── 5. preempt ACK 严格性 ───────────────────────────────────────────────────


class PreemptAckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.state = SimpleNamespace(
            tasks={},
            eventlog=_EventLog(),
            _lock=threading.RLock(),
        )
        self.state._lock = self.state._lock
        for name in ("preempt_task", "consume_preempt_message", "is_task_preempted", "_task_terminal"):
            setattr(self.state, name, types.MethodType(getattr(TaskStoreMixin, name), self.state))
        self.state.tasks["t"] = _task("t", "sess_a")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_invalid_attachment_count_keeps_the_whole_queue(self) -> None:
        for count in (-1, 100, True, "1", None):
            with self.subTest(count=count):
                self.state.tasks["t"].preempt_message = ""
                self.state.tasks["t"].preempt_attachments = []
                self.state.preempt_task("t", "text", [{"path": "file"}])
                before = self.state.is_task_preempted("t")
                self.state.consume_preempt_message("t", "text", count)
                self.assertEqual(self.state.is_task_preempted("t"), before)

    def test_only_the_observed_prefix_is_consumed(self) -> None:
        self.state.preempt_task("t", "first", [{"path": "one"}])
        first = self.state.is_task_preempted("t")
        self.state.preempt_task("t", "second", [{"path": "two"}])
        consumed = self.state.consume_preempt_message("t", first["message"], 1)

        self.assertEqual(consumed["message"], "first")
        self.assertEqual(consumed["attachments"], [{"path": "one"}])
        self.assertEqual(
            self.state.is_task_preempted("t"),
            {"preempted": True, "message": "second", "attachments": [{"path": "two"}]},
        )

    def test_mismatched_observed_text_keeps_the_queue(self) -> None:
        self.state.preempt_task("t", "abcdef", [{"path": "file"}])
        before = self.state.is_task_preempted("t")
        self.state.consume_preempt_message("t", "abc", 1)
        self.assertEqual(self.state.is_task_preempted("t"), before)


# ── 5b. reset 保护调用任务的 child/dispatch 历史 ─────────────────────────────


class ResetProtectionTests(unittest.TestCase):
    _BOUND = (
        "reset_conversation",
        "_cleanup_branch_locked",
        "_cancel_task_for_reset_locked",
        "_task_terminal",
        "_purge_context_usage_for_session",
        "_remove_child_mapping_for_session_locked",
        "_delete_conversation_session_locked",
        "_ensure_entry_branch_indexes_locked",
        "_entry_branch_ids_for_parent_locked",
        "_is_entry_branch_session_locked",
        "_event_task_snapshot",
        "_event_payload_for_task",
        "_cleanup_tool_result_artifacts_for_task",
    )

    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.state = SimpleNamespace(
            workspace_root=self.ws,
            sessions={},
            session_generations={},
            tasks={},
            conversation_map={},
            child_session_map={},
            parent_children={},
            parent_entry_branches={},
            entry_branch_parents={},
            _cancelled_sessions=set(),
            _session_context_usage={},
            _session_store=_SessionStore(),
            eventlog=_EventLog(),
            approvals={},
            _async_tools={},
            lock=threading.RLock(),
        )
        self.state._lock = self.state.lock
        for name in self._BOUND:
            setattr(self.state, name, types.MethodType(getattr(SupervisorState, name), self.state))

    def tearDown(self) -> None:
        self._td.cleanup()

    def _conv(self, sid: str) -> Path:
        path = self.ws / "data" / "conversations" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type": "message", "role": "assistant", "content": "ok"}\n', encoding="utf-8")
        return path

    def test_caller_dispatch_session_history_survives_reset(self) -> None:
        main = "sess_main"
        self.state.conversation_map["discord:1"] = main
        self.state.sessions[main] = SimpleNamespace(session_id=main)
        self.state._session_store._registry[main] = {
            "session_id": main, "conversation_key": "discord:1", "reset": False,
        }
        self._conv(main)

        # 调用任务跑在 agent:* dispatch 子会话上（clear_context 自身的运行会话）。
        dispatch_sid = "sess_dispatch"
        dispatch_key = "agent:_worker:discord:1"
        self.state.conversation_map[dispatch_key] = dispatch_sid
        self.state.sessions[dispatch_sid] = SimpleNamespace(session_id=dispatch_sid)
        self.state._session_store._registry[dispatch_sid] = {
            "session_id": dispatch_sid, "conversation_key": dispatch_key,
            "parent_session_id": main, "context_mode": "fork", "reset": False,
        }
        self._conv(dispatch_sid)

        self.state.tasks = {
            "t_caller": _task("t_caller", dispatch_sid, TaskStatus.running),
            "t_other": _task("t_other", main, TaskStatus.pending),
        }

        result = self.state.reset_conversation(
            conversation_key="discord:1",
            exclude_session_id=dispatch_sid,
            exclude_task_id="t_caller",
        )

        self.assertTrue(result["ok"])
        # 主会话历史照常清掉
        self.assertFalse((self.ws / "data" / "conversations" / f"{main}.jsonl").exists())
        # 调用任务的运行会话及其 dispatch 历史必须保留
        self.assertIn(dispatch_sid, self.state.sessions)
        self.assertIn(dispatch_sid, self.state._session_store._registry)
        self.assertTrue((self.ws / "data" / "conversations" / f"{dispatch_sid}.jsonl").exists())
        # 调用任务不被取消
        self.assertEqual(self.state.tasks["t_caller"].status, TaskStatus.running)
        self.assertFalse(self.state.tasks["t_caller"].cancel_requested)


# ── 6. compaction 关卡：写盘成功后才推进分支游标 ───────────────────────────


def _stored(
    mid: str, role: str, content: str, *, task: str,
    mtype: MessageType | None = None,
) -> Message:
    return Message(
        id=mid, role=role, content=content,
        message_type=mtype or MessageType.ASSISTANT,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_task_id=task,
    )


class CompactCursorOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _temporary_directory()
        self.ws = Path(self._td.name)
        self.store = ConversationStore(self.ws / "data" / "conversations")
        self.state = SimpleNamespace(
            workspace_root=self.ws,
            tasks={},
            eventlog=_EventLog(),
            _session_context_usage={},
            _lock=threading.RLock(),
        )
        for name in ("_apply_compact_via_conv_store_locked", "_clone_compact_summary_message"):
            setattr(self.state, name, types.MethodType(getattr(TaskRouterMixin, name), self.state))
        self.state._sync_compact_to_branches = lambda *args, **kwargs: None
        self.state._route_session_id_for_session_locked = lambda sid: sid
        self.parent = "sess_parent"
        self.branch = "branch_1"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed(self) -> Task:
        base = [
            _stored("p-u", "user", "old question", task="old", mtype=MessageType.USER_INPUT),
            _stored("p-a", "assistant", "old answer", task="old"),
        ]
        self.store.append_batch(self.parent, base)
        tail = [
            _stored("b-u1", "user", "first", task="task", mtype=MessageType.USER_INPUT),
            _stored("b-a1", "assistant", "answer", task="task"),
            _stored("b-u2", "user", "next", task="task", mtype=MessageType.USER_INPUT),
            _stored("b-a2", "assistant", "next answer", task="task"),
        ]
        self.store.append_batch(self.branch, base + tail)
        task = _task(
            "task", self.branch, TaskStatus.running,
            parent_session_id=self.parent, branch_session_id=self.branch,
            base_count=len(base), base_last_id=base[-1].id,
        )
        self.state.tasks[task.task_id] = task
        return task

    def test_write_failure_does_not_advance_the_branch_cursor(self) -> None:
        task = self._seed()
        with patch.object(ConversationStore, "replace_all", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.state._apply_compact_via_conv_store_locked(
                    self.parent, "summary", keep_recent=1, threshold_tokens=0,
                    compress_count=1, has_active_branch=True, caller_branch_task=task,
                )
        self.assertEqual(task.input["base_count"], 2, "写盘失败不得推进分支游标")
        self.assertNotIn("orig_base_last_id", task.input)

    def test_successful_write_advances_the_branch_cursor(self) -> None:
        task = self._seed()
        result = self.state._apply_compact_via_conv_store_locked(
            self.parent, "summary", keep_recent=1, threshold_tokens=0,
            compress_count=1, has_active_branch=True, caller_branch_task=task,
        )
        self.assertLess(result["after"], result["before"])
        self.assertEqual(task.input["base_count"], 4, "已并入父会话的分支尾段必须推进游标")
        self.assertEqual(task.input["base_last_id"], "b-a1")
        persisted = self.store.load(self.parent)
        self.assertEqual(persisted[0].message_type, MessageType.SUMMARY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
