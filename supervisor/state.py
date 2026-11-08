"""Supervisor 核心状态 —— 组合类，继承三个 mixin。"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ._helpers import SessionInfo, _now
from .eventlog import EventLog, SYSTEM_SESSION_ID
from engine.builtin.loader import auto_discover_and_register
from engine.hooks import HookRegistry
from .policy import PolicyEngine
from .session_approval import approval_action, stored_approval_rules, validate_approval_rules
from .session import SessionMixin
from .session_store import SessionStore
from .task_router import TaskRouterMixin
from .task_store import TaskStoreMixin
from .types import (
    AdminStateOut,
    Approval,
    ApprovalStatus,
    OpRequestOut,
    SafetyLevel,
    Task,
    TaskKind,
    TaskStatus,
)


class SupervisorState(SessionMixin, TaskStoreMixin, TaskRouterMixin):
    """Supervisor 核心状态。管理会话、inbound 队列、task 队列与审批。"""

    def __init__(self, *, workspace_root: Path, eventlog: EventLog, policy: PolicyEngine) -> None:
        self.workspace_root = workspace_root
        self.eventlog = eventlog
        self.policy = policy
        self.started_at = _now()

        self._lock = threading.RLock()

        self.sessions: dict[str, SessionInfo] = {}
        self.conversation_map: dict[str, str] = {}
        self.session_generations: dict[str, int] = {}

        self.approvals: dict[str, Approval] = {}

        self.tasks: dict[str, Task] = {}
        self._task_order: list[str] = []

        self._engine_last_seen_at: datetime | None = None
        self._engine_last_worker_id: str | None = None

        self._inbound_order: list[int] = []
        self._inbound_events: dict[int, dict[str, Any]] = {}
        self._inbound_processed: set[int] = set()
        self._inbound_routed: dict[int, dict[str, Any]] = {}
        self._inbound_cursor: int = 0

        @dataclass
        class _InboundLease:
            worker_id: str
            expires_at: datetime

        self._InboundLease = _InboundLease
        self._inbound_leases: dict[int, _InboundLease] = {}

        self._cancelled_sessions: set[str] = set()
        self._skip_merge_sessions: set[str] = set()
        self._tools_reload_seq: int = 0
        self.session_entry_overrides: dict[str, str] = {}  # session_id -> node_id (AI switch)
        self.session_last_entry_node: dict[str, str] = {}  # session_id -> last used entry node
        # [2026-06-07] Keyed by (session_id, node_id) tuple so each node's
        # usage is stored independently. get_session_context_usage aggregates
        # by selecting the non-system entry with the highest prompt_tokens.
        self._session_context_usage: dict[tuple[str, str], dict[str, Any]] = {}
        self._engine_generations: dict[str, str] = {}  # worker_id -> generation_id (Direction 2)

        # [AutoC 2026-09-02] 异步工具登记处：engine 在启动/完成/失败时上报，
        # supervisor 持有影子清单。engine 本地 _async_tool_tasks 仍是执行侧唯一
        # 真实来源；此处供管理查询与重启 lost 标记。
        self._async_tools: dict[str, dict[str, Any]] = {}
        # [AutoC 2026-09-02] 待消费的取消意图队列：admin 取消 → 入队 →
        # engine 轮询拉走 → 标 cancel_requested。幂等：同一 async_id 重复
        # 入队去重，lost 后拒绝再入队。
        self._async_tool_pending_cancels: dict[str, list[str]] = {}  # worker_id -> [async_id]

        self._init_hooks_and_sessions(workspace_root)

    def _purge_context_usage_for_session(self, session_id: str) -> None:
        """Remove all context usage entries keyed under the given session_id."""
        keys_to_remove = [k for k in self._session_context_usage if k[0] == session_id]
        for k in keys_to_remove:
            del self._session_context_usage[k]

    def _init_hooks_and_sessions(self, workspace_root: Path) -> None:
        """Initialise hook registry, plugins, and session persistence (called from __init__)."""
        self._pending_restart_notify: str | None = None  # session_id to notify after engine registers

        # Why: supervisor feature side effects should be registered handlers rather
        # than hard-coded router or scheduler branches. How: create one unified
        # HookRegistry per SupervisorState and auto-discover built-ins before
        # session-store reconciliation. Purpose: runtime hook notifications can run
        # without storing mutable handler state on SupervisorState or using a
        # separate supervisor registry.
        self.hook_registry = HookRegistry()
        # Why: built-in supervisor handlers now live under engine.builtin, where
        # they cannot import SupervisorState or other supervisor internals. How:
        # register every PLUGIN_META declaration through the auto loader and retain
        # the returned instances for diagnostics and existing tests. Purpose:
        # preserve handler-owned timer/cursor state while removing cyclic imports
        # and hard-coded registration.
        from engine.context import EngineContext

        # Why: plugins declare HTTP routes on a face only the supervisor can
        # satisfy (the FastAPI app lives in this process); engine-side loads
        # see contributions.get("routes") -> None and skip. How: mount one
        # PluginRoutesFace on the supervisor context before the loaders run
        # and keep it on the state so create_app can attach the app later.
        from engine.faces.routes import PluginRoutesFace

        self.routes_face = PluginRoutesFace()
        _supervisor_ctx = EngineContext(hooks=self.hook_registry)
        _supervisor_ctx.contributions.mount("routes", self.routes_face)
        # [AutoC 2026-08-30] nodes face: plugins declare nodes visible to the
        # supervisor node list and switch-node discovery in this process.
        from engine.faces.nodes import PluginNodesFace
        _supervisor_ctx.contributions.mount("nodes", PluginNodesFace())
        _builtin_handlers = auto_discover_and_register(self.hook_registry, context=_supervisor_ctx)
        # Why: the Clonoth runtime may temporarily run with a reduced built-in
        # handler set while features are being rolled out. How: keep optional
        # references with dict.get instead of indexing. Purpose: synchronizing
        # this file does not remove the existing deployment compatibility guard.
        self._memory_extract_handler = _builtin_handlers.get("memory_extract")
        self._dream_handler = _builtin_handlers.get("dream")

        # External plugins: same mechanism as engine, scan plugins/ directory
        from engine.hooks.loader import load_external_plugins
        _plugins_dir = workspace_root / "plugins"
        _ext_count = load_external_plugins(
            self.hook_registry, _plugins_dir, context=_supervisor_ctx, process="supervisor"
        )
        if _ext_count:
            logger.info("Loaded %d external plugin(s) for supervisor hooks", _ext_count)
        # [plugin-admin 2026-08-23] Runtime plugin management state. Why: the
        # admin plugin's routes reach SupervisorState through app.state and need
        # the same loader inputs the startup scan used. How: keep the resolved
        # plugins dir and the supervisor context for later single-entry loads.
        # Purpose: load/unload/enable/disable without a process restart.
        self.plugins_dir = _plugins_dir
        self._supervisor_ctx = _supervisor_ctx

        # ---- Child Session 隔离（Phase A）：映射表 ----
        # (parent_session_id, node_id, context_key) → child_session_id
        self.child_session_map: dict[tuple[str, str, str], str] = {}
        # 反向索引：parent_session_id → set of child_session_ids，用于 clear 时快速查找
        self.parent_children: dict[str, set[str]] = {}
        # [Fork/Merge 2026-05-12] 记录入口分支与主 session 的关系。
        # 原因：入口 task 现在运行在 branch session 上，主 session 的取消、重置和
        # running_tasks 查询仍需要找到这些分支。做法：维护 parent→branches 与
        # branch→parent 两张轻量内存索引，持久清理仍复用 parent_children/session_store。
        # 目的：允许同一主 session 下多个入口分支并发执行，同时保持旧 session 映射不变。
        self.parent_entry_branches: dict[str, set[str]] = {}
        self.entry_branch_parents: dict[str, str] = {}

        # ---- 方案 A: 独立 session 持久化 ----
        self._session_store = SessionStore(workspace_root / "data" / "sessions.json")
        loaded_sessions, loaded_conv_map, loaded_child_map, loaded_parent_children = self._session_store.load()
        if loaded_sessions:
            self.sessions.update(loaded_sessions)
            self.conversation_map.update(loaded_conv_map)
        # Child Session 隔离（Phase A）：从 sessions.json 恢复 child session 映射
        if loaded_child_map:
            self.child_session_map.update(loaded_child_map)
        if loaded_parent_children:
            for psid, children in loaded_parent_children.items():
                self.parent_children.setdefault(psid, set()).update(children)
        # [Fork/Merge 2026-05-17] Why: sessions.json stores entry branches as
        # child sessions with node_id="__entry_branch__", but the fast branch→parent
        # indexes are in-memory only. How: rebuild those indexes from the loaded
        # child registry before startup reconciliation. Purpose: public APIs and
        # async dispatch can resolve branch sessions correctly after supervisor restart.
        for psid, children in self.parent_children.items():
            for child_sid in children:
                raw = self._session_store._registry.get(child_sid)
                if isinstance(raw, dict) and raw.get("node_id") == "__entry_branch__":
                    self.entry_branch_parents[child_sid] = psid
                    self.parent_entry_branches.setdefault(psid, set()).add(child_sid)

        self._reconcile_after_restart()

    # ── [plugin-admin 2026-08-23] Runtime external-plugin management ───────

    # The manager plugin itself is exempt from unload/disable: removing it
    # would take the routes serving the management UI offline mid-operation.
    _PROTECTED_PLUGIN_ENTRIES = {"plugin_manager"}

    def _emit_plugin_event(self, type_: str, payload: dict[str, Any]) -> None:
        """Append one plugin lifecycle event to the global event stream."""
        # Why: the web pluginsStore must learn about load/unload without polling.
        # How: EventLog global subscribers (/v1/ws) receive every row regardless
        # of session_id; use SYSTEM_SESSION_ID and component "supervisor". All
        # management paths (API, future RPC, CLI) emit from here so event shape
        # stays uniform. Purpose: one source of plugin lifecycle notifications.
        try:
            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="supervisor",
                type_=type_,
                payload={**payload, "component": "supervisor"},
            )
        except Exception as exc:
            logger.warning("plugin event %s failed: %s", type_, exc)

    def _plugin_entry_path(self, entry_name: str, *, allow_disabled: bool = True) -> Path:
        """Resolve one plugin entry inside plugins_dir; raises ValueError on escape."""
        clean = str(entry_name or "").strip()
        if not clean or "/" in clean or "\\" in clean or ".." in Path(clean).parts:
            raise ValueError(f"invalid plugin entry name: {entry_name!r}")
        base = self.plugins_dir.resolve()
        cand = (base / clean).resolve()
        if base not in cand.parents:
            raise ValueError(f"plugin entry escapes plugins directory: {entry_name!r}")
        if not cand.exists() and allow_disabled:
            disabled = base / (clean + ".disabled")
            if disabled.exists():
                return disabled
        return cand

    def _entry_meta_name(self, entry: Path) -> str:
        """Best-effort meta name for one entry without registering anything."""
        from engine.hooks.loader import plugin_module_key

        module = sys.modules.get(plugin_module_key(entry.name))
        raw = getattr(module, "PLUGIN_META", None)
        if isinstance(raw, dict) and raw.get("name"):
            return str(raw["name"])
        return entry.stem

    def plugin_admin_list(self) -> list[dict[str, Any]]:
        """Merge filesystem entries with loaded registry state for the admin UI."""
        from engine.hooks.loader import _is_enabled_python_plugin, _is_plugin_package, get_load_error

        loaded = {str(m.get("name") or ""): m for m in self.hook_registry.list_plugins()}
        entries: list[dict[str, Any]] = []
        seen_entries: set[str] = set()
        if self.plugins_dir.is_dir():
            for entry in sorted(self.plugins_dir.iterdir()):
                is_pkg = _is_plugin_package(entry)
                is_disabled_pkg = entry.is_dir() and entry.name.endswith(".disabled") and (entry / "__init__.py").is_file()
                is_disabled_py = entry.is_file() and entry.name.endswith(".py.disabled")
                if not (is_pkg or is_disabled_pkg or _is_enabled_python_plugin(entry) or is_disabled_py):
                    continue
                stem = entry.name.removesuffix(".disabled")
                # [plugin-admin 2026-08-23] Why: display and dedup keys must be
                # suffix-free for both shapes — a .py file previously kept its
                # extension in the entry name shown by the admin UI. How: strip
                # .disabled first, then a trailing .py. Purpose: entries read as
                # stable plugin names regardless of file kind.
                if stem.endswith(".py"):
                    stem = stem[:-3]
                seen_entries.add(stem)
                meta_name = self._entry_meta_name(entry)
                meta = loaded.get(meta_name)
                entries.append({
                    "entry": stem,
                    "file_name": entry.name,
                    "kind": "package" if (is_pkg or is_disabled_pkg) else "file",
                    "enabled": not entry.name.endswith(".disabled"),
                    "loaded": meta is not None,
                    "name": meta_name,
                    "version": str(meta.get("version") or "") if meta else "",
                    "description": str(meta.get("description") or "") if meta else "",
                    "hooks": list(meta.get("hooks") or []) if meta else [],
                    "builtin": False,
                    "external": True,
                    # [plugin-admin 2026-08-23] surface the last load failure so
                    # "未加载" stops being a dead end for the operator
                    "error": "" if meta else get_load_error(stem),
                })
        # builtin (engine-side) plugins: display-only, engine process owns them
        for name, meta in sorted(loaded.items()):
            if meta.get("version") == "builtin" and name not in seen_entries:
                entries.append({
                    "entry": str(meta.get("module") or ""),
                    "file_name": "",
                    "kind": "builtin",
                    "enabled": True,
                    "loaded": True,
                    "name": name,
                    "version": "builtin",
                    "description": str(meta.get("description") or ""),
                    "hooks": list(meta.get("hooks") or []),
                    "builtin": True,
                    "external": False,
                })
        return entries

    def _plugin_event_sink(self, entry: str):
        def _sink(event_type: str, payload: dict[str, Any]) -> None:
            self._emit_plugin_event(event_type, payload)

        return _sink

    def _builtin_meta_name(self, entry_name: str) -> str | None:
        """Resolve the registry meta name for one built-in entry, or None."""
        for meta in self.hook_registry.list_plugins():
            if str(meta.get("version") or "") != "builtin":
                continue
            if str(meta.get("module") or "") == entry_name:
                return str(meta.get("name") or entry_name)
        return None

    def plugin_admin_load(self, entry_name: str) -> dict[str, Any]:
        """Load (or reload) one external plugin entry at runtime."""
        from engine.builtin.loader import builtin_entry_path, load_single_builtin
        from engine.hooks.loader import drop_plugin_module, load_single_plugin

        # [plugin-admin 2026-08-28] built-in entries reload through the builtin
        # loader: same register_meta_handler path as the startup scan, disposal
        # ledger attribution intact. Why: verifying a fix inside engine/builtin/
        # otherwise required a process restart.
        if builtin_entry_path(entry_name) is not None:
            meta_name = self._builtin_meta_name(entry_name)
            if meta_name is not None:
                self.hook_registry.unload_plugin(meta_name)
            result = load_single_builtin(
                self.hook_registry, entry_name, context=self._supervisor_ctx,
            )
            self._emit_plugin_event("plugin_loaded", {"plugin": result["name"], "entry": entry_name})
            return {"ok": True, "plugin": result["name"], "entry": entry_name}

        entry = self._plugin_entry_path(entry_name)
        if entry.name.endswith(".disabled"):
            raise ValueError(f"plugin {entry_name!r} is disabled; enable it first")
        meta_name = self._entry_meta_name(entry)
        if meta_name in {str(m.get("name") or "") for m in self.hook_registry.list_plugins()}:
            # already loaded — treat as reload: undo old registrations first
            self.hook_registry.unload_plugin(meta_name)
            drop_plugin_module(entry.stem)
        meta = load_single_plugin(
            self.hook_registry,
            self.plugins_dir,
            entry.stem if entry.is_dir() else entry.name,
            context=self._supervisor_ctx,
            event_sink=self._plugin_event_sink(entry.stem),
        )
        return {"ok": True, "plugin": meta.get("name"), "entry": entry_name}

    def plugin_admin_unload(self, entry_name: str) -> dict[str, Any]:
        """Unload one external plugin entry; registrations are replayed in reverse."""
        from engine.builtin.loader import builtin_entry_path, drop_builtin_module
        from engine.hooks.loader import drop_plugin_module

        if entry_name in self._PROTECTED_PLUGIN_ENTRIES:
            raise ValueError("the plugin manager cannot unload itself")

        # built-in entry: registry owns the meta name; no filesystem entry exists
        if builtin_entry_path(entry_name) is not None:
            meta_name = self._builtin_meta_name(entry_name)
            if meta_name is None:
                raise ValueError(f"built-in plugin {entry_name!r} is not loaded")
            result = self.hook_registry.unload_plugin(meta_name)
            drop_builtin_module(entry_name)
            self._emit_plugin_event("plugin_unloaded", {"plugin": meta_name, "entry": entry_name})
            return {"ok": True, "plugin": meta_name, "entry": entry_name, **result}

        entry = self._plugin_entry_path(entry_name)
        meta_name = self._entry_meta_name(entry)
        known = {str(m.get("name") or ""): m for m in self.hook_registry.list_plugins()}
        if meta_name not in known:
            raise ValueError(f"plugin {entry_name!r} is not loaded")
        result = self.hook_registry.unload_plugin(meta_name)
        drop_plugin_module(entry.name.removesuffix(".disabled"))
        self._emit_plugin_event("plugin_unloaded", {"plugin": meta_name, "entry": entry_name})
        return {"ok": True, **result}

    def plugin_admin_enable(self, entry_name: str) -> dict[str, Any]:
        """Remove the .disabled suffix and load the entry."""
        entry = self._plugin_entry_path(entry_name, allow_disabled=False)
        disabled = entry.with_name(entry.name + ".disabled")
        target = disabled if disabled.exists() else entry
        if not target.exists():
            raise ValueError(f"no plugin entry named {entry_name!r}")
        if target.name.endswith(".disabled"):
            restored = target.with_name(target.name.removesuffix(".disabled"))
            target.rename(restored)
        return self.plugin_admin_load(entry_name)

    def plugin_admin_disable(self, entry_name: str) -> dict[str, Any]:
        """Unload if loaded, then rename the entry with a .disabled suffix."""
        if entry_name in self._PROTECTED_PLUGIN_ENTRIES:
            raise ValueError("the plugin manager cannot disable itself")
        entry = self._plugin_entry_path(entry_name, allow_disabled=False)
        if not entry.exists():
            raise ValueError(f"no plugin entry named {entry_name!r}")
        if entry.name.endswith(".disabled"):
            return {"ok": True, "already": True}
        meta_name = self._entry_meta_name(entry)
        known = {str(m.get("name") or ""): m for m in self.hook_registry.list_plugins()}
        if meta_name in known:
            self.hook_registry.unload_plugin(meta_name)
            self._emit_plugin_event("plugin_unloaded", {"plugin": meta_name, "entry": entry_name})
        from engine.hooks.loader import drop_plugin_module

        drop_plugin_module(entry.name)
        entry.rename(entry.with_name(entry.name + ".disabled"))
        return {"ok": True}

    def _build_supervisor_hook_ctx(self, **extra: Any) -> dict[str, Any]:
        """Build the callback-only context passed to built-in supervisor hooks."""
        # Why: engine.builtin handlers must not import or keep a reference to
        # SupervisorState. How: expose the small set of required supervisor
        # operations as callbacks and plain values. Purpose: make hook handlers
        # cycle-free while preserving the previous behavior at each fire point.
        ctx: dict[str, Any] = {
            "workspace_root": self.workspace_root,
            "session_messages": lambda sid, limit=0: self.session_messages(session_id=sid, limit=limit),
            "create_task": self._create_task_from_hook_locked,
            "acquire_lock": self._lock,
            "format_transcript": self._format_transcript_for_extract_callback,
            "current_session_generation": lambda sid: self._current_session_generation_locked(sid),
            "session_count": lambda: len(self.sessions),
            "task_snapshots": self._task_snapshots_from_hook_locked,
        }
        ctx.update(extra)
        return ctx

    def _task_snapshots_from_hook_locked(self, task_ids: list[str]) -> dict[str, dict[str, str]]:
        """Return safe task status/result snapshots for built-in hook handlers."""
        # [AutoC 2026-05-31] Why: built-in handlers such as Dream need to poll
        # child task completion without importing SupervisorState or receiving
        # mutable Task objects. How: expose only status, result text, and error
        # strings through the hook context. Purpose: keep the capability generic
        # and avoid adding feature-specific branches to the core router.
        result: dict[str, dict[str, str]] = {}
        for tid in task_ids:
            task_id = str(tid or "")
            task = self.tasks.get(task_id)
            if task is None:
                result[task_id] = {"status": "missing", "result_text": "", "error": ""}
                continue
            task_result = task.result or {}
            inner = task_result.get("result", {}) if isinstance(task_result.get("result"), dict) else {}
            result[task_id] = {
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "result_text": str(inner.get("text") or task_result.get("text") or ""),
                "error": str(task_result.get("error") or ""),
            }
        return result

    def _create_task_from_hook_locked(self, **kwargs: Any) -> Task:
        """Create a task for a built-in hook using callback-friendly arguments."""
        # Why: relocated supervisor handlers cannot import TaskKind or call session
        # helpers directly. How: normalize string/enum kind values and optionally
        # create a session from channel plus conversation_key before delegating to
        # _create_task_locked. Purpose: keep handler code callback-only while still
        # using the existing task creation path and event snapshot behavior.
        data = dict(kwargs)
        conversation_key = str(data.pop("conversation_key", "") or "").strip()
        channel = str(data.pop("channel", "") or "").strip()
        if not str(data.get("session_id") or "").strip():
            if not conversation_key:
                raise ValueError("session_id or conversation_key is required")
            if not channel:
                channel = conversation_key.split(":", 1)[0] if ":" in conversation_key else "system"
            data["session_id"] = self.get_or_create_session(channel=channel, conversation_key=conversation_key)

        sid = str(data.get("session_id") or "").strip()
        generation = self._to_positive_int(data.get("session_generation"))
        if generation is None:
            generation = self._current_session_generation_locked(sid) or 1
        data["session_generation"] = generation
        if sid and not self.session_generations.get(sid):
            self.session_generations[sid] = generation

        kind = data.get("kind") or TaskKind.node
        if not isinstance(kind, TaskKind):
            value = getattr(kind, "value", kind)
            try:
                kind = TaskKind(str(value or "node"))
            except Exception:
                kind = TaskKind.node
        data["kind"] = kind

        input_data = data.get("input_data")
        if isinstance(input_data, dict):
            task_context = input_data.get("task_context")
            if isinstance(task_context, dict):
                # Why: hooks that create tasks directly bypass inbound processing,
                # which used to add these identifiers. How: fill missing values in
                # the provided task_context. Purpose: preserve system-task metadata
                # for engine-side checks without requiring supervisor imports.
                task_context.setdefault("session_id", sid)
                task_context.setdefault("session_generation", generation)

        data.setdefault("continuation", {})
        data.setdefault("source_inbound_seq", None)
        data.setdefault("caller_task_id", None)
        return self._create_task_locked(**data)

    @staticmethod
    def _format_transcript_for_extract_callback(messages: list[dict[str, Any]], *, max_chars: int = 12000) -> str:
        """Format conversation messages into transcript text for extraction hooks."""
        # Why: MemoryExtractHandler now receives formatting as an injected callback
        # rather than importing supervisor code. How: keep the previous pure
        # formatter on SupervisorState and pass it through ctx. Purpose: retain the
        # fallback transcript format while removing handler-side supervisor imports.
        parts: list[str] = []
        total = 0
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                ]
                content = "\n".join(texts)
            if not isinstance(content, str):
                content = str(content)
            if len(content) > 2000:
                content = content[:2000] + "...<truncated>"
            line = f"[{role}]\n{content}"
            total += len(line)
            if total > max_chars:
                break
            parts.append(line)
        parts.reverse()
        return "\n\n---\n\n".join(parts)

    def _reconcile_after_restart(self) -> None:
        """启动期状态协调：清理上一进程遗留的运行期状态。

        [AutoC 2026-05-30] Why: 移除 EventLog 启动回放后，需要独立的
        启动清理逻辑替代原有的 cancel_orphaned_tasks 等依赖回放重建的机制。
        How: 从 sessions.json 扫描并清理 orphan branch、过期 child session。
        Purpose: 确保重启后内存状态干净，不留上一进程的运行期残余。
        """
        cleaned = 0
        stale_sids: list[str] = []
        seen_stale: set[str] = set()

        def mark_stale(session_id: str) -> None:
            """记录待清理 session，保持顺序并避免重复。"""
            # [AutoC 2026-05-30] Why: 同一个 session 可能同时满足 reset、branch、
            # 过期 child 等条件。How: 用 set 做去重，用 list 保留可读的处理顺序。
            # Purpose: 清理日志和计数稳定，不重复删除同一条 sessions.json 记录。
            sid = str(session_id or "").strip()
            if sid and sid not in seen_stale:
                seen_stale.add(sid)
                stale_sids.append(sid)

        # 1. 清理 sessions.json 中 reset=true 的条目。
        for sid, entry in list(self._session_store._registry.items()):
            if isinstance(entry, dict) and entry.get("reset"):
                mark_stale(sid)

        # 2. 清理 orphan branch session（重启后上一进程的 branch 不再有活跃任务）。
        for sid in list(self._session_store._registry.keys()):
            if str(sid).startswith("branch_"):
                mark_stale(sid)

        # 3. 清理非 accumulate 的 child session。
        # [AutoC 2026-05-30] 与 _cleanup_stale_sessions_locked 保持一致：
        #   - accumulate: 永久保留
        #   - 无 context_mode（历史遗留）: 直接清理，不等 TTL
        #   - fresh/fork: 24h 无活动后清理
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        for sid, entry in list(self._session_store._registry.items()):
            if sid in seen_stale or not isinstance(entry, dict):
                continue
            if not entry.get("is_child"):
                continue
            ctx_mode = str(entry.get("context_mode") or "").strip()
            if ctx_mode == "accumulate":
                continue
            # 无 context_mode 的历史 child session 直接清理
            if not ctx_mode:
                mark_stale(sid)
                continue
            # fresh/fork: 正常 TTL 检查
            updated_str = (
                entry.get("last_active_at")
                or entry.get("updated_at")
                or entry.get("created_at")
                or ""
            )
            try:
                updated_at = datetime.fromisoformat(str(updated_str))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at < stale_threshold:
                    mark_stale(sid)
            except Exception:
                pass

        # 执行清理。
        for sid in stale_sids:
            # [AutoC 2026-05-30] Why: startup reconcile 不再依赖 EventLog 中的 task
            # 快照，因此必须同步清理 session 相关内存索引、会话文件和 registry。
            # How: 对 branch 复用已有递归清理逻辑；对普通 child/reset session 执行
            # 轻量清理并收缩 parent/child 索引。Purpose: 重启后只保留 sessions.json
            # 中仍可作为主状态源的有效 session。
            if self._is_entry_branch_session_locked(sid):
                self._cleanup_branch_locked(sid)
                tr_path = self.workspace_root / "data" / "transcripts" / f"{sid}.jsonl"
                if tr_path.exists():
                    try:
                        tr_path.unlink()
                    except Exception:
                        pass
                # [AutoC 2026-05-30] Why: _cleanup_branch_locked handles branch
                # conversation JSONL and indexes, but branch transcript files are a
                # separate runtime artifact. How: remove the transcript here in the
                # startup-specific reconcile path. Purpose: no branch-owned runtime
                # residue survives a supervisor restart.
                cleaned += 1
                continue

            self.sessions.pop(sid, None)
            self.session_generations.pop(sid, None)
            self._cancelled_sessions.discard(sid)
            self._purge_context_usage_for_session(sid)
            self._remove_child_mapping_for_session_locked(sid)
            self.entry_branch_parents.pop(sid, None)
            for branches in self.parent_entry_branches.values():
                branches.discard(sid)
            for children in self.parent_children.values():
                children.discard(sid)
            self.parent_entry_branches = {
                parent_sid: branches
                for parent_sid, branches in self.parent_entry_branches.items()
                if branches
            }
            self.parent_children = {
                parent_sid: children
                for parent_sid, children in self.parent_children.items()
                if children
            }

            conv_path = self.workspace_root / "data" / "conversations" / f"{sid}.jsonl"
            if conv_path.exists():
                try:
                    conv_path.unlink()
                except Exception:
                    pass
            tr_path = self.workspace_root / "data" / "transcripts" / f"{sid}.jsonl"
            if tr_path.exists():
                try:
                    tr_path.unlink()
                except Exception:
                    pass
            if self._session_store._registry.pop(sid, None) is not None:
                cleaned += 1

        if cleaned:
            self._session_store._flush()
            logger.info(
                "startup reconcile: cleaned %d stale sessions (reset/orphan-branch/expired-child)",
                cleaned,
            )
        else:
            logger.info("startup reconcile: no stale sessions found")

    # ------------------------------------------------------------------ #
    #  事件回放
    # ------------------------------------------------------------------ #

    def rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        """[DEPRECATED] 从 EventLog 事件重建内存状态。

        [AutoC 2026-05-30] 此方法不再在启动路径使用。
        EventLog 已降级为纯审计日志，启动状态完全从 sessions.json 恢复。
        保留此方法仅供测试和一次性迁移脚本使用。
        """
        for e in events:
            et = e.get("type")
            session_id = str(e.get("session_id") or "")
            payload = e.get("payload") or {}

            try:
                seq = int(e.get("seq", 0))
            except Exception:
                seq = 0

            if et == "session_created":
                self._apply_session_created(session_id, payload)
            elif et == "inbound_message":
                conv = payload.get("conversation_key")
                if isinstance(conv, str) and session_id:
                    self.conversation_map[conv] = session_id
                    # 方案 B: 若 session 记录缺失，从 inbound 事件恢复最小 SessionInfo
                    if session_id not in self.sessions:
                        self.sessions[session_id] = SessionInfo(
                            session_id=session_id,
                            channel=payload.get("channel", ""),
                            conversation_key=conv,
                            created_at=_now(),
                            updated_at=_now(),
                            # [AutoC 2026-06-01] Why: event replay may synthesize
                            # a SessionInfo for old logs that never had a stored
                            # provider override. How: leave the new field at its
                            # empty default. Purpose: rebuild compatibility stays
                            # explicit while the dataclass grows a new field.
                        )
                self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "inbound_processed":
                self._apply_inbound_processed(payload)
            elif et == "outbound_message":
                self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "approval_requested":
                self._apply_approval_requested(payload)
            elif et == "approval_decided":
                self._apply_approval_decided(payload)
            elif et in {"task_created", "task_started", "task_completed", "task_cancelled", "task_cancel_requested", "task_requeued", "task_suspended", "task_resumed"}:
                self._apply_task_snapshot(payload)
            elif et == "context_reset":
                conv = payload.get("conversation_key")
                if isinstance(conv, str):
                    self.conversation_map.pop(conv, None)
            elif et == "cancel_requested":
                self._apply_cancel_requested(session_id, payload)
            elif et == "node_switch":
                self._apply_node_switch(session_id, payload)
            elif et == "engine_registered":
                wid = str(payload.get("worker_id") or "").strip()
                gid = str(payload.get("generation_id") or "").strip()
                if wid and gid:
                    self._engine_generations[wid] = gid

        self._advance_inbound_cursor()
    # ------------------------------------------------------------------ #
    #  节点切换
    # ------------------------------------------------------------------ #

    def _apply_node_switch(self, session_id: str, payload: dict[str, Any]) -> None:
        target = str(payload.get("target_node_id") or "").strip()
        if session_id and target:
            self.session_entry_overrides[session_id] = target
        elif session_id and not target:
            self.session_entry_overrides.pop(session_id, None)

    def _is_switchable_entry_node(self, node_id: str) -> bool:
        """Use node metadata rather than deployment-specific node ID lists."""
        from engine.node import load_node

        if node_id.startswith("system."):
            return False
        node = load_node(self.workspace_root, node_id)
        if node is None:
            return True
        ctl = getattr(node, "control_tools", "all")
        can_switch_back = ctl in (None, "all") or (isinstance(ctl, (list, set, tuple)) and "switch_node" in ctl)
        return (node.extra.get("switchable", True) is not False) and can_switch_back

    def switch_session_node(self, session_id: str, target_node_id: str) -> dict[str, Any]:
        """设置或清除 session 级入口节点覆盖。"""
        sid = (session_id or "").strip()
        target = (target_node_id or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id required"}
        with self._lock:
            # [Fork/Merge 2026-05-17] Why: switch_node can be invoked from an
            # entry branch, but future inbound routing reads overrides from the
            # parent session. How: normalize the supplied session id before
            # mutating session_entry_overrides or writing node_switch. Purpose:
            # the switch survives branch merge/cleanup.
            route_sid = self._route_session_id_for_session_locked(sid)
            if route_sid not in self.sessions:
                return {"ok": False, "error": "session not found"}
            default_node = self._default_entry_node()
            if target and not self._is_switchable_entry_node(target):
                return {"ok": False, "error": f"node '{target}' is an internal/child node and cannot be used as session entry"}
            if target and target != default_node:
                self.session_entry_overrides[route_sid] = target
                self.session_last_entry_node[route_sid] = target
                persisted_entry_node = target
            else:
                self.session_entry_overrides.pop(route_sid, None)
                self.session_last_entry_node[route_sid] = default_node
                target = ""  # 清除覆盖
                persisted_entry_node = ""
            info = self.sessions.get(route_sid)
            if info is not None and info.entry_node_id != persisted_entry_node:
                # Why: switch_node is an explicit session-level route change, but
                # session_entry_overrides is intentionally cleared on restart.
                # How: mirror the effective override state into SessionInfo and
                # sessions.json whenever the live switch endpoint runs. Purpose:
                # restart recovery uses the same entry node, and clearing a switch
                # does not leave a stale persisted target behind.
                info.entry_node_id = persisted_entry_node
                self._session_store.update_entry_node(route_sid, persisted_entry_node)
            self.eventlog.append(
                session_id=route_sid,
                component="engine",
                type_="node_switch",
                payload={
                    "target_node_id": target,
                    "default_node_id": default_node,
                    "ts": _now().isoformat(),
                    "requested_session_id": sid,
                },
            )
            return {
                "ok": True,
                "session_id": route_sid,
                "target_node_id": target or default_node,
                "is_override": bool(target),
            }

    def _effective_approval_rules_locked(self, session_id: str, task_id: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve live grants through stored parent relationships, never channel names."""
        task = self.tasks.get(task_id) if task_id else None
        # Synchronous dispatch keeps task.session_id on the parent for routing;
        # its execution/history session is stored separately in task.input.
        sid = str(task.input.get("child_session_id") or task.session_id) if task else session_id
        seen: set[str] = set()
        while sid and sid not in seen:
            seen.add(sid)
            info = self.sessions.get(sid)
            entry = self._session_store._registry.get(sid, {})
            rules = stored_approval_rules(info.approval_rules if info is not None else entry.get("approval_rules"))
            if rules is not None:
                return rules, sid
            parent = str(entry.get("parent_session_id") or "")
            if not parent:
                parent = self.entry_branch_parents.get(sid, "")
            if not parent and task and task.session_id == sid and task.caller_task_id:
                task = self.tasks.get(task.caller_task_id)
                parent = task.session_id if task else ""
            sid = parent
        return None, None

    def get_session_approval_rules(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            info = self.sessions.get(session_id)
            entry = self._session_store._registry.get(session_id)
            if info is None and (entry is None or entry.get("reset")):
                return None
            effective, source = self._effective_approval_rules_locked(session_id)
            own = info.approval_rules if info is not None else entry.get("approval_rules")
            return {"rules": stored_approval_rules(own),
                    "effective_rules": effective, "source_session_id": source}

    def set_session_approval_rules(self, session_id: str, rules: dict[str, Any] | None) -> dict[str, Any] | None:
        clean = validate_approval_rules(rules) if rules is not None else None
        with self._lock:
            info = self.sessions.get(session_id)
            entry = self._session_store._registry.get(session_id)
            if info is None and (entry is None or entry.get("reset")):
                return None
            self._session_store.update_approval_rules(session_id, clean)
            if info is not None:
                info.approval_rules = clean
            self.eventlog.append(session_id=session_id, component="supervisor",
                                 type_="session_approval_rules_changed", payload={"session_id": session_id})
            return self.get_session_approval_rules(session_id)

    def get_session_provider_override(self, session_id: str) -> dict[str, Any] | None:
        """读取 session 级 provider 覆盖配置。"""
        sid = (session_id or "").strip()
        with self._lock:
            # [AutoC 2026-06-01] Why: engine tasks may run on entry branches while
            # the durable provider override is attached to the parent session. How:
            # normalize a possible branch id before reading SessionInfo. Purpose:
            # all runtime tasks see the same session-scoped provider choice.
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            return dict(info.provider_override or {})

    def set_session_provider_override(self, session_id: str, provider_override: dict[str, Any]) -> dict[str, Any] | None:
        """设置 session 级 provider 覆盖配置并持久化。"""
        sid = (session_id or "").strip()
        clean = dict(provider_override or {}) if isinstance(provider_override, dict) else {}
        with self._lock:
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            # [AutoC 2026-06-01] Why: the API accepts arbitrary JSON dicts for
            # provider adapters and future fields. How: copy the dict into
            # SessionInfo and mirror it to sessions.json through SessionStore.
            # Purpose: runtime provider overrides are durable without constraining
            # future provider-specific configuration keys.
            info.provider_override = clean
            self._session_store.update_provider_override(route_sid, clean)
            self.eventlog.append(
                session_id=route_sid,
                component="supervisor",
                type_="provider_override_updated",
                # [AutoC 2026-06-01] Why: provider_override may contain api_key or
                # provider-specific secret fields. How: audit only safe summary
                # metadata and the field names, never the raw dict. Purpose:
                # updates remain observable without leaking credentials to
                # data/events.jsonl.
                payload={
                    "provider": str(clean.get("provider") or clean.get("provider_type") or ""),
                    "model": str(clean.get("model") or ""),
                    "keys": sorted(str(k) for k in clean.keys()),
                    "api_key_present": bool(clean.get("api_key")),
                    "requested_session_id": sid,
                    "ts": _now().isoformat(),
                },
            )
            return dict(clean)

    def clear_session_provider_override(self, session_id: str) -> dict[str, Any] | None:
        """清除 session 级 provider 覆盖配置并持久化。"""
        # [AutoC 2026-06-01] Why: DELETE should share the same persistence and
        # event path as PUT. How: delegate to the setter with an empty dict.
        # Purpose: the in-memory session and sessions.json are cleared together.
        return self.set_session_provider_override(session_id, {})

    def get_session_workspace(self, session_id: str) -> dict[str, Any] | None:
        """读取 session 级工作区配置 {name, path}。"""
        sid = (session_id or "").strip()
        with self._lock:
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            return dict(info.workspace or {})

    def set_session_workspace(self, session_id: str, workspace: dict[str, Any]) -> dict[str, Any] | None:
        """设置 session 级工作区配置并持久化。"""
        sid = (session_id or "").strip()
        clean = dict(workspace or {}) if isinstance(workspace, dict) else {}
        # 只保留 name/path 两个字段，避免脏数据
        name = str(clean.get("name") or "").strip()
        path = str(clean.get("path") or "").strip() or None
        clean = {"name": name}
        if path:
            clean["path"] = path
        with self._lock:
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            info.workspace = clean
            self._session_store.update_workspace(route_sid, clean)
            self.eventlog.append(
                session_id=route_sid,
                component="supervisor",
                type_="workspace_updated",
                payload={
                    "name": name,
                    "path": path or "",
                    "requested_session_id": sid,
                    "ts": _now().isoformat(),
                },
            )
            return dict(clean)

    def clear_session_workspace(self, session_id: str) -> dict[str, Any] | None:
        """清除 session 级工作区配置并持久化。"""
        return self.set_session_workspace(session_id, {})

    def get_session_active_node(self, session_id: str) -> dict[str, Any]:
        """获取 session 当前实际使用的入口节点。
        优先级: AI switch override > 上次 inbound 实际用的节点 > session 记录 > 全局默认"""
        sid = (session_id or "").strip()
        with self._lock:
            # [Fork/Merge 2026-05-17] Why: callers may ask using a branch id seen
            # in task metadata. How: resolve entry branches to their parent before
            # reading override/last-entry maps. Purpose: active-node queries match
            # the session that will receive the next inbound.
            route_sid = self._route_session_id_for_session_locked(sid)
            override = self.session_entry_overrides.get(route_sid, "").strip()
            last_used = self.session_last_entry_node.get(route_sid, "").strip()
            info = self.sessions.get(route_sid)
            # Why: immediately after restart, getActiveNode has no last-used
            # memory. How: include the persisted SessionInfo.entry_node_id before
            # falling back to the global default. Purpose: UI callers see the
            # same session-level route that inbound task creation will use.
            recorded = (info.entry_node_id if info else "").strip() if info else ""
            default_node = self._default_entry_node()
            node_id = override or last_used or recorded or default_node
            return {
                "node_id": node_id,
                "is_override": bool(override),
                "default_node_id": default_node,
                "session_id": route_sid,
            }

    # ------------------------------------------------------------------ #
    #  Child Session 隔离（Phase A + B）
    # ------------------------------------------------------------------ #

    def session_children(self, *, session_id: str) -> list[dict[str, Any]]:
        """Return non-branch child sessions that belong to a parent session.

        [2026-06-03] Why: the web frontend cannot rebuild childNodes from
        ConversationStore history alone after a browser refresh. How: read the
        durable sessions.json child registry, walk active entry branches, and enrich
        each child row with the latest matching task status. Purpose: the frontend can
        restore child-session navigation without replaying old WebSocket events.
        """
        parent = str(session_id or "").strip()
        if not parent:
            return []

        def _task_sort_key(task: Task) -> datetime:
            return task.updated_at or task.created_at or _now()

        def _frontend_status(task_status: str) -> str:
            # [2026-06-03] Why: Supervisor task states are more granular than the web
            # child-node badge states. How: collapse pending/running/suspended-like
            # states into "running" and preserve terminal states. Purpose: the API
            # returns the same small status vocabulary the frontend already renders.
            if task_status in {"completed", "failed", "cancelled"}:
                return task_status
            if task_status == "awaiting_approval":
                return "awaiting_approval"
            return "running"

        with self._lock:
            registry = getattr(self._session_store, "_registry", {})
            child_ids: set[str] = set()
            branch_ids: set[str] = set()
            stack = [parent]
            visited: set[str] = set()

            while stack:
                current_parent = stack.pop()
                if current_parent in visited:
                    continue
                visited.add(current_parent)
                for child_sid in self.parent_children.get(current_parent, set()):
                    raw = registry.get(child_sid) if isinstance(registry, dict) else None
                    if not isinstance(raw, dict) or raw.get("reset"):
                        continue
                    if str(raw.get("node_id") or "") == "__entry_branch__" or child_sid.startswith("branch_"):
                        branch_ids.add(child_sid)
                        stack.append(child_sid)
                        continue
                    child_ids.add(child_sid)

            for (child_parent, _node_id, _ctx_key), child_sid in self.child_session_map.items():
                if child_parent == parent or child_parent in branch_ids:
                    raw = registry.get(child_sid) if isinstance(registry, dict) else None
                    if isinstance(raw, dict) and not raw.get("reset"):
                        child_ids.add(child_sid)

            rows: list[dict[str, Any]] = []
            for child_sid in sorted(child_ids):
                raw = registry.get(child_sid) if isinstance(registry, dict) else None
                if not isinstance(raw, dict) or raw.get("reset"):
                    continue
                node_id = str(raw.get("node_id") or "")
                context_key = str(raw.get("context_key") or "")
                if not node_id:
                    for (child_parent, mapped_node, mapped_key), mapped_sid in self.child_session_map.items():
                        if mapped_sid == child_sid:
                            node_id = mapped_node
                            context_key = context_key or mapped_key
                            break

                # [2026-09-14] Why: accumulate 复用与嵌套派发的新任务运行在子会话的
                # 入口分支（branch_*）上，旧行为只按 session_id/child_session_id 归属任务，
                # 分支上的任务对不上任何行——worker 自身任务已终结而分支任务仍在跑时，
                # /children、manage_workers、前端子节点面板全部漏看（实测 96edb136 会话
                # 两个分支任务 20+ 分钟不可见）。How: 把子会话的活跃入口分支并入任务归属
                # （与 running_tasks 端点同源的解析视图）。Purpose: 子会话行状态反映其
                # 分支上的真实任务，工具与面板不再漏看嵌套/复用派发。
                _child_branch_ids = self._entry_branch_ids_for_parent_locked(child_sid)
                related_tasks = [
                    task for task in self.tasks.values()
                    if str(task.input.get("child_session_id") or "").strip() == child_sid
                    or task.session_id == child_sid
                    or task.session_id in _child_branch_ids
                ]
                latest_task = max(related_tasks, key=_task_sort_key) if related_tasks else None
                if latest_task is not None:
                    task_status = latest_task.status.value
                else:
                    # [2026-09-14] Why: 内存任务表会定期裁剪（scheduler 只保留最近
                    # 200 个终态任务）且重启后仅回放 5000 条事件，子会话关联任务
                    # 消失后旧行为无条件报 "running"，早已结束的子会话在前端常驻
                    # 为运行中幽灵（实测挂 20+ 小时）。How: 无任务时回退 registry：
                    # terminal_status 存在（终态写回）直接采用；否则按
                    # last_active_at/created_at 距今是否超过宽限期（15 分钟）判定，
                    # 超过则报 completed。Purpose: 任务被裁剪或重启丢失后，子会话
                    # 状态立即收敛到真实终态，前端浮动面板自动隐藏。
                    _reg_status = str(raw.get("terminal_status") or "").strip()
                    if _reg_status in {"completed", "failed", "cancelled"}:
                        task_status = _reg_status
                    else:
                        task_status = "running"
                        _ref_str = str(
                            raw.get("last_active_at")
                            or raw.get("updated_at")
                            or raw.get("created_at")
                            or ""
                        )
                        try:
                            _ref_dt = datetime.fromisoformat(_ref_str)
                            if _ref_dt.tzinfo is None:
                                _ref_dt = _ref_dt.replace(tzinfo=timezone.utc)
                            if (_now() - _ref_dt) >= timedelta(minutes=15):
                                task_status = "completed"
                        except Exception:
                            pass
                started_at = latest_task.created_at.isoformat() if latest_task and latest_task.created_at else str(raw.get("created_at") or "")
                updated_at = latest_task.updated_at.isoformat() if latest_task and latest_task.updated_at else str(raw.get("last_active_at") or raw.get("created_at") or "")
                terminal = task_status in {"completed", "failed", "cancelled"}

                rows.append({
                    "session_id": child_sid,
                    "parent_session_id": str(raw.get("parent_session_id") or ""),
                    "route_parent_session_id": parent,
                    "node_id": node_id or "unknown",
                    "context_key": context_key,
                    "context_mode": str(raw.get("context_mode") or ""),
                    "status": _frontend_status(task_status),
                    "task_id": latest_task.task_id if latest_task is not None else "",
                    "started_at": started_at,
                    "updated_at": updated_at,
                    "completed_at": updated_at if terminal else "",
                })

            rows.sort(key=lambda item: str(item.get("started_at") or ""))
            return rows

    def _mark_child_session_terminal(self, task: Task) -> None:
        """任务终态时把终态写回关联子会话 registry。

        [2026-09-14] Why: dispatch/spawn_worker 子会话创建后 last_active_at 从不
        更新（update_last_active 仅在 accumulate 复用时被调用，其文档串声称的
        "子 task 创建/完成时调用" 接线从未存在），24h TTL 只能按创建时间算；
        且 scheduler 会把终态任务裁剪出内存、重启后只回放 5000 条事件，
        session_children() 找不到任务时无从得知终态。How: 在 complete_task 与
        超时回收处调用，把 last_active_at / completed_at / terminal_status 写入
        task.session_id 及 task.input.child_session_id 对应的 is_child registry
        条目。Purpose: TTL 按真实活动时间计算；任务从内存消失后子会话仍能报出
        准确终态。
        """
        candidates: list[str] = []
        _sid = str(getattr(task, "session_id", "") or "").strip()
        if _sid:
            candidates.append(_sid)
        if isinstance(task.input, dict):
            _alt = str(task.input.get("child_session_id") or "").strip()
            if _alt and _alt not in candidates:
                candidates.append(_alt)
        touched = False
        _now_iso = _now().isoformat()
        _status = str(getattr(getattr(task, "status", None), "value", "") or "")
        for cid in candidates:
            entry = self._session_store._registry.get(cid)
            if isinstance(entry, dict) and entry.get("is_child"):
                entry["last_active_at"] = _now_iso
                entry["completed_at"] = _now_iso
                if _status:
                    entry["terminal_status"] = _status
                touched = True
        if touched:
            self._session_store._flush()

    def get_or_create_child_session(
        self,
        parent_session_id: str,
        node_id: str,
        context_key: str,
        context_mode: str,
    ) -> tuple[str, bool]:
        """根据 context_mode 获取或创建 child session。

        Child Session 隔离核心方法。三种 context_mode：
        - accumulate：复用已有 child session（如存在且未过期），否则新建
        - fresh：强制创建新 child session，旧的不删除（等 TTL 过期清理）
        - fork：创建新 child session，调用方需额外做 ConversationStore.fork()

        Returns:
            (child_session_id, is_new) — is_new 表示是否新建了 session
        """
        map_key = (parent_session_id, node_id, context_key)

        if context_mode == "accumulate":
            existing_cid = self.child_session_map.get(map_key)
            if existing_cid and not self._is_child_session_expired(existing_cid):
                # 复用现有 child session，更新 last_active_at
                self._session_store.update_last_active(existing_cid)
                return existing_cid, False
            # 已过期或不存在：新建（过期的旧 session 留给 TTL 清理）
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        elif context_mode == "fresh":
            # 强制新建，旧映射更新为新 ID
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        elif context_mode == "fork":
            # fork 与 fresh 类似：总是新建
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        else:
            # 未知模式，按 accumulate 处理
            logger.warning("unknown context_mode '%s', falling back to accumulate", context_mode)
            return self.get_or_create_child_session(
                parent_session_id, node_id, context_key, "accumulate",
            )

    def _create_child_session(
        self, parent_session_id: str, node_id: str, context_key: str, context_mode: str,
    ) -> str:
        """生成新 child session 并持久化映射。

        Child Session 隔离（Phase A）：生成 child_{uuid4().hex[:12]} 格式的 ID，
        写入 child_session_map、parent_children、sessions.json。
        """
        child_sid = f"child_{uuid.uuid4().hex[:12]}"
        map_key = (parent_session_id, node_id, context_key)

        # 更新内存映射
        self.child_session_map[map_key] = child_sid
        self.parent_children.setdefault(parent_session_id, set()).add(child_sid)

        # 持久化到 sessions.json
        self._session_store.on_child_session_created(
            child_session_id=child_sid,
            parent_session_id=parent_session_id,
            node_id=node_id,
            context_key=context_key,
        )
        # [AutoC 2026-05-30] Why: 24h TTL 清理需要知道 child session 的
        # context_mode，才能删除 fresh/fork 并永久保留 accumulate。How: child
        # 创建后立即补写 context_mode 到持久 registry 并落盘。Purpose: 重启后
        # stale cleanup 仍能按模式执行正确清理。
        self._session_store._registry[child_sid]["context_mode"] = str(context_mode or "").strip()
        self._session_store._flush()

        logger.info(
            "child session created: %s (parent=%s, node=%s, key=%s)",
            child_sid, parent_session_id, node_id, context_key,
        )
        return child_sid

    def _is_child_session_expired(self, child_session_id: str) -> bool:
        """检查 child session 是否已超过 TTL。

        Child Session 隔离（Phase A）：从 sessions.json 的 last_active_at 字段
        判定是否过期。如果记录不存在或已 reset，视为过期。
        """
        from clonoth_runtime import get_int, load_runtime_config
        runtime_cfg = load_runtime_config(self.workspace_root)
        ttl_hours = get_int(runtime_cfg, "engine.child_session.ttl_hours", 24, min_value=1)

        entry = self._session_store._registry.get(child_session_id)
        if entry is None or entry.get("reset"):
            return True
        # [AutoC 2026-05-30] Why: accumulate child session 是长期上下文，老大要求
        # 永久保留，不能再按 24h TTL 失效。How: 在 TTL 判断前直接保留明确标记
        # 为 accumulate 的记录。Purpose: get_or_create_child_session 不会因时间流逝
        # 创建新的 accumulate 会话并留下旧记录。
        if str(entry.get("context_mode") or "").strip() == "accumulate":
            return False
        last_active_str = entry.get("last_active_at", "")
        if not last_active_str:
            return True
        try:
            last_active = datetime.fromisoformat(last_active_str)
            age_hours = (_now() - last_active).total_seconds() / 3600
            return age_hours > ttl_hours
        except (ValueError, TypeError):
            return True

    def _expire_child_session(self, child_session_id: str) -> None:
        """删除一个过期的 child session。

        Child Session 隔离（Phase A）：删除 JSONL 文件、从映射表移除、物理删除 registry 条目。
        """
        # 1. 删除 JSONL 文件与缓存
        self._delete_conversation_session_locked(child_session_id)

        # 2. 从映射表移除
        key_to_remove = None
        for key, cid in self.child_session_map.items():
            if cid == child_session_id:
                key_to_remove = key
                break
        if key_to_remove:
            del self.child_session_map[key_to_remove]
            parent_sid = key_to_remove[0]
            if parent_sid in self.parent_children:
                self.parent_children[parent_sid].discard(child_session_id)

        # [AutoC 2026-05-30] Why: fresh/fork child session 过期后应从
        # sessions.json 物理删除。How: 删除 registry 条目而不是写 reset=true。
        # Purpose: 让完成或过期的临时 child session 不再堆积。
        self.sessions.pop(child_session_id, None)
        self.session_generations.pop(child_session_id, None)
        self._cancelled_sessions.discard(child_session_id)
        self._purge_context_usage_for_session(child_session_id)
        self._session_store.remove_session(child_session_id)

        logger.info("child session expired: %s", child_session_id)

    def _task_referenced_session_ids_locked(self) -> set[str]:
        """收集仍被非终态 task 引用的 session id（须持 self._lock）。

        [2026-11-02] 审计修复：Why: TTL sweep 曾只看 task.session_id，而 dispatch
        task 把 child_session_id / parent_session_id / branch_session_id 存在 input 里，
        sweep 会把这些仍在被使用的会话连同 JSONL 一起删掉，Worker 后续写入落到已删
        除的历史上。How: 遍历所有非终态 task，收集 session_id 与上述四类引用，并把
        它们的入口分支一并纳入。Purpose: 清理永不触碰活跃关系。
        """
        protected: set[str] = set()
        for task in list(self.tasks.values()):
            if self._task_terminal(task):
                continue
            data = task.input if isinstance(getattr(task, "input", None), dict) else {}
            origin = data.get("_dispatch_origin")
            origin = origin if isinstance(origin, dict) else {}
            chain_origin = getattr(task, "output_chain_source_dispatch_origin", None)
            chain_origin = chain_origin if isinstance(chain_origin, dict) else {}
            for value in (
                str(getattr(task, "session_id", "") or ""),
                str(data.get("parent_session_id") or ""),
                str(data.get("child_session_id") or ""),
                str(data.get("branch_session_id") or ""),
                str(data.get("_route_session_id") or ""),
                str(origin.get("parent_session_id") or ""),
                str(getattr(task, "output_chain_source_session_id", "") or ""),
                str(chain_origin.get("parent_session_id") or ""),
            ):
                sid = value.strip()
                if sid:
                    protected.add(sid)
        # A still-running descendant may return after its caller has finished.
        # Preserve the caller's session chain until that callback can be delivered.
        pending = list(protected)
        while pending:
            sid = pending.pop()
            info = self._session_store._registry.get(sid)
            parent = str(info.get("parent_session_id") or "").strip() if isinstance(info, dict) else ""
            if parent and parent not in protected:
                protected.add(parent)
                pending.append(parent)
        branches_for_parent = getattr(self, "_entry_branch_ids_for_parent_locked", None)
        if callable(branches_for_parent):
            for pid in list(protected):
                protected.update(branches_for_parent(pid))
        return protected

    def _cleanup_stale_sessions_locked(self) -> None:
        """清理 sessions.json 中已无用的 branch 和 child session 记录。

        [2026-11-02] 审计修复：Why: 旧 sweep 只按 task.session_id 判断活跃，且删除
        registry 时只删 conversations JSONL，留下 transcripts、cache、conversation_map
        与 child 映射的悬挂引用。How: 先算「非终态 task 引用集合」作为硬保护，再
        对 reset / 孤儿 branch / 过期 fresh-fork child 统一走
        _delete_conversation_session_locked + 映射收缩。Purpose: 完成/历史对话在 TTL
        内可回看（与历史 UI 一致），过期后磁盘与索引一起干净回收。
        """
        now = datetime.now(timezone.utc)
        stale_threshold = now - timedelta(hours=24)
        protected = self._task_referenced_session_ids_locked()
        to_remove: list[str] = []

        for sid, info in list(self._session_store._registry.items()):
            sid = str(sid or "").strip()
            if not sid or not isinstance(info, dict):
                continue
            # 活跃关系硬保护：仍被非终态 task（含 dispatch child/branch）引用就不删。
            if sid in protected:
                continue
            # [AutoC 2026-05-30] reset=true 条目已经不会参与活跃会话恢复。
            if info.get("reset"):
                to_remove.append(sid)
                continue
            # 孤儿入口分支：只要没有非终态 task 跑在它上面就回收。
            if (
                sid.startswith("branch_")
                or str(info.get("node_id") or "") == "__entry_branch__"
                or sid in self.entry_branch_parents
            ):
                to_remove.append(sid)
                continue
            # child-like 记录：is_child / 有 parent / agent:* 派发会话（含老数据形状）。
            conv_key = str(info.get("conversation_key") or "")
            is_child = (
                bool(info.get("is_child"))
                or bool(info.get("parent_session_id"))
                or conv_key.startswith("agent:")
            )
            if not is_child:
                continue
            ctx_mode = str(info.get("context_mode") or "").strip()
            # accumulate 是长期上下文，永久保留。
            if ctx_mode == "accumulate":
                continue
            # 无 context_mode 的历史 child session 直接清理，不等 TTL。
            if not ctx_mode:
                to_remove.append(sid)
                continue
            updated_str = str(
                info.get("last_active_at")
                or info.get("updated_at")
                or info.get("created_at")
                or ""
            )
            try:
                updated_at = datetime.fromisoformat(updated_str)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at < stale_threshold:
                    to_remove.append(sid)
            except Exception:
                # 时间不可解析时保守保留，避免误删。
                pass

        if to_remove:
            for sid in to_remove:
                if sid in protected:
                    continue
                # [2026-11-02] 统一清理：conversation JSONL + transcripts + cache
                # 走同一个 helper；registry / conversation_map / child 映射 / 分支索引
                # 在同一处收缩，不留孤儿文件与悬挂映射。
                self._delete_conversation_session_locked(sid)
                for _ck, _mapped in list(self.conversation_map.items()):
                    if _mapped == sid:
                        self.conversation_map.pop(_ck, None)
                self._session_store._registry.pop(sid, None)
                self.sessions.pop(sid, None)
                self.session_generations.pop(sid, None)
                self._cancelled_sessions.discard(sid)
                self._purge_context_usage_for_session(sid)
                self._remove_child_mapping_for_session_locked(sid)
                self.entry_branch_parents.pop(sid, None)
                for branches in self.parent_entry_branches.values():
                    branches.discard(sid)
                for children in self.parent_children.values():
                    children.discard(sid)
            self.parent_entry_branches = {pid: branches for pid, branches in self.parent_entry_branches.items() if branches}
            self.parent_children = {pid: children for pid, children in self.parent_children.items() if children}
            self._session_store._flush()
            logger.info("stale session cleanup: removed %d sessions from registry", len(to_remove))

    # ------------------------------------------------------------------ #
    #  审批
    # ------------------------------------------------------------------ #

    def _apply_approval_requested(self, payload: dict[str, Any]) -> None:
        try:
            a = Approval.model_validate(payload)
        except Exception:
            return
        self.approvals[a.approval_id] = a

    def _apply_approval_decided(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("approval_id")
        if approval_id in self.approvals:
            a = self.approvals[approval_id]
            decision = payload.get("decision")
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = payload.get("comment")
            a.decided_at = _now()

    def create_approval(
        self,
        *,
        session_id: str,
        operation: str,
        details: dict[str, Any],
        tool_call_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        trust_level: str = "",
    ) -> Approval:
        now = _now()
        # [AutoC 2026-05-31] Why: duplicate pending approval suppression must not
        # collapse two simultaneous calls to the same operation and path. How: add
        # optional execution identity to the fingerprint source. Purpose: each
        # ToolCallCard can own its own approval while legacy callers still dedupe by
        # operation/details when no identity is available.
        fingerprint_src = json.dumps({
            "operation": operation,
            "details": details,
            "tool_call_id": tool_call_id or "",
            "node_id": node_id or "",
            "task_id": task_id or "",
        }, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:12]

        approval = Approval(
            approval_id=str(uuid.uuid4()),
            session_id=session_id,
            operation=operation,
            details=details,
            fingerprint=fingerprint,
            status=ApprovalStatus.pending,
            decision=None,
            comment=None,
            requested_at=now,
            decided_at=None,
            tool_call_id=tool_call_id,
            node_id=node_id,
            task_id=task_id,
            trust_level=trust_level,
        )

        with self._lock:
            # 同指纹的 pending 审批不再重复创建
            for a in self.approvals.values():
                if a.fingerprint == fingerprint and a.status == ApprovalStatus.pending:
                    return a

            self.approvals[approval.approval_id] = approval
            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="approval_requested",
                payload=approval.model_dump(mode="json"),
            )
        return approval

    def decide_approval(self, *, approval_id: str, decision: str, comment: str | None = None) -> Approval | None:
        with self._lock:
            a = self.approvals.get(approval_id)
            if a is None or a.status != ApprovalStatus.pending:
                return a
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = comment
            a.decided_at = _now()
            self.eventlog.append(
                session_id=a.session_id,
                component="supervisor",
                type_="approval_decided",
                payload={
                    "approval_id": a.approval_id,
                    "decision": decision,
                    "comment": comment,
                    "ts": a.decided_at.isoformat(),
                    # [AutoC 2026-05-31] Why: approval_decided must find the same
                    # ToolExecution that approval_requested updated. How: mirror
                    # the stored execution identity into the decision event. Purpose:
                    # the frontend does not need a separate lookup endpoint.
                    "tool_call_id": a.tool_call_id,
                    "node_id": a.node_id,
                    "task_id": a.task_id,
                },
            )
            return a

    def _is_subagent_operation_locked(self, session_id: str, task_id: str | None) -> bool:
        """Identify delegated agents from stored ancestry, excluding entry branches.

        Tool tasks may share the parent's routing session, so inspect their caller
        chain too. A caller ID alone does not make a main agent's tool a subagent.
        """
        task = self.tasks.get(task_id) if task_id else None
        seen_tasks: set[str] = set()
        while True:
            sid = str(task.input.get("child_session_id") or task.session_id) if task else session_id
            seen_sessions: set[str] = set()
            while sid and sid not in seen_sessions:
                seen_sessions.add(sid)
                entry = self._session_store._registry.get(sid, {})
                parent = str(entry.get("parent_session_id") or self.entry_branch_parents.get(sid, ""))
                is_branch = entry.get("node_id") == "__entry_branch__" or sid in self.entry_branch_parents
                if entry.get("is_child") and not is_branch:
                    return True
                sid = parent
            caller_id = task.caller_task_id if task else None
            if not caller_id or caller_id in seen_tasks:
                return False
            seen_tasks.add(caller_id)
            task = self.tasks.get(caller_id)
            if task is None:
                return False

    def request_operation(
        self,
        *,
        session_id: str,
        op: str,
        parameters: dict[str, Any],
        tool_call_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        workspace: str | None = None,
        tool_name: str | None = None,
    ) -> OpRequestOut:
        _ws = Path(workspace) if workspace else None
        decision = self.policy.evaluate(op=op, parameters=parameters, workspace=_ws)
        _tl = decision.trust_level or ""
        if decision.safety_level == SafetyLevel.auto:
            return OpRequestOut(safety_level=SafetyLevel.auto, reason=decision.reason, approval_id=None, trust_level=_tl)
        # [AutoC 2026-06-04] Why: hard policy denials must not be converted into
        # human approval requests. How: return SafetyLevel.deny immediately before
        # any scheduler or approval handling. Purpose: destructive or invalid
        # operations stay blocked even when they come from unattended jobs.
        if decision.safety_level == SafetyLevel.deny:
            return OpRequestOut(safety_level=SafetyLevel.deny, reason=decision.reason, approval_id=None, trust_level=_tl)

        # [AutoC 2026-06-04] Why: scheduler-triggered tasks have no source inbound
        # sequence, so their approval events cannot be routed to Discord and would
        # leave the task suspended forever. How: when the active task belongs to a
        # session whose conversation_key starts with "scheduler:", convert the
        # approval-required decision into an automatic allow. Purpose: scheduled
        # maintenance tasks can continue while normal user tasks still use approval.
        #
        # [AutoC 2026-08-10] Why: system nodes (dream, memory_extractor, compactor,
        # turn_summarizer) are internal maintenance tasks with no human operator
        # watching. Their approval_requested events either route to nobody or hang
        # forever. How: also auto-approve when node_id starts with "system.".
        # Purpose: system nodes can use save_memory / delete_memory / etc. without
        # blocking on approvals that will never be reviewed.
        if task_id:
            task = self.tasks.get(task_id)
            if task:
                session = self.sessions.get(task.session_id or session_id)
                conversation_key = str(getattr(session, "conversation_key", "") or "") if session else ""
                if conversation_key.startswith("scheduler:"):
                    return OpRequestOut(
                        safety_level=SafetyLevel.auto,
                        reason="auto-approved: scheduler task",
                        approval_id=None,
                    )
                # [AutoC 2026-08-11] Why: scheduled tasks with custom
                # conversation_key (e.g. "duck_hunt") don't start with
                # "scheduler:" and are not owned by any adapter, so their
                # approval events are never consumed and tasks hang forever.
                # How: also check task.input for schedule_id. Purpose: all
                # schedule-triggered tasks auto-approve regardless of
                # conversation_key format.
                if task.input.get("schedule_id"):
                    return OpRequestOut(
                        safety_level=SafetyLevel.auto,
                        reason="auto-approved: scheduled task",
                        approval_id=None,
                    )
        # System nodes: auto-approve regardless of conversation_key
        if node_id and str(node_id).startswith("system."):
            return OpRequestOut(
                safety_level=SafetyLevel.auto,
                reason=f"auto-approved: system node ({node_id})",
                approval_id=None,
            )

        with self._lock:
            # A repeated request must not bypass an already-issued human approval.
            for pending in self.approvals.values():
                if (pending.status == ApprovalStatus.pending
                        and pending.session_id == session_id and pending.operation == op
                        and pending.details == parameters
                        and (pending.tool_call_id or "") == (tool_call_id or "")
                        and (pending.task_id or "") == (task_id or "")
                        and (pending.node_id or "") == (node_id or "")):
                    return OpRequestOut(safety_level=SafetyLevel.approval_required,
                                        reason=decision.reason, approval_id=pending.approval_id, trust_level=_tl)
            rules, source = self._effective_approval_rules_locked(session_id, task_id)
            name = str(parameters.get("tool_name") or "") if op == "tool_call" else str(tool_name or op)
            if rules is not None and approval_action(rules, tool=name, operation=op, trust_level=_tl) == "allow":
                return OpRequestOut(safety_level=SafetyLevel.auto,
                                    reason=f"auto-approved: session rules ({source})", trust_level=_tl)

            # Delegated agents report missing authorization instead of waiting for
            # a user who may never open their session. Existing pending stays above.
            if self._is_subagent_operation_locked(session_id, task_id):
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason=(
                        "Subagent operation requires human authorization and was not executed. "
                        "Subagents do not wait for approval. Use an already authorized alternative "
                        "or report this limitation to the parent task; do not repeat the same request."
                    ),
                    trust_level=_tl,
                )

            # Keep rule evaluation and creation atomic with a concurrent rule edit.
            approval = self.create_approval(
                session_id=session_id,
                operation=op,
                details=parameters,
                tool_call_id=tool_call_id,
                node_id=node_id,
                task_id=task_id,
                trust_level=_tl,
            )
            return OpRequestOut(safety_level=SafetyLevel.approval_required, reason=decision.reason, approval_id=approval.approval_id, trust_level=_tl)

    # ------------------------------------------------------------------ #
    #  Admin / 杂项
    # ------------------------------------------------------------------ #

    def list_events(self, *, session_id: str, after_seq: int) -> list[dict[str, Any]]:
        return self.eventlog.list_events(session_id=session_id, after_seq=after_seq)

    def admin_state(self) -> AdminStateOut:
        approval_counts = {s.value: 0 for s in ApprovalStatus}
        pending: list[Approval] = []
        for a in self.approvals.values():
            approval_counts[a.status.value] = approval_counts.get(a.status.value, 0) + 1
            if a.status == ApprovalStatus.pending:
                pending.append(a)

        task_counts = {s.value: 0 for s in TaskStatus}
        for t in self.tasks.values():
            task_counts[t.status.value] = task_counts.get(t.status.value, 0) + 1

        engine_last_seen_at, engine_worker_id = self.engine_seen_snapshot()

        return AdminStateOut(
            sessions=len(self.sessions),
            approvals=approval_counts,
            tasks=task_counts,
            pending_approvals=pending,
            engine_runtime={
                "worker_id": engine_worker_id,
                "last_seen_at": engine_last_seen_at,
                "workers": sorted({t.worker_id for t in self.tasks.values() if t.worker_id}),
            },
        )

    # ------------------------------------------------------------------ #
    #  Direction 2: Engine Generation ID
    # ------------------------------------------------------------------ #

    def register_engine(self, worker_id: str, generation_id: str) -> dict[str, Any]:
        """Register an engine worker with its generation ID.

        Cancels any running/pending tasks from the same worker_id with a
        different generation (orphans from a previous engine instance).
        """
        wid = (worker_id or "").strip()
        gid = (generation_id or "").strip()
        if not wid or not gid:
            return {"ok": False, "error": "worker_id and generation_id required"}

        with self._lock:
            old_gen = self._engine_generations.get(wid)
            self._engine_generations[wid] = gid

            orphan_count = 0
            if old_gen and old_gen != gid:
                # Same worker restarted with new generation — cancel its old tasks
                orphan_count = self._cancel_worker_orphans_locked(wid)
            elif not old_gen:
                # First registration of this worker — also clean up any tasks
                # from a previous run where this worker_id was used
                orphan_count = self._cancel_worker_orphans_locked(wid)

            # [AutoC 2026-09-02] 引擎重启后，其名下 running 的异步工具实际已死亡：
            # 标记 lost 并向对应会话注入通知，不再静默丢失。
            for _lost in self._mark_worker_async_tools_lost_locked(wid):
                _sid = str(_lost.get("session_id") or "")
                if not _sid or _sid not in self.sessions:
                    continue
                try:
                    self.inject_async_result(
                        _sid,
                        text=(
                            f'⚠️ 异步工具 "{_lost.get("tool_name", "")}" '
                            f'(id: {_lost.get("async_id", "")}) 因引擎重启中断，结果已丢失。'
                        ),
                        source_task_id=str(_lost.get("task_id") or ""),
                    )
                except Exception:
                    pass

            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="engine",
                type_="engine_registered",
                payload={
                    "worker_id": wid,
                    "generation_id": gid,
                    "previous_generation_id": old_gen or "",
                    "orphans_cancelled": orphan_count,
                    "ts": _now().isoformat(),
                },
            )

            # ---- Deferred restart notification ----
            # Injected here (after orphan cleanup) so the new task gets the
            # current generation and won't be reaped as an orphan.
            if self._pending_restart_notify:
                _notify_sid = self._pending_restart_notify
                self._pending_restart_notify = None  # consume once
                _si = self.sessions.get(_notify_sid)
                if _si:
                    _restart_evt = self.eventlog.append(
                        session_id=_notify_sid,
                        component="supervisor",
                        type_="inbound_message",
                        payload={
                            "channel": _si.channel,
                            "conversation_key": _si.conversation_key,
                            "text": "[系统通知] Engine 重启已完成，新代码已生效。",
                        },
                    )
                    self.record_inbound_message_event(_restart_evt)

            return {"ok": True, "orphans_cancelled": orphan_count, "generation_id": gid}

    # ------------------------------------------------------------------ #
    #  异步工具登记处（engine 生命周期上报的影子视图）
    # ------------------------------------------------------------------ #

    _ASYNC_TOOLS_MAX = 200  # 已完成条目的保留上限

    def register_async_tool(self, entry: dict[str, Any]) -> dict[str, Any]:
        """登记一个异步工具启动。engine 在创建后台任务时调用。

        Why: 执行真实来源仍在 engine 进程，supervisor 这里只保留影子清单。
        How: 以 async_id 为键覆盖写入；条目上限超限时淘汰最老的已完成条目。
        """
        aid = str(entry.get("async_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "async_id required"}
        with self._lock:
            self._async_tools[aid] = {
                "async_id": aid,
                "tool_name": str(entry.get("tool_name") or ""),
                "session_id": str(entry.get("session_id") or ""),
                "task_id": str(entry.get("task_id") or ""),
                "node_id": str(entry.get("node_id") or ""),
                "worker_id": str(entry.get("worker_id") or ""),
                "args_summary": str(entry.get("args_summary") or "")[:200],
                "upgraded_from": str(entry.get("upgraded_from") or ""),
                "status": "running",
                "started_at": _now().isoformat(),
                "finished_at": "",
                "elapsed_sec": None,
                "error": "",
            }
            finished = [
                k for k, v in self._async_tools.items()
                if v.get("status") not in ("running",)
            ]
            if len(finished) > self._ASYNC_TOOLS_MAX:
                finished.sort(key=lambda k: self._async_tools[k].get("finished_at") or "")
                for k in finished[: len(finished) - self._ASYNC_TOOLS_MAX]:
                    del self._async_tools[k]
        return {"ok": True}

    def finish_async_tool(self, async_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """标记一个异步工具完成或失败。engine 在结果投递时调用。"""
        aid = (async_id or "").strip()
        with self._lock:
            entry = self._async_tools.get(aid)
            if entry is None:
                return {"ok": False, "error": "unknown async_id"}
            status = str(body.get("status") or "done").strip()
            if status not in {"done", "failed", "cancelled"}:
                status = "done"
            entry["status"] = status
            entry["finished_at"] = _now().isoformat()
            # [fix 2026-09-03] a finish report settles the entry; a stale
            # cancel_requested flag would linger in the admin list otherwise.
            entry.pop("cancel_requested", None)
            try:
                entry["elapsed_sec"] = round(float(body.get("elapsed_sec")), 1)
            except (TypeError, ValueError):
                entry["elapsed_sec"] = None
            entry["error"] = str(body.get("error") or "")[:500]
        return {"ok": True}

    def list_async_tools(self) -> list[dict[str, Any]]:
        """导出全部异步工具条目，running 条目补算已运行时长。"""
        with self._lock:
            rows = [dict(e) for e in self._async_tools.values()]
        for row in rows:
            if row.get("status") == "running" and row.get("started_at"):
                try:
                    started = datetime.fromisoformat(str(row["started_at"]))
                    row["elapsed_sec"] = round((_now() - started).total_seconds(), 1)
                except Exception:
                    pass
        rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return rows

    def cancel_async_tool(self, async_id: str) -> dict[str, Any]:
        """请求取消一个 running 的异步工具。

        Why: engine 是纯轮询 worker，没有 HTTP 服务接收反向调用。How: 把
        async_id 挂到其 worker_id 的待消费队列；engine 每秒轮询时拉走。
        Purpose: 取消语义与其他 admin 操作一致，幂等且立即可见。
        """
        aid = (async_id or "").strip()
        with self._lock:
            entry = self._async_tools.get(aid)
            if entry is None:
                return {"ok": False, "error": "unknown async_id"}
            if entry.get("status") != "running":
                return {"ok": False, "error": f"not running (status={entry.get('status')})"}
            wid = str(entry.get("worker_id") or "")
            if not wid:
                return {"ok": False, "error": "no worker_id"}
            queue = self._async_tool_pending_cancels.setdefault(wid, [])
            if aid not in queue:
                queue.append(aid)
            entry["cancel_requested"] = True
        return {"ok": True}

    def pull_pending_cancels(self, worker_id: str) -> list[str]:
        """engine 轮询拉取并清空该 worker 的待消费取消队列。"""
        wid = (worker_id or "").strip()
        if not wid:
            return []
        with self._lock:
            return self._async_tool_pending_cancels.pop(wid, [])

    def _mark_worker_async_tools_lost_locked(self, worker_id: str) -> list[dict[str, Any]]:
        """将某 worker 名下 running 的异步工具标记为 lost（引擎重启语义）。"""
        wid = (worker_id or "").strip()
        lost: list[dict[str, Any]] = []
        for entry in self._async_tools.values():
            if entry.get("worker_id") == wid and entry.get("status") == "running":
                entry["status"] = "lost"
                entry["finished_at"] = _now().isoformat()
                entry["error"] = "engine worker restarted"
                entry.pop("cancel_requested", None)
                lost.append(dict(entry))
        self._async_tool_pending_cancels.pop(wid, None)
        return lost

    def _cancel_worker_orphans_locked(self, worker_id: str) -> int:
        """Cancel all non-terminal tasks assigned to a specific worker_id."""
        now = _now()
        count = 0
        for task in self.tasks.values():
            if self._task_terminal(task):
                continue
            if task.worker_id != worker_id:
                continue
            task.cancel_requested = True
            task.status = TaskStatus.cancelled
            task.updated_at = now
            task.lease_expires_at = None
            if task.waiting_for_task_id:
                task.waiting_for_task_id = None
            task.result = {"action": "cancelled", "error": f"engine worker {worker_id} restarted (generation mismatch)"}
            self._cleanup_tool_result_artifacts_for_task(task.task_id)
            self._event_task_snapshot("task_cancelled", task)
            count += 1
        return count

    def write_boot_event(self) -> dict[str, Any]:
        prev = self.eventlog.last_boot_run_id()
        payload = {
            "previous_run_id": prev,
            "restarted": prev is not None,
            "ts": _now().isoformat(),
        }
        return self.eventlog.append(session_id=SYSTEM_SESSION_ID, component="supervisor", type_="boot", payload=payload)
