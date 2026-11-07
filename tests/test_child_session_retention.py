"""Completed child session 保留 + 后台 TTL 清理自检（纯临时目录，不启动 supervisor）。

Why: Web 子会话视图用回调消息里的 child_session_id 定位历史；若 task 终态立即
_expire_child_session，完成的子会话在回调到达前就被销毁，前端只能显示空态死链。
How: 用 SimpleNamespace 绑定真实方法（不初始化 SupervisorState，避免拉起调度器/
hooks），在临时 workspace 里演练「完成保留」与「24h sweep」两条路径。
Purpose: 证明 fresh/fork 完成保留、accumulate 永久保留、过期记录与 JSONL/映射一起清、
仍被活跃 task 持有的子会话/dispatch/branch 不被误删、新 session 不被删。

Run: python3.11 -B -m unittest discover -s tests -p 'test_child_session_retention.py' -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import types
import unittest
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import TaskStatus  # noqa: E402


# 只绑定实际用到的方法，保持对象是 SimpleNamespace（不跑 SupervisorState.__init__）。
_BOUND_METHODS = (
    "_cleanup_stale_sessions_locked",
    "_cleanup_task_child_session_if_needed",
    "_task_referenced_session_ids_locked",
    "_task_terminal",
    "_purge_context_usage_for_session",
    "_remove_child_mapping_for_session_locked",
    "_delete_conversation_session_locked",
)


class _RegistryStore:
    """sessions.json 的最小替身：只持有 registry 与 flush 计数。"""

    def __init__(self) -> None:
        self._registry: dict[str, dict] = {}
        self.flushes = 0

    def _flush(self) -> None:
        self.flushes += 1

    def remove_session(self, session_id: str) -> None:
        if self._registry.pop(session_id, None) is not None:
            self.flushes += 1


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _task(*, status: TaskStatus, session_id: str = "sess_parent", task_id: str = "task_1", **input_data):
    return SimpleNamespace(
        task_id=task_id,
        status=status,
        session_id=session_id,
        input=dict(input_data),
        result={},
        node_id="_worker",
    )


def _make_state(tmp: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        workspace_root=tmp,
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
        _session_store=_RegistryStore(),
    )
    for name in _BOUND_METHODS:
        setattr(state, name, types.MethodType(getattr(SupervisorState, name), state))
    return state


def _write_jsonl(ws: Path, kind: str, sid: str) -> Path:
    path = ws / "data" / kind / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "message", "role": "assistant", "content": "ok"}\n', encoding="utf-8")
    return path


def _conv_file(ws: Path, sid: str) -> Path:
    return ws / "data" / "conversations" / f"{sid}.jsonl"


def _tr_file(ws: Path, sid: str) -> Path:
    return ws / "data" / "transcripts" / f"{sid}.jsonl"


def _add_child(
    state: SimpleNamespace,
    *,
    child_sid: str,
    parent_sid: str = "sess_parent",
    context_mode: str = "fresh",
    hours_ago: float = 0.0,
    node_id: str = "_worker",
) -> str:
    """按 on_child_session_created 的真实字段写一条 is_child registry 条目。"""
    node_id = f"{node_id}_{child_sid}" if child_sid else node_id
    context_key = f"ctx:{node_id}"
    state._session_store._registry[child_sid] = {
        "session_id": child_sid,
        "channel": "internal",
        "conversation_key": "web:retention",
        "created_at": _iso(hours_ago),
        "reset": False,
        "is_child": True,
        "parent_session_id": parent_sid,
        "node_id": node_id,
        "context_key": context_key,
        "context_mode": context_mode,
        "last_active_at": _iso(hours_ago),
    }
    state.sessions[child_sid] = SimpleNamespace(session_id=child_sid)
    state.session_generations[child_sid] = 1
    state.child_session_map[(parent_sid, node_id, context_key)] = child_sid
    state.parent_children.setdefault(parent_sid, set()).add(child_sid)
    _write_jsonl(state.workspace_root, "conversations", child_sid)
    _write_jsonl(state.workspace_root, "transcripts", child_sid)
    return child_sid


def _add_inbound_dispatch_session(
    state: SimpleNamespace,
    *,
    node_id: str = "_worker",
    parent_sid: str = "sess_parent",
    context_mode: str = "fresh",
    hours_ago: float = 0.0,
    is_child: bool = True,
    parent_in_registry: bool = True,
    unique_key: bool = True,
) -> tuple[str, str]:
    """镜像 /v1/inbound 异步 dispatch 会话的真实形状（而不是手搋 is_child 行）。

    生产链路：pseudo_handlers 以 conversation_key=agent:{node}:{parent_conv}[:{uuid}]
    POST /v1/inbound → supervisor get_or_create_session 建会话并写 conversation_map
    → api.py 就地补 is_child / parent_session_id / node_id / context_mode。
    早于该补丁的老数据只带 parent_session_id（无 is_child），还有仅靠 agent: 前缀
    识别的遗留行，所以这里 is_child / parent_in_registry / context_mode 都可关。
    """
    conv_key = f"agent:{node_id}:web:retention" + (f":{uuid.uuid4().hex}" if unique_key else "")
    sid = f"inbound_{uuid.uuid4().hex[:10]}"
    entry: dict[str, object] = {
        "session_id": sid,
        "channel": "web",
        "conversation_key": conv_key,
        "created_at": _iso(hours_ago),
        "updated_at": _iso(hours_ago),
        "reset": False,
        "entry_node_id": node_id,
        "node_id": node_id,
        "context_mode": context_mode,
    }
    if parent_in_registry:
        entry["parent_session_id"] = parent_sid
    if is_child:
        entry["is_child"] = True
    state._session_store._registry[sid] = entry
    # get_or_create_session 写的 conversation_map（agent:* → sid）
    state.conversation_map[conv_key] = sid
    state.sessions[sid] = SimpleNamespace(session_id=sid)
    state.session_generations[sid] = 1
    if parent_in_registry:
        state.child_session_map[(parent_sid, node_id, "")] = sid
        state.parent_children.setdefault(parent_sid, set()).add(sid)
    _write_jsonl(state.workspace_root, "conversations", sid)
    _write_jsonl(state.workspace_root, "transcripts", sid)
    return sid, conv_key


def _add_branch(state: SimpleNamespace, *, branch_sid: str, parent_sid: str = "sess_parent") -> str:
    state._session_store._registry[branch_sid] = {        "session_id": branch_sid,
        "channel": "web",
        "conversation_key": "web:retention",
        "created_at": _iso(30),
        "reset": False,
    }
    state.sessions[branch_sid] = SimpleNamespace(session_id=branch_sid)
    state.session_generations[branch_sid] = 1
    state.parent_entry_branches.setdefault(parent_sid, set()).add(branch_sid)
    state.entry_branch_parents[branch_sid] = parent_sid
    _write_jsonl(state.workspace_root, "conversations", branch_sid)
    _write_jsonl(state.workspace_root, "transcripts", branch_sid)
    return branch_sid


class ChildSessionRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)

    def tearDown(self) -> None:
        self._td.cleanup()

    # ---- 完成路径：保留 ------------------------------------------------ #

    def test_completed_fresh_and_fork_child_sessions_are_retained(self) -> None:
        for mode in ("fresh", "fork"):
            child = _add_child(self.state, child_sid=f"child_{mode}", context_mode=mode)
            task = _task(
                status=TaskStatus.completed, task_id=f"t_{mode}",
                child_session_id=child, context_mode=mode,
            )
            self.state._cleanup_task_child_session_if_needed(task)
            self.assertIn(child, self.state._session_store._registry, f"{mode} child registry must survive completion")
            self.assertIn(child, self.state.sessions, f"{mode} child session must survive completion")
            self.assertTrue(_conv_file(self.ws, child).exists(), f"{mode} conversation JSONL must survive completion")
            self.assertTrue(_tr_file(self.ws, child).exists(), f"{mode} transcript must survive completion")
            self.assertIn(child, self.state.child_session_map.values(), f"{mode} child mapping must survive completion")

    def test_completed_accumulate_child_session_is_retained(self) -> None:
        child = _add_child(self.state, child_sid="child_accumulate", context_mode="accumulate")
        task = _task(status=TaskStatus.completed, child_session_id=child, context_mode="accumulate")
        self.state._cleanup_task_child_session_if_needed(task)
        self.assertIn(child, self.state._session_store._registry)
        self.assertTrue(_conv_file(self.ws, child).exists())

    def test_suspended_task_never_drops_child_session(self) -> None:
        child = _add_child(self.state, child_sid="child_suspended", context_mode="fresh")
        task = _task(status=TaskStatus.suspended, child_session_id=child, context_mode="fresh")
        self.state._cleanup_task_child_session_if_needed(task)
        self.assertIn(child, self.state._session_store._registry)
        self.assertTrue(_conv_file(self.ws, child).exists())

    # ---- 24h TTL sweep：记录 + JSONL + 映射一起清 ---------------------- #

    def test_sweep_removes_stale_child_with_jsonl_and_mapping(self) -> None:
        stale = _add_child(self.state, child_sid="child_stale", context_mode="fresh", hours_ago=25)
        self.state.child_session_map[("sess_parent", "other_node", "ctx:other_node")] = stale
        mapping_key = ("sess_parent", "other_node", "ctx:other_node")
        self.assertIn(mapping_key, self.state.child_session_map)

        self.state._cleanup_stale_sessions_locked()

        self.assertNotIn(stale, self.state._session_store._registry, "stale child registry row must be gone")
        self.assertNotIn(stale, self.state.sessions)
        self.assertNotIn(stale, self.state.session_generations)
        self.assertFalse(_conv_file(self.ws, stale).exists(), "stale conversation JSONL must be deleted")
        self.assertFalse(_tr_file(self.ws, stale).exists(), "stale transcript JSONL must be deleted")
        self.assertNotIn(stale, self.state.child_session_map.values(), "child mapping must be dropped")
        self.assertNotIn(stale, self.state.parent_children.get("sess_parent", set()))
        self.assertGreaterEqual(self.state._session_store.flushes, 1, "sweep must flush sessions.json")

    def test_sweep_keeps_recent_child_and_accumulate(self) -> None:
        recent = _add_child(self.state, child_sid="child_recent", context_mode="fresh", hours_ago=2)
        accumulate = _add_child(self.state, child_sid="child_accumulate_old", context_mode="accumulate", hours_ago=72)

        self.state._cleanup_stale_sessions_locked()

        for sid in (recent, accumulate):
            self.assertIn(sid, self.state._session_store._registry, f"{sid} must be retained")
            self.assertTrue(_conv_file(self.ws, sid).exists(), f"{sid} JSONL must be retained")

    def test_sweep_cleans_legacy_child_without_context_mode(self) -> None:
        # 生产既有特例：无 context_mode 的历史系统节点子会话不等 TTL，直接清理。
        legacy = _add_child(self.state, child_sid="child_legacy", context_mode="", hours_ago=0)

        self.state._cleanup_stale_sessions_locked()

        self.assertNotIn(legacy, self.state._session_store._registry)
        self.assertFalse(_conv_file(self.ws, legacy).exists())

    # ---- 活跃关系守卫 -------------------------------------------------- #

    def test_sweep_keeps_sessions_held_by_active_tasks(self) -> None:
        running_child = _add_child(self.state, child_sid="child_running", context_mode="fresh", hours_ago=48)
        dispatch_child = _add_child(self.state, child_sid="child_dispatch", context_mode="fresh", hours_ago=48)
        dispatch_parent = _add_child(self.state, child_sid="child_dispatch_parent", context_mode="fork", hours_ago=48)
        parent_rel = _add_child(self.state, child_sid="child_parent_rel", context_mode="fresh", hours_ago=48)
        running_branch = _add_branch(self.state, branch_sid="branch_running")
        branch_rel = _add_branch(self.state, branch_sid="branch_rel")
        idle_branch = _add_branch(self.state, branch_sid="branch_idle")

        self.state.tasks = {
            "t_child": _task(status=TaskStatus.running, session_id=running_child, task_id="t_child"),
            "t_dispatch": _task(
                status=TaskStatus.running, session_id="sess_other", task_id="t_dispatch",
                child_session_id=dispatch_child, parent_session_id=dispatch_parent,
            ),
            "t_parent_rel": _task(
                status=TaskStatus.pending, session_id="sess_other2", task_id="t_parent_rel",
                parent_session_id=parent_rel,
            ),
            "t_branch": _task(status=TaskStatus.running, session_id=running_branch, task_id="t_branch"),
            "t_branch_rel": _task(
                status=TaskStatus.running, session_id="sess_other3", task_id="t_branch_rel",
                branch_session_id=branch_rel,
            ),
            # 终态 task 不构成保护。
            "t_done": _task(
                status=TaskStatus.completed, session_id="sess_done", task_id="t_done",
                child_session_id="child_already_done",
            ),
        }

        self.state._cleanup_stale_sessions_locked()

        for sid in (running_child, dispatch_child, dispatch_parent, parent_rel, running_branch, branch_rel):
            self.assertIn(sid, self.state._session_store._registry, f"{sid} is held by an active task and must be kept")
            self.assertTrue(_conv_file(self.ws, sid).exists(), f"{sid} JSONL must be kept")
        self.assertNotIn(idle_branch, self.state._session_store._registry, "idle orphan branch must still be swept")
        self.assertFalse(_conv_file(self.ws, idle_branch).exists())

    # ---- 真实 inbound dispatch 形状（agent:* + conversation_map） ------- #

    def test_inbound_dispatch_session_kept_until_ttl_then_released(self) -> None:
        fresh_sid, fresh_ck = _add_inbound_dispatch_session(self.state, context_mode="fresh", hours_ago=2)
        stale_sid, stale_ck = _add_inbound_dispatch_session(
            self.state, node_id="_reviewer", context_mode="fork", hours_ago=25,
        )

        self.state._cleanup_stale_sessions_locked()

        # TTL 内继续保留：回调/Web 子会话视图靠它回看历史。
        self.assertIn(fresh_sid, self.state._session_store._registry)
        self.assertEqual(self.state.conversation_map.get(fresh_ck), fresh_sid)
        self.assertTrue(_conv_file(self.ws, fresh_sid).exists())

        # 过期后 registry / JSONL / conversation_map / child 映射一起释放。
        self.assertNotIn(stale_sid, self.state._session_store._registry)
        self.assertNotIn(stale_sid, self.state.sessions)
        self.assertFalse(_conv_file(self.ws, stale_sid).exists())
        self.assertFalse(_tr_file(self.ws, stale_sid).exists())
        self.assertNotIn(stale_ck, self.state.conversation_map, "agent:* conversation_map 映射必须跟着清")
        self.assertNotIn(stale_sid, self.state.child_session_map.values())
        self.assertNotIn(stale_sid, self.state.parent_children.get("sess_parent", set()))

    def test_inbound_dispatch_accumulate_session_never_expires(self) -> None:
        sid, ck = _add_inbound_dispatch_session(
            self.state, context_mode="accumulate", hours_ago=72, unique_key=False,
        )

        self.state._cleanup_stale_sessions_locked()

        self.assertIn(sid, self.state._session_store._registry)
        self.assertEqual(self.state.conversation_map.get(ck), sid)
        self.assertTrue(_conv_file(self.ws, sid).exists())

    def test_legacy_inbound_dispatch_without_is_child_gets_ttl(self) -> None:
        # 早于 api.py is_child 补丁的老数据：只有 parent_session_id，没有 is_child。
        legacy_new, _ = _add_inbound_dispatch_session(
            self.state, node_id="_legacy_new", context_mode="fresh", hours_ago=1, is_child=False,
        )
        legacy_old, legacy_old_ck = _add_inbound_dispatch_session(
            self.state, node_id="_legacy_old", context_mode="fresh", hours_ago=30, is_child=False,
        )
        # 无 context_mode 的历史遗留：按生产既有特例直接清。
        legacy_mode, legacy_mode_ck = _add_inbound_dispatch_session(
            self.state, node_id="_legacy_mode", context_mode="", hours_ago=0, is_child=False,
        )

        self.state._cleanup_stale_sessions_locked()

        self.assertIn(legacy_new, self.state._session_store._registry, "TTL 内的老形状 dispatch 会话应保留")
        for sid, ck in ((legacy_old, legacy_old_ck), (legacy_mode, legacy_mode_ck)):
            self.assertNotIn(sid, self.state._session_store._registry)
            self.assertNotIn(ck, self.state.conversation_map)
            self.assertFalse(_conv_file(self.ws, sid).exists())

    def test_agent_keyed_session_without_parent_still_ttl_cleaned(self) -> None:
        kept, kept_ck = _add_inbound_dispatch_session(
            self.state, node_id="_prefix_new", context_mode="fresh", hours_ago=1,
            is_child=False, parent_in_registry=False,
        )
        gone, gone_ck = _add_inbound_dispatch_session(
            self.state, node_id="_prefix_old", context_mode="fresh", hours_ago=30,
            is_child=False, parent_in_registry=False,
        )

        self.state._cleanup_stale_sessions_locked()

        self.assertIn(kept, self.state._session_store._registry)
        self.assertEqual(self.state.conversation_map.get(kept_ck), kept)
        self.assertNotIn(gone, self.state._session_store._registry, "agent: 前缀会话必须被 TTL 覆盖")
        self.assertNotIn(gone_ck, self.state.conversation_map)

    def test_running_inbound_dispatch_session_not_swept(self) -> None:
        # pseudo_handlers 派发时把 parent_session_id 写进 task.input；
        # 派发会话自身也可作为 task.session_id 运行。
        via_parent_rel, _ = _add_inbound_dispatch_session(
            self.state, node_id="_rel", context_mode="fresh", hours_ago=48, unique_key=False,
        )
        via_session, _ = _add_inbound_dispatch_session(
            self.state, node_id="_run", context_mode="fresh", hours_ago=48,
        )
        idle, _ = _add_inbound_dispatch_session(
            self.state, node_id="_idle", context_mode="fresh", hours_ago=48,
        )
        self.state.tasks = {
            "t_rel": _task(
                status=TaskStatus.running, session_id="sess_parent", task_id="t_rel",
                parent_session_id=via_parent_rel,
            ),
            "t_run": _task(status=TaskStatus.running, session_id=via_session, task_id="t_run"),
        }

        self.state._cleanup_stale_sessions_locked()

        for sid in (via_parent_rel, via_session):
            self.assertIn(sid, self.state._session_store._registry, f"{sid} 仍被派发任务使用，不得删")
            self.assertTrue(_conv_file(self.ws, sid).exists())
        self.assertNotIn(idle, self.state._session_store._registry)

    # ---- 结构性守卫：即时删除路径已彻底移除 ---------------------------- #

    def test_router_has_no_immediate_child_session_deletion(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        router_src = (repo_root / "supervisor" / "task_router.py").read_text(encoding="utf-8")
        self.assertNotIn("_cleanup_dispatch_session_locked", router_src,
                         "immediate fresh/fork dispatch cleanup must be gone (TTL sweep owns deletion)")
        self.assertNotIn("self._expire_child_session(", router_src,
                         "router must not destroy completed child sessions immediately")
        self.assertIn("fresh/fork dispatch session 不再完成即删", router_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
