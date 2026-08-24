"""Session 管理 mixin —— 会话创建、inbound/outbound 队列、消息记录。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from ._helpers import SessionInfo, _now
from .eventlog import SYSTEM_SESSION_ID
from .types import ApprovalStatus, TaskStatus



# ---------------------------------------------------------------------------
#  多模态消息构建（内联版本，避免 supervisor → engine 依赖）
# ---------------------------------------------------------------------------

_ALLOWED_ATT_PREFIX = "data/attachments/"


def _build_multimodal_content(
    text: str, attachments: list[dict[str, Any]],
) -> list[dict[str, Any]] | str:
    """将文本和附件合并为多模态消息内容。

    图片附件生成 file:// image_url 引用；
    文本文件附件生成元数据文本引用（不读内容）。
    """
    parts: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "").strip()
        if not path or not path.replace("\\", "/").lstrip("/").startswith(_ALLOWED_ATT_PREFIX):
            continue
        att_type = str(att.get("type") or "").strip()
        if att_type == "file":
            # Text file: metadata-only reference
            from pathlib import Path as _Path
            name = att.get("name") or _Path(path).name
            mime = att.get("mime_type") or "text/plain"
            parts.append({"type": "text", "text": f"[Attached file: {name} | type: {mime} | path: {path}]"})
        else:
            # Image: file:// reference
            parts.append({"type": "image_url", "image_url": {"url": f"file://{path}"}})
    if not parts:
        return text
    return [{"type": "text", "text": text}] + parts


class SessionMixin:
    """提供会话管理与 inbound/outbound 消息队列方法。

    运行时 self 是 SupervisorState 实例。
    """

    # ---- session generation ----

    def _next_session_generation_locked(self, session_id: str) -> int:
        cur = int(self.session_generations.get(session_id, 0) or 0) + 1
        self.session_generations[session_id] = cur
        return cur

    def _current_session_generation_locked(self, session_id: str) -> int:
        return int(self.session_generations.get(session_id, 0) or 0)

    # ---- Fork/Merge 入口分支管理 ----

    def _ensure_entry_branch_indexes_locked(self) -> None:
        """Ensure fork/merge branch indexes exist on older reconstructed states."""
        # [Fork/Merge 2026-05-12] 兼容旧 SupervisorState 实例或测试桩。
        # 原因：分支索引是新增内存字段，部分单元测试可能直接构造 mixin 所需属性。
        # 做法：在每个分支管理入口惰性补齐两张索引。目的：让新逻辑不依赖
        # __init__ 一定已经按最新代码运行，保持 supervisor 层向后兼容。
        if not hasattr(self, "parent_entry_branches"):
            self.parent_entry_branches = {}
        if not hasattr(self, "entry_branch_parents"):
            self.entry_branch_parents = {}

    def _is_entry_branch_session_locked(self, branch_session_id: str, *, parent_session_id: str | None = None) -> bool:
        """Return whether a session id is an entry fork branch."""
        self._ensure_entry_branch_indexes_locked()
        bid = str(branch_session_id or "").strip()
        if not bid:
            return False
        parent = self.entry_branch_parents.get(bid)
        if parent_session_id is not None and parent and parent != parent_session_id:
            return False
        if parent:
            return True
        entry = getattr(self, "_session_store", None)
        registry = getattr(entry, "_registry", {}) if entry is not None else {}
        raw = registry.get(bid) if isinstance(registry, dict) else None
        if isinstance(raw, dict) and raw.get("node_id") == "__entry_branch__":
            if parent_session_id is None:
                return True
            return str(raw.get("parent_session_id") or "") == parent_session_id
        return bid.startswith("branch_")

    def _entry_branch_ids_for_parent_locked(self, parent_session_id: str) -> set[str]:
        """Collect active entry branch session ids for a parent session."""
        self._ensure_entry_branch_indexes_locked()
        parent = str(parent_session_id or "").strip()
        if not parent:
            return set()
        branch_ids: set[str] = set(self.parent_entry_branches.get(parent, set()))
        for child_sid in self.parent_children.get(parent, set()):
            if self._is_entry_branch_session_locked(child_sid, parent_session_id=parent):
                branch_ids.add(child_sid)
        return branch_ids

    def _route_session_id_for_session_locked(self, session_id: str) -> str:
        """Return the user-facing parent session for an entry branch session."""
        # [Fork/Merge 2026-05-17] Why: public session APIs may still be called with
        # a branch_xxx runtime id from older engine paths or task metadata. How:
        # resolve active branch indexes first, then sessions.json entry-branch
        # records, and fall back to the original id for normal sessions. Purpose:
        # switch_node/context/cancel endpoints mutate the durable conversation.
        self._ensure_entry_branch_indexes_locked()
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        parent = self.entry_branch_parents.get(sid, "")
        if parent:
            return parent
        entry = getattr(self, "_session_store", None)
        registry = getattr(entry, "_registry", {}) if entry is not None else {}
        raw = registry.get(sid) if isinstance(registry, dict) else None
        if isinstance(raw, dict) and raw.get("node_id") == "__entry_branch__":
            stored_parent = str(raw.get("parent_session_id") or "").strip()
            if stored_parent:
                return stored_parent
        return sid

    def _remove_child_mapping_for_session_locked(self, child_session_id: str) -> None:
        """Remove child_session_map entries that point at one session id."""
        # [Fork/Merge 2026-05-12] 分支清理需要删除 branch 自身及其派生 child。
        # 原因：分支运行时可能通过 dispatch 创建普通 child session；只删 JSONL 会留下
        # child_session_map 的悬挂条目。做法：按 value 反查并删除映射，同时更新
        # parent_children。目的：reset 与后续 context_mode=accumulate 不会复用已清理分支。
        keys_to_remove = [key for key, cid in self.child_session_map.items() if cid == child_session_id]
        for key in keys_to_remove:
            self.child_session_map.pop(key, None)
            parent_sid = key[0]
            children = self.parent_children.get(parent_sid)
            if children is not None:
                children.discard(child_session_id)
                if not children:
                    self.parent_children.pop(parent_sid, None)

    def _delete_conversation_session_locked(self, session_id: str) -> None:
        """Delete a ConversationStore JSONL file and invalidate its cache."""
        from engine.conversation_store import ConversationStore

        # [Fork/Merge 2026-05-12] 分支生命周期由 supervisor 管理。
        # 原因：入口分支只是一次 inbound 的执行副本，merge 后继续保留会污染后续查询。
        # 做法：通过 ConversationStore.delete 删除 JSONL 和缓存。目的：确保已合并分支
        # 不再作为可恢复会话被误用。
        store = ConversationStore(self.workspace_root / "data" / "conversations")
        store.delete(session_id)

    def _create_entry_branch_locked(self, parent_session_id: str, inbound_seq: int) -> tuple[str, dict[str, Any]]:
        """Create a branch session for one inbound and fork parent history into it."""
        from engine.conversation_store import ConversationStore

        self._ensure_entry_branch_indexes_locked()
        parent = str(parent_session_id or "").strip()
        seq = int(inbound_seq or 0)
        branch_session_id = f"branch_{seq}" if seq > 0 else f"branch_{uuid.uuid4().hex[:12]}"
        parent_info = self.sessions.get(parent)
        now = _now()
        self.sessions[branch_session_id] = SessionInfo(
            session_id=branch_session_id,
            channel=parent_info.channel if parent_info else "internal",
            # [Fork/Merge 2026-05-12] branch 保留父 conversation_key 作为元数据，
            # 但不写 conversation_map。目的：内部事件可追溯到同一平台会话，
            # 同时 get_or_create_session 仍稳定返回主 session。
            conversation_key=parent_info.conversation_key if parent_info else "",
            created_at=now,
            updated_at=now,
        )
        parent_generation = self._current_session_generation_locked(parent) or 1
        self.session_generations[branch_session_id] = parent_generation
        self.entry_branch_parents[branch_session_id] = parent
        self.parent_entry_branches.setdefault(parent, set()).add(branch_session_id)
        self.parent_children.setdefault(parent, set()).add(branch_session_id)

        # [Fork/Merge 2026-05-12] 持久登记入口分支为内部 child。
        # 原因：reset_conversation 可能在重启后执行，单纯内存索引会丢失未合并分支。
        # 做法：复用 sessions.json 的 child session 记录，并用专用 node_id 区分入口分支。
        # 目的：重置主会话时能找到并删除遗留 branch JSONL。
        self._session_store.on_child_session_created(
            child_session_id=branch_session_id,
            parent_session_id=parent,
            node_id="__entry_branch__",
            context_key=str(seq),
        )

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        fork_meta: dict[str, Any] = {"copied": 0, "base_count": 0, "base_last_id": ""}
        try:
            try:
                fork_result = store.fork(parent, branch_session_id, include_system=True)
            except TypeError:
                # [Fork/Merge 2026-05-12] 兼容旧 ConversationStore.fork 签名。
                # 原因：supervisor 层必须能在 engine store 还未完全升级时通过 py_compile
                # 和本地运行。做法：手动复制父 session 的全部消息，等价实现 include_system=True。
                # 目的：不修改 engine 层文件，也能得到 merge 所需的 branch 基准长度。
                source_messages = list(store.load(parent))
                if store.message_count(branch_session_id) == 0 and source_messages:
                    store.append_batch(branch_session_id, source_messages)
                    fork_result = {
                        "copied": len(source_messages),
                        "base_count": len(source_messages),
                        "base_last_id": getattr(source_messages[-1], "id", "") if source_messages else "",
                    }
                else:
                    branch_count = store.message_count(branch_session_id)
                    fork_result = {"copied": 0, "base_count": branch_count, "base_last_id": ""}
            if isinstance(fork_result, dict):
                fork_meta.update({
                    "copied": int(fork_result.get("copied") or 0),
                    "base_count": int(fork_result.get("base_count") or 0),
                    "base_last_id": str(fork_result.get("base_last_id") or ""),
                })
            else:
                copied = int(fork_result or 0)
                fork_meta.update({"copied": copied, "base_count": copied})
        except Exception as exc:
            fork_meta["error"] = str(exc)

        self.eventlog.append(
            session_id=parent,
            component="supervisor",
            type_="branch_created",
            payload={
                "parent_session_id": parent,
                "branch_session_id": branch_session_id,
                "inbound_seq": seq,
                **fork_meta,
            },
            transient=True,
        )
        return branch_session_id, fork_meta

    def _merge_branch_locked(self, parent_session_id: str, branch_session_id: str, base_count: int) -> int:
        """Merge a completed entry branch back into its parent ConversationStore."""
        from engine.conversation_store import ConversationStore

        parent = str(parent_session_id or "").strip()
        branch = str(branch_session_id or "").strip()
        if not parent or not branch:
            return 0
        store = ConversationStore(self.workspace_root / "data" / "conversations")
        count = 0
        try:
            merge_fn = getattr(store, "merge", None)
            if callable(merge_fn):
                count = int(merge_fn(parent, branch, base_count=int(base_count or 0)) or 0)
            else:
                # [Fork/Merge 2026-05-12] 兼容旧 ConversationStore 无 merge 方法的情况。
                # 原因：本次任务只允许改 supervisor 层，目标树里的 engine store 可能仍是旧版。
                # 做法：读取 branch 中 fork 基准之后的消息并批量追加到 parent。
                # 目的：保持新 supervisor 在旧 store 上也能完成分支回写。
                branch_messages = list(store.load(branch))
                tail = branch_messages[int(base_count or 0):]
                if tail:
                    store.append_batch(parent, tail)
                count = len(tail)
        except Exception as exc:
            self.eventlog.append(
                session_id=parent,
                component="supervisor",
                type_="branch_merge_failed",
                payload={
                    "parent_session_id": parent,
                    "branch_session_id": branch,
                    "base_count": int(base_count or 0),
                    "error": str(exc),
                },
                transient=True,
            )
            return 0

        self.eventlog.append(
            session_id=parent,
            component="supervisor",
            type_="branch_merged",
            payload={
                "parent_session_id": parent,
                "branch_session_id": branch,
                "base_count": int(base_count or 0),
                "merged_count": count,
            },
            transient=True,
        )
        return count

    def _cleanup_branch_locked(self, branch_session_id: str) -> None:
        """Delete one entry branch session and its derived child sessions."""
        self._ensure_entry_branch_indexes_locked()
        branch = str(branch_session_id or "").strip()
        if not branch:
            return
        parent = self.entry_branch_parents.pop(branch, "")
        entry = self._session_store._registry.get(branch)
        if not parent and isinstance(entry, dict):
            parent = str(entry.get("parent_session_id") or "")

        # [Fork/Merge 2026-05-12] 先清理分支派生的普通 child session。
        # 原因：入口分支运行过程中仍可 dispatch 子节点，这些子节点的 parent_session_id
        # 是 branch。做法：删除 parent_children[branch] 下的所有 JSONL、映射和 registry 标记。
        # 目的：merge 后不会留下只能由已删除 branch 访问的悬挂上下文。
        derived_children = list(self.parent_children.pop(branch, set()))
        for child_sid in derived_children:
            if self._is_entry_branch_session_locked(child_sid, parent_session_id=branch):
                self._cleanup_branch_locked(child_sid)
                continue
            self._delete_conversation_session_locked(child_sid)
            self._remove_child_mapping_for_session_locked(child_sid)
            self.sessions.pop(child_sid, None)
            self.session_generations.pop(child_sid, None)
            self._cancelled_sessions.discard(child_sid)
            self._purge_context_usage_for_session(child_sid)
            # [AutoC 2026-05-30] Why: branch 派生 child session 在 branch merge 后
            # 不应继续留在 sessions.json。How: 物理删除 registry 条目而不是标记 reset。
            # Purpose: 防止临时 child 记录随 branch 清理后继续堆积。
            self._session_store.remove_session(child_sid)

        # [Fork/Merge 2026-05-12] 删除 branch 前取消仍挂在该运行 session 下的派生任务。
        # 原因：异步 dispatch 可能让 root entry 已结束但 branch 下仍有 pending/running 子任务。
        # 做法：把这些任务标记为 cancelled 并写快照，不递归 finalize。目的：避免任务继续
        # 写入已清理的 branch session。
        now = _now()
        for task in self.tasks.values():
            if task.session_id != branch or self._task_terminal(task):
                continue
            task.cancel_requested = True
            task.status = TaskStatus.cancelled
            task.updated_at = now
            task.lease_expires_at = None
            if task.waiting_for_task_id:
                task.waiting_for_task_id = None
            self._cleanup_tool_result_artifacts_for_task(task.task_id)
            self._event_task_snapshot("task_cancelled", task)

        self._delete_conversation_session_locked(branch)
        self.sessions.pop(branch, None)
        self.session_generations.pop(branch, None)
        self._cancelled_sessions.discard(branch)
        self._purge_context_usage_for_session(branch)
        self._remove_child_mapping_for_session_locked(branch)
        if parent:
            branches = self.parent_entry_branches.get(parent)
            if branches is not None:
                branches.discard(branch)
                if not branches:
                    self.parent_entry_branches.pop(parent, None)
            children = self.parent_children.get(parent)
            if children is not None:
                children.discard(branch)
                if not children:
                    self.parent_children.pop(parent, None)
        else:
            for branches in self.parent_entry_branches.values():
                branches.discard(branch)
            for children in self.parent_children.values():
                children.discard(branch)
        # [AutoC 2026-05-30] Why: branch merge 完成后应立即从 sessions.json 物理删除，
        # 而不是仅标记 reset，否则注册记录无限堆积。
        # How: 用 remove_session 代替 on_session_reset。
        self._session_store.remove_session(branch)
        self.eventlog.append(
            session_id=parent or branch,
            component="supervisor",
            type_="branch_cleaned",
            payload={"parent_session_id": parent, "branch_session_id": branch},
            transient=True,
        )

    # ---- 引擎心跳 ----

    def mark_engine_seen(self, *, worker_id: str) -> None:
        with self._lock:
            self._engine_last_seen_at = _now()
            self._engine_last_worker_id = worker_id

    def engine_seen_snapshot(self) -> tuple[datetime | None, str | None]:
        with self._lock:
            return self._engine_last_seen_at, self._engine_last_worker_id

    # ---- 工具热重载信号 ----

    def bump_tools_reload(self) -> int:
        with self._lock:
            self._tools_reload_seq += 1
            return self._tools_reload_seq

    def tools_reload_seq(self) -> int:
        with self._lock:
            return self._tools_reload_seq

    # ---- inbound 游标 ----

    def _advance_inbound_cursor(self) -> None:
        while self._inbound_cursor < len(self._inbound_order):
            seq = self._inbound_order[self._inbound_cursor]
            if seq in self._inbound_processed or seq in self._inbound_routed:
                self._inbound_cursor += 1
                continue
            break

    # ---- 事件 apply（rebuild 用） ----

    def _apply_session_created(self, session_id: str, payload: dict[str, Any]) -> None:
        if not session_id:
            return
        created_at = _now()
        existing_info = self.sessions.get(session_id)
        existing_entry_node = existing_info.entry_node_id if existing_info else ""
        info = SessionInfo(
            session_id=str(session_id or ""),
            channel=str(payload.get("channel") or ""),
            conversation_key=str(payload.get("conversation_key") or ""),
            created_at=created_at,
            updated_at=created_at,
            # Why: sessions.json is loaded before event replay, but older
            # session_created events do not contain entry_node_id. How: preserve
            # a value already restored from SessionStore, while still accepting
            # a future event payload that may include the field. Purpose: replay
            # cannot erase the persisted session entry-node binding.
            entry_node_id=str(payload.get("entry_node_id") or existing_entry_node or ""),
        )
        self.sessions[info.session_id] = info
        conv = info.conversation_key
        if conv:
            self.conversation_map[conv] = info.session_id

    def _apply_inbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not isinstance(seq, int) or seq <= 0 or not session_id:
            return
        # [2026-06-03] Why: the sidebar sorts conversations by session.updated_at, but
        # this (the effective, later-defined) inbound handler never refreshed it, so
        # updated_at stayed equal to created_at and the list was effectively sorted by
        # creation time. How: bump updated_at whenever a new inbound message lands.
        # Purpose: a conversation receiving fresh user input rises to the top.
        si = self.sessions.get(session_id)
        if si is not None:
            si.updated_at = _now()
            self._session_store.touch_updated_at(session_id, si.updated_at)
        if seq not in self._inbound_events:
            self._inbound_events[seq] = {"session_id": session_id, "payload": payload}
            self._inbound_order.append(seq)
            # Why: inbound side effects now belong to supervisor hook handlers.
            # How: fire only when this seq is first applied, so replayed or
            # duplicate inbound events do not repeatedly disturb handler state.
            # Purpose: let MemoryExtractHandler cancel idle extraction without
            # keeping timer dictionaries on SupervisorState.
            self.hook_registry.fire(
                "on_inbound_message",
                # Why: engine.builtin handlers cannot receive SupervisorState.
                # How: pass a callback-only context plus inbound metadata.
                # Purpose: preserve idle-timer cancellation without cyclic imports.
                self._build_supervisor_hook_ctx(
                    session_id=session_id,
                    inbound_seq=seq,
                    payload=payload,
                ),
            )

    def _apply_inbound_processed(self, payload: dict[str, Any]) -> None:
        try:
            inbound_seq = int(payload.get("inbound_seq", 0))
        except Exception:
            return
        if inbound_seq > 0:
            self._inbound_processed.add(inbound_seq)

    def _apply_outbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not session_id or not isinstance(payload, dict):
            return
        src = payload.get("source_inbound_seq")
        try:
            inbound_seq = int(src) if src is not None else 0
        except Exception:
            inbound_seq = 0
        # [2026-06-03] Why: bump session.updated_at on outbound (reply/finish) so a
        # conversation that just produced a response also rises in the recency-sorted
        # sidebar. How: refresh updated_at in memory (no persist call — SupervisorState
        # has no _persist_session; the session store serializes updated_at elsewhere).
        # Purpose: both user input and assistant output count as recent activity
        # without throwing AttributeError on every inbound/outbound.
        si = self.sessions.get(session_id)
        if si is not None:
            si.updated_at = _now()
            # [AutoC 2026-06-04] Outbound is low-frequency enough to flush.
            self._session_store.touch_updated_at(session_id, si.updated_at)
            self._session_store._flush()
        if inbound_seq > 0 and inbound_seq not in self._inbound_routed:
            self._inbound_routed[inbound_seq] = {
                "action": "reply",
                "session_id": session_id,
                "event_seq": int(seq or 0),
            }

    # ---- 公开方法：会话操作 ----

    def cancel_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self._lock:
            if sid not in self.sessions:
                return False
            generation = self._next_session_generation_locked(sid)
            self._cancelled_sessions.add(sid)
            self._cancel_session_tasks_locked(sid)
            # [Fork/Merge 2026-05-12] 主 session 取消必须覆盖所有入口分支。
            # 原因：入口 task 已经运行在 branch session 上，只取消主 session 会遗漏正在
            # pending/running 的分支任务。做法：遍历 parent→branches 索引并逐个调用
            # 现有 task 取消逻辑。目的：保持 /sessions/{id}/cancel 的语义仍是取消整段对话。
            for branch_sid in self._entry_branch_ids_for_parent_locked(sid):
                self._cancelled_sessions.add(branch_sid)
                self._cancel_session_tasks_locked(branch_sid)
            self.eventlog.append(
                session_id=sid,
                component="supervisor",
                type_="cancel_requested",
                payload={"session_id": sid, "ts": _now().isoformat(), "session_generation": generation},
            )
            return True

    def is_cancelled(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._cancelled_sessions

    def clear_cancelled(self, session_id: str) -> None:
        with self._lock:
            self._cancelled_sessions.discard(session_id)

    def is_task_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get((task_id or "").strip())
            if task is None:
                return True
            current_gen = self._current_session_generation_locked(task.session_id)
            if current_gen and task.session_generation != current_gen:
                return True
            return task.cancel_requested or task.status == TaskStatus.cancelled

    def record_inbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "inbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            if session_id and session_id in self.sessions:
                self._create_entry_task_for_inbound_locked(inbound_seq=seq, session_id=session_id, payload=payload)

    def record_outbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "outbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not session_id or not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()

    # ---- inbound 队列 ----

    def assign_next_inbound(self, *, worker_id: str, lease_sec: float = 30.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        with self._lock:
            now = _now()
            lease_val = max(5.0, min(lease_sec, 120.0))
            for i in range(self._inbound_cursor, len(self._inbound_order)):
                seq = self._inbound_order[i]

                if seq in self._inbound_processed or seq in self._inbound_routed:
                    if i == self._inbound_cursor:
                        self._inbound_cursor += 1
                    self._inbound_leases.pop(seq, None)
                    continue

                lease = self._inbound_leases.get(seq)
                if lease is not None and lease.expires_at > now:
                    continue

                evt = self._inbound_events.get(seq)
                if not isinstance(evt, dict):
                    continue

                session_id = str(evt.get("session_id") or "")
                payload = evt.get("payload")
                if not session_id or not isinstance(payload, dict):
                    continue

                self._inbound_leases[seq] = self._InboundLease(
                    worker_id=wid,
                    expires_at=now + timedelta(seconds=lease_val),
                )
                return {"inbound_seq": seq, "session_id": session_id, **payload}

            return None

    def ack_inbound(self, *, inbound_seq: int, worker_id: str) -> bool:
        wid = (worker_id or "").strip()
        if not wid:
            return False
        try:
            seq = int(inbound_seq)
        except Exception:
            seq = 0
        if seq <= 0:
            return False

        with self._lock:
            if seq in self._inbound_processed:
                return True
            if seq not in self._inbound_events:
                return False
            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="shell",
                type_="inbound_processed",
                payload={"inbound_seq": seq, "worker_id": wid, "ts": _now().isoformat()},
            )
            self._inbound_processed.add(seq)
            self._inbound_leases.pop(seq, None)
            self._advance_inbound_cursor()
            return True

    # ---- outbound / session 创建 ----

    def get_or_create_session(self, *, channel: str, conversation_key: str) -> str:
        with self._lock:
            # 方案 C: 检查映射指向的 session 是否真实存在，清除幽灵映射
            existing_sid = self.conversation_map.get(conversation_key)
            if existing_sid is not None:
                if existing_sid in self.sessions:
                    return existing_sid
                # 幽灵映射：conversation_map 有记录但 sessions 中无对应条目，清除
                self.conversation_map.pop(conversation_key, None)

            session_id = str(uuid.uuid4())
            created_at = _now()

            info = SessionInfo(
                session_id=session_id,
                channel=channel,
                conversation_key=conversation_key,
                created_at=created_at,
                updated_at=created_at,
            )
            self.sessions[session_id] = info
            self.conversation_map[conversation_key] = session_id

            # 方案 A: 持久化到 sessions.json
            self._session_store.on_session_created(info)

            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="session_created",
                payload={
                    "session_id": session_id,
                    "channel": channel,
                    "conversation_key": conversation_key,
                    "created_at": created_at.isoformat(),
                },
            )
            return session_id

    def append_outbound_message(
        self,
        *,
        session_id: str,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        source_inbound_seq: int | None = None,
        node_id: str | None = None,
        action_type: str | None = None,
        llm_request_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        text_clean = str(text or "").strip()
        # [Fix] 当 source_inbound_seq 存在时，允许空文本通过。
        # 空 outbound_message 事件用于触发 Bot 侧 trigger/status_msg 的清理收尾。
        # 仅在无 source_inbound_seq（如 API 直接调用发消息）时仍拒绝空消息。
        if not text_clean and not attachments and source_inbound_seq is None:
            return {"ok": False, "reason": "empty"}

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                v = int(source_inbound_seq)
            except Exception:
                v = 0
            if v > 0:
                src_seq = v

        with self._lock:
            if session_id not in self.sessions:
                raise KeyError("session not found")

            if src_seq is not None:
                inbound_evt = self._inbound_events.get(src_seq)
                if not isinstance(inbound_evt, dict):
                    raise ValueError(f"source_inbound_seq not found: {src_seq}")
                if str(inbound_evt.get("session_id") or "") != session_id:
                    raise ValueError("source_inbound_seq session mismatch")

                existing = self._inbound_routed.get(src_seq)
                if isinstance(existing, dict) and existing.get("action") == "reply":
                    return {"ok": True, "deduped": True, "route": existing}

            payload: dict[str, Any] = {"text": text_clean}
            if attachments:
                payload["attachments"] = list(attachments)
            if src_seq is not None:
                payload["source_inbound_seq"] = src_seq
            if node_id:
                payload["node_id"] = node_id
            if llm_request_id:
                # [AutoC 2026-06-04] Why: outbound_message is emitted after the task
                # has already finished, but it must finalize the card for one LLM
                # request. How: persist the request id captured by the engine in the
                # outbound event payload. Purpose: clients replace the existing stream
                # card instead of creating a second final card.
                payload["llm_request_id"] = str(llm_request_id).strip()
            if action_type:
                # [AutoC 2026-05-31] Why: Phase 0 routes ask through the same
                # outbound path as finish, but Phase 1 needs to distinguish the
                # originating terminal action. How: optionally persist action_type
                # on outbound payloads. Purpose: add metadata without changing
                # existing callers that do not pass it.
                payload["action_type"] = str(action_type).strip()
            if task_id:
                # [AutoC 2026-06-30] Why: outbound_message previously lacked task_id,
                # so the frontend could not match it to the streaming card when
                # source_inbound_seq was absent (e.g. Web frontend inbound). How:
                # persist the originating task_id in the outbound event payload.
                # Purpose: frontend findOutboundReplacementTarget can match by
                # task_id as a fallback when llm_request_id alone is insufficient.
                payload["task_id"] = str(task_id).strip()

            evt = self.eventlog.append(
                session_id=session_id,
                component="shell",
                type_="outbound_message",
                payload=payload,
            )

            if src_seq is not None and src_seq not in self._inbound_routed:
                self._inbound_routed[src_seq] = {
                    "action": "reply",
                    "session_id": session_id,
                    "event_seq": int(evt.get("seq", 0) or 0),
                }

            return {"ok": True, "deduped": False, "event_seq": int(evt.get("seq", 0) or 0)}

    def reset_conversation(self, *, conversation_key: str) -> dict[str, Any]:
        """Reset a conversation by removing the conversation_map entry.

        Next inbound message with this conversation_key will create a fresh session.
        Also cleans up node_contexts for the old session.

        Child Session 隔离（Phase C）：级联清理所有关联的 child session，
        包括 JSONL 文件、映射表条目、sessions.json 标记。
        """
        with self._lock:
            old_session_id = self.conversation_map.pop(conversation_key, None)
            if not old_session_id:
                return {"ok": False, "error": f"conversation not found: {conversation_key}"}

            # 方案 A: 标记 session 为已重置
            self._session_store.on_session_reset(old_session_id)

            # Child Session 隔离（Phase C）：级联清理所有关联的 child session
            cleared_children = 0
            conv_dir = self.workspace_root / "data" / "conversations"

            # 删除主 session 的 JSONL
            main_jsonl = conv_dir / f"{old_session_id}.jsonl"
            if main_jsonl.exists():
                try:
                    main_jsonl.unlink()
                except Exception:
                    pass

            # [Fork/Merge 2026-05-12] reset 时必须先收集未合并入口分支。
            # 原因：branch session 可能已从内存索引丢失，但仍存在于 parent_children 或
            # sessions.json。做法：把 parent_children 和 entry_branch 索引合并后逐个判断。
            # 目的：清理对话时不会留下未 merge 的 branch JSONL 和派生 child session。
            child_ids = set(self.parent_children.pop(old_session_id, set()))
            child_ids.update(self._entry_branch_ids_for_parent_locked(old_session_id))
            for child_sid in list(child_ids):
                if self._is_entry_branch_session_locked(child_sid, parent_session_id=old_session_id):
                    self._cleanup_branch_locked(child_sid)
                    cleared_children += 1
                    continue
                child_jsonl = conv_dir / f"{child_sid}.jsonl"
                if child_jsonl.exists():
                    try:
                        child_jsonl.unlink()
                    except Exception:
                        pass
                self._remove_child_mapping_for_session_locked(child_sid)
                self.sessions.pop(child_sid, None)
                self.session_generations.pop(child_sid, None)
                self._cancelled_sessions.discard(child_sid)
                self._purge_context_usage_for_session(child_sid)
                # [AutoC 2026-05-30] Why: reset 主会话时清理的是 child session，
                # 这些临时上下文不应在 sessions.json 中保留 reset 标记。
                # How: 对 child 使用物理删除，主 session 仍在上方保留 on_session_reset。
                # Purpose: 清理对话时同步收缩 child registry。
                self._session_store.remove_session(child_sid)
                cleared_children += 1

            # 清理 child_session_map 中所有以 old_session_id 为 parent 的条目
            keys_to_remove = [k for k in self.child_session_map if k[0] == old_session_id]
            for k in keys_to_remove:
                del self.child_session_map[k]

            # [2026-05-28] dispatch session 级联清理。
            # 为什么：异步 dispatch 统一走 inbound 后，子节点 session 的 conversation_key
            #   以 agent:{node_id}:{parent_conv_key} 为前缀。重置父会话时应级联清除。
            # 怎么改：扫描 conversation_map 中以 agent:*:{conversation_key} 为前缀的
            #   条目，删除对应 session 及其 JSONL。
            # 目的：避免父会话重置后赖留已无用的 dispatch session。
            _dispatch_prefix = f":{conversation_key}"
            _dispatch_keys_to_remove: list[str] = []
            for ck, sid in self.conversation_map.items():
                if ck.startswith("agent:") and ck.endswith(_dispatch_prefix):
                    _dispatch_keys_to_remove.append(ck)
                elif ck.startswith("agent:") and _dispatch_prefix + ":" in ck:
                    # 匹配 fresh/fork 模式的 agent:{node}:{parent_conv}:{uuid}
                    _dispatch_keys_to_remove.append(ck)
            for ck in _dispatch_keys_to_remove:
                _dsid = self.conversation_map.pop(ck, None)
                if _dsid:
                    # 清理 dispatch session 的 JSONL 和内存状态
                    _d_jsonl = conv_dir / f"{_dsid}.jsonl"
                    if _d_jsonl.exists():
                        try:
                            _d_jsonl.unlink()
                        except Exception:
                            pass
                    self.sessions.pop(_dsid, None)
                    self.session_generations.pop(_dsid, None)
                    self._cancelled_sessions.discard(_dsid)
                    self._purge_context_usage_for_session(_dsid)
                    # [AutoC 2026-05-30] Why: dispatch session 是由父会话派生的临时
                    # child/fork 会话，重置父会话时不需要保留其 registry 行。
                    # How: 物理删除 sessions.json 条目。
                    # Purpose: 避免 agent:* 派生 session 在主会话 reset 后继续堆积。
                    self._session_store.remove_session(_dsid)
                    cleared_children += 1

            return {"ok": True, "old_session_id": old_session_id,
                    "conversation_key": conversation_key,
                    "cleared_children": cleared_children}

    def session_messages(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Read session messages from ConversationStore (persistent JSONL).

        [2026-06-06] Rewritten: previously read from EventLog (in-memory, lost on
        restart). Now reads from ConversationStore which is the durable source of
        truth. EventLog is only used for real-time event streaming and audit.
        """
        from pathlib import Path
        from engine.conversation_store import ConversationStore

        try:
            store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            stored_msgs = list(store.load(session_id))
        except Exception:
            return []

        # Also merge active entry branch messages (same as session_history_structured)
        with self._lock:
            branch_ids = self._entry_branch_ids_for_parent_locked(session_id)
        if branch_ids:
            parent_msg_ids = {m.id for m in stored_msgs}
            for bid in branch_ids:
                try:
                    branch_msgs = store.load(bid)
                    for bm in branch_msgs:
                        if bm.id not in parent_msg_ids:
                            stored_msgs.append(bm)
                except Exception:
                    pass
            stored_msgs.sort(key=lambda m: m.created_at or "")

        # Convert Message objects to the simple {role, content} format
        # expected by callers (engine _fetch_history, hook ctx, /messages API)
        msgs: list[dict[str, Any]] = []
        for m in stored_msgs:
            if m.role in ("user", "assistant", "system"):
                content = m.content
                if isinstance(content, str) and content.strip():
                    msgs.append({"role": m.role, "content": content})
                elif isinstance(content, list) and content:
                    msgs.append({"role": m.role, "content": content})

        if limit > 0:
            msgs = msgs[-limit:]
        return msgs

    # ---- 结构化历史 (ConversationStore) ----

    def session_history_structured(self, *, session_id: str, limit: int = 200, task_id: str | None = None) -> list[dict[str, Any]]:
        """Read structured messages from ConversationStore for web frontend.

        Returns Message objects with thinking, tool_calls, and tool results
        as structured fields rather than flattened text.

        If task_id is provided, only messages whose source_task_id matches are returned.
        """
        from pathlib import Path
        from engine.conversation_store import ConversationStore

        store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
        messages = list(store.load(session_id))

        # [2026-06-02] Merge active entry branch messages into the parent history.
        # Why: the web frontend calls this with the parent session_id, but the latest
        # conversation turn may still be in an unmerged branch_xxx session. How: find
        # all active entry branches for this parent and append their messages (that
        # are newer than the parent's last message) to the result. Purpose: history
        # reconstruction shows the same content as live WebSocket streaming.
        with self._lock:
            branch_ids = self._entry_branch_ids_for_parent_locked(session_id)
        if branch_ids:
            parent_msg_ids = {m.id for m in messages}
            for bid in branch_ids:
                branch_msgs = store.load(bid)
                for bm in branch_msgs:
                    if bm.id not in parent_msg_ids:
                        messages.append(bm)
            # Re-sort by created_at to maintain chronological order
            messages.sort(key=lambda m: m.created_at or "")

        # [AutoC 2026-06-04] Why: the active-task modal needs to show only the
        # messages produced by a single task, not the entire session history.
        # How: filter by source_task_id when the caller provides a task_id.
        # Purpose: operators can inspect one task's work without noise from
        # prior tasks in the same long-lived session.
        if task_id:
            messages = [m for m in messages if getattr(m, 'source_task_id', '') == task_id]

        # [2026-06-07] Structural compatibility for older mixed terminal-tool
        # rejection rows. Why: old rows predate MessageMeta.tool_rejection_code,
        # but the corresponding assistant message still records the tool_calls
        # batch shape. How: if one assistant turn calls finish/ask together with
        # non-reply tools, mark all tool results in that batch as hidden by tool
        # call id. Purpose: the frontend can hide the protocol rejection without
        # parsing localized rejection text.
        hidden_tool_result_ids: dict[str, str] = {}
        terminal_tools = {"finish", "ask"}
        for amsg in messages:
            calls = getattr(amsg, "tool_calls", None)
            if getattr(amsg, "role", "") != "assistant" or not isinstance(calls, list):
                continue
            names: list[str] = []
            ids: list[str] = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "").strip()
                if name:
                    names.append(name)
                cid = str(call.get("id") or "").strip()
                if cid:
                    ids.append(cid)
            terminal_name = next((name for name in names if name in terminal_tools), "")
            has_non_reply_other = any(name not in (*terminal_tools, "reply") for name in names)
            if terminal_name and has_non_reply_other:
                code = f"{terminal_name}_colocated"
                for cid in ids:
                    hidden_tool_result_ids[cid] = code

        def _history_attachment_from_item(item: Any) -> dict[str, Any] | None:
            if isinstance(item, dict):
                return dict(item)
            if isinstance(item, str):
                path = item.replace("file://", "").lstrip("/").strip()
                if not path:
                    return None
                suffix = Path(path).suffix.lower()
                image_exts = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
                return {
                    "path": path,
                    "name": Path(path).name or "附件",
                    "type": "image" if suffix in image_exts else "file",
                    "mime_type": f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else suffix.lstrip('.')}" if suffix in image_exts else "",
                }
            return None

        def _history_attachments_from_value(value: Any, depth: int = 0) -> list[dict[str, Any]]:
            if depth > 5:
                return []
            if isinstance(value, str):
                text = value.strip()
                if not text or ("attachments" not in text and "data/" not in text):
                    return []
                try:
                    parsed = json.loads(text)
                except Exception:
                    return []
                return _history_attachments_from_value(parsed, depth + 1)
            if isinstance(value, list):
                out: list[dict[str, Any]] = []
                for item in value:
                    att = _history_attachment_from_item(item)
                    if att is not None:
                        out.append(att)
                    out.extend(_history_attachments_from_value(item, depth + 1))
                return out
            if not isinstance(value, dict):
                return []
            out: list[dict[str, Any]] = []
            for key in ("attachments", "tool_attachments", "tool_result_attachments", "attachment_paths", "attachmentPaths"):
                raw = value.get(key)
                if isinstance(raw, list):
                    for item in raw:
                        att = _history_attachment_from_item(item)
                        if att is not None:
                            out.append(att)
            for key in ("data", "result", "output", "payload"):
                out.extend(_history_attachments_from_value(value.get(key), depth + 1))
            deduped: list[dict[str, Any]] = []
            seen: set[str] = set()
            for att in out:
                key = str(att.get("path") or att.get("url") or att.get("name") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                deduped.append(att)
            return deduped

        result: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "message_type": msg.message_type,
                "created_at": msg.created_at,
                "source_node_id": msg.source_node_id,
                # [AutoC 2026-06-03] Why: source_task_id is useful for auditing
                # hydrated callback rows and remains harmless for ordinary history.
                # How: include the existing ConversationStore field in the structured
                # history response. Purpose: frontend message sources can preserve
                # task metadata after refresh.
                "source_task_id": msg.source_task_id,
            }
            # [2026-08-19] source_inbound_seq: expose for retry button in web frontend.
            if isinstance(msg.meta, dict) and msg.meta.get("source_inbound_seq") is not None:
                try:
                    entry["source_inbound_seq"] = int(msg.meta["source_inbound_seq"])
                except (ValueError, TypeError):
                    pass
            # Extract thinking/reasoning from meta
            if isinstance(msg.meta, dict):
                reasoning = str(
                    msg.meta.get("reasoning")
                    or msg.meta.get("thinking_text")
                    or msg.meta.get("thinking")
                    or ""
                )
                _llm_req_id = str(msg.meta.get("llm_request_id") or "").strip()
                if _llm_req_id:
                    entry["llm_request_id"] = _llm_req_id
                if reasoning:
                    # [AutoC 2026-06-04] Why: refreshed pages must render historical
                    # reasoning with the same timed ThinkingBlock header as live streams.
                    # How: accept new and legacy reasoning keys, normalize timestamp
                    # aliases, and fall back to created_at when old rows predate timing.
                    # Purpose: history no longer falls back to the character-count title.
                    _rs = (
                        msg.meta.get("reasoning_started_at")
                        or msg.meta.get("reasoning_startedAt")
                        or msg.meta.get("thinking_started_at")
                        or msg.meta.get("thinking_startedAt")
                        or msg.created_at
                    )
                    _re = (
                        msg.meta.get("reasoning_ended_at")
                        or msg.meta.get("reasoning_endedAt")
                        or msg.meta.get("thinking_ended_at")
                        or msg.meta.get("thinking_endedAt")
                        or _rs
                    )
                    entry["thinking"] = {"text": reasoning, "started_at": _rs, "ended_at": _re}
                    entry["thinking_text"] = reasoning
                    entry["thinking_blocks"] = [{
                        "text": reasoning,
                        "started_at": _rs,
                        "ended_at": _re,
                    }]
                # [thinking-time 2026-06-01] Pass precise reasoning timing to frontend.
                _rs = (
                    msg.meta.get("reasoning_started_at")
                    or msg.meta.get("reasoning_startedAt")
                    or msg.meta.get("thinking_started_at")
                    or msg.meta.get("thinking_startedAt")
                )
                _re = (
                    msg.meta.get("reasoning_ended_at")
                    or msg.meta.get("reasoning_endedAt")
                    or msg.meta.get("thinking_ended_at")
                    or msg.meta.get("thinking_endedAt")
                )
                if _rs:
                    entry["reasoning_started_at"] = _rs
                if _re:
                    entry["reasoning_ended_at"] = _re
                # [AutoC 2026-06-03] Why: dispatch callback rows store their child
                # navigation target in Message.meta. How: expose only the selected
                # structured keys needed by the web client. Purpose: refreshed history
                # can render the child-session jump button without parsing text.
                _child_sid = str(msg.meta.get("child_session_id") or "").strip()
                if _child_sid:
                    entry["child_session_id"] = _child_sid
                # [AutoC 2026-06-04] Why: dispatch callback metadata was renamed from
                # dispatch_* to explicit child_* fields and gained caller/summary.
                # How: expose the new fields while retaining legacy dispatch_* output
                # when older rows still contain it. Purpose: refreshed web history uses
                # the same structured contract as realtime inbound events.
                _child_tid = str(msg.meta.get("child_task_id") or msg.meta.get("dispatch_task_id") or "").strip()
                if _child_tid:
                    entry["child_task_id"] = _child_tid
                    entry["dispatch_task_id"] = _child_tid
                _child_node_id = str(msg.meta.get("child_node_id") or msg.meta.get("dispatch_node_id") or "").strip()
                if _child_node_id:
                    entry["child_node_id"] = _child_node_id
                    entry["dispatch_node_id"] = _child_node_id
                _caller_node_id = str(msg.meta.get("caller_node_id") or "").strip()
                if _caller_node_id:
                    entry["caller_node_id"] = _caller_node_id
                _dispatch_summary = str(msg.meta.get("summary") or "").strip()
                if _dispatch_summary:
                    entry["summary"] = _dispatch_summary
                # [2026-06-06] Why: preempt messages are stored with meta.preempt=True
                # by the engine preempt handler. How: forward the flag so the frontend
                # can render them with the inline preempt card style during history
                # hydration. Purpose: refreshed pages show preempt messages consistently.
                if msg.meta.get("preempt"):
                    entry["is_preempt"] = True
                _attachments = msg.meta.get("attachments")
                if isinstance(_attachments, list):
                    # [AutoC 2026-06-17] Why: web history hydration cannot infer file
                    # names, MIME types, and paths from plain content reliably. How:
                    # forward the attachment metadata stored by runner/preempt. Purpose:
                    # refreshed inbound messages render the same image/file cards as
                    # realtime WebSocket messages.
                    entry["attachments"] = list(_attachments)
                _provider = str(msg.meta.get("provider") or "").strip()
                if _provider:
                    entry["provider"] = _provider
                _tool_mode = str(msg.meta.get("tool_mode") or "").strip()
                if _tool_mode:
                    entry["tool_mode"] = _tool_mode
                _provider_metadata = msg.meta.get("metadata")
                if isinstance(_provider_metadata, dict):
                    entry["provider_metadata"] = dict(_provider_metadata)
                _usage = msg.meta.get("usage")
                if isinstance(_usage, dict):
                    entry["usage"] = dict(_usage)
                _tool_call_ids = msg.meta.get("tool_call_ids")
                if isinstance(_tool_call_ids, list):
                    entry["tool_call_ids"] = list(_tool_call_ids)
                _raw_inline = str(msg.meta.get("tool_result_raw_inline") or msg.meta.get("raw_inline") or "")
                if _raw_inline:
                    entry["raw_inline"] = _raw_inline
                    entry["tool_result_raw_inline"] = _raw_inline
                _result_format = str(msg.meta.get("tool_result_format") or msg.meta.get("format") or "").strip()
                if _result_format:
                    entry["format"] = _result_format
                    entry["tool_result_format"] = _result_format
                _result_summary = str(msg.meta.get("tool_result_summary") or msg.meta.get("summary") or "").strip()
                if _result_summary:
                    entry["tool_summary"] = _result_summary
                    entry["tool_result_summary"] = _result_summary
                _result_elapsed = msg.meta.get("tool_result_elapsed_ms", msg.meta.get("elapsed_ms"))
                if isinstance(_result_elapsed, (int, float)):
                    entry["elapsed_ms"] = _result_elapsed
                    entry["tool_result_elapsed_ms"] = _result_elapsed
                _tool_result_attachments = msg.meta.get("tool_result_attachments")
                if isinstance(_tool_result_attachments, list):
                    entry["tool_attachments"] = list(_tool_result_attachments)
                else:
                    _structured_atts = _history_attachments_from_value(msg.meta.get("tool_result_structured"))
                    if _structured_atts:
                        entry["tool_attachments"] = _structured_atts
                # [2026-06-07] Forward structured tool rejection metadata.
                # Why: frontend should not parse rejection text. How: expose fields
                # persisted by MessageMeta on tool_result rows. Purpose: UI can hide
                # mixed terminal rejection batches by structure.
                if msg.meta.get("tool_rejected"):
                    entry["tool_rejected"] = True
                _rej_code = str(msg.meta.get("tool_rejection_code") or "").strip()
                if _rej_code:
                    entry["tool_rejection_code"] = _rej_code
                _visibility = str(msg.meta.get("tool_result_visibility") or "").strip()
                if _visibility:
                    entry["tool_result_visibility"] = _visibility
            _legacy_rejection_code = hidden_tool_result_ids.get(str(msg.tool_call_id or ""))
            if _legacy_rejection_code:
                entry["tool_rejected"] = True
                entry["tool_rejection_code"] = _legacy_rejection_code
                entry["tool_result_visibility"] = "hidden"
            # Tool calls (assistant requesting tools)
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            # Tool result fields
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.name:
                entry["tool_name"] = msg.name
            # [AutoC 2026-06-15] Why: tool result content in ConversationStore is
            # the human-readable text produced by result_to_raw, but the frontend's
            # getDataField / getToolResultSuffix needs the original structured dict
            # (with fields like appliedCount, returncode, totalFiles). How: if the
            # engine stored the structured result in meta.tool_result_structured,
            # expose it as tool_result_structured in the history response. Purpose:
            # refreshed history renders the same result suffixes as live WS events.
            if isinstance(msg.meta, dict):
                _trs = msg.meta.get("tool_result_structured")
                if isinstance(_trs, dict):
                    entry["tool_result_structured"] = _trs
            result.append(entry)

        # [2026-06-07] Why: limit truncation drops compact_summary and turn
        # summaries sitting at the top of the JSONL. How: remove server-side
        # limit entirely; frontend handles rendering performance via virtual
        # scrolling. Purpose: all messages including summaries are always
        # returned.
        return result

    # ---- 上下文窗口用量 ----

    def update_context_usage(self, session_id: str, payload: dict[str, Any]) -> None:
        """更新 session 的上下文窗口用量（由 engine 上报）。

        [2026-06-07] 缓存按 (session_id, node_id) 双 key 存储。
        每个 session 上的每个 node 各自维护独立的 usage 条目。
        查询时 get_session_context_usage 聚合所有 resolve 到目标
        session 的条目，选择非 system 节点中 prompt_tokens 最大的。
        这样系统节点的 usage 天然不会覆盖主对话节点的 usage。
        """
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if not session_id or not usage:
            return
        node_id = str(payload.get("node_id") or "")
        cache_key = (session_id, node_id)
        with self._lock:
            self._session_context_usage[cache_key] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                # [AutoC 2026-08-24] 缓存字段透传：provider 层已归一化为
                # cached_prompt_tokens / cache_write_prompt_tokens，此处白名单
                # 存储此前将其丢弃，导致快照响应的 usage 子字典缺缓存字段，
                # 前端仪表的会话级缓存率无法从实时事件播种。
                "cached_prompt_tokens": usage.get("cached_prompt_tokens"),
                "cache_write_prompt_tokens": usage.get("cache_write_prompt_tokens"),
                "node_id": node_id,
                "task_id": str(payload.get("task_id") or ""),
                "updated_at": _now().isoformat(),
            }

    def get_session_context_usage(self, session_id: str) -> dict[str, Any]:
        """获取 session 的上下文窗口用量，包含实际值、估算值和利用率。

        [2026-06-07] 缓存按 (session_id, node_id) 双 key 存储。
        查找策略：遍历所有缓存条目，找到 session_id 直接命中或
        resolve 到 session_id 的条目中，排除 system.* 节点后，
        取 prompt_tokens 最大的作为该 session 的代表 usage。
        """
        from pathlib import Path

        from clonoth_runtime import get_int, load_runtime_config
        from engine.conversation_store import ConversationStore

        with self._lock:
            usage: dict[str, Any] = {}
            best_prompt = -1
            for (sid, nid), entry in self._session_context_usage.items():
                entry_node = str(entry.get("node_id") or nid or "")
                if entry_node.startswith("system."):
                    continue
                if sid == session_id:
                    pt = entry.get("prompt_tokens") or 0
                    if pt > best_prompt:
                        best_prompt = pt
                        usage = dict(entry)
                else:
                    resolved = self._route_session_id_for_session_locked(sid)
                    if resolved == session_id:
                        pt = entry.get("prompt_tokens") or 0
                        if pt > best_prompt:
                            best_prompt = pt
                            usage = dict(entry)

        # [2026-06-06] Read from ConversationStore (persistent JSONL), not EventLog.
        # Priority: 1) in-memory usage from context_usage WS event
        #           2) last assistant message's meta.usage from ConversationStore
        #           3) character-based estimate as last resort
        stored_msgs = []
        try:
            store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            stored_msgs = store.load(session_id)
        except Exception:
            pass

        # Try to recover real prompt_tokens from the last assistant message's meta.usage
        # if in-memory usage is empty (e.g. after restart)
        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is None and stored_msgs:
            for msg in reversed(stored_msgs):
                if msg.role == "assistant" and isinstance(msg.meta, dict):
                    msg_usage = msg.meta.get("usage")
                    if isinstance(msg_usage, dict) and msg_usage.get("prompt_tokens"):
                        prompt_tokens = msg_usage["prompt_tokens"]
                        usage["completion_tokens"] = msg_usage.get("completion_tokens")
                        usage["total_tokens"] = msg_usage.get("total_tokens")
                        # [AutoC 2026-08-24] 缓存字段一并恢复，供快照响应里的
                        # usage 子字典携带，前端仪表重载后缓存率不丢。
                        for _ck in ("cached_prompt_tokens", "cache_write_prompt_tokens"):
                            if msg_usage.get(_ck):
                                usage[_ck] = msg_usage[_ck]
                        break

        msgs = [{"content": m.content, "role": m.role} for m in stored_msgs]
        estimated = self._estimate_tokens_from_messages(msgs)

        runtime_cfg = load_runtime_config(self.workspace_root)
        compact_threshold = get_int(
            runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0,
        )

        source = "llm_usage" if prompt_tokens is not None else "estimate"
        effective_tokens = prompt_tokens if prompt_tokens is not None else estimated
        utilization = (
            round(effective_tokens / compact_threshold, 4)
            if compact_threshold > 0
            else 0.0
        )

        return {
            "session_id": session_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_tokens": estimated,
            "effective_tokens": effective_tokens,
            "source": source,
            "node_id": usage.get("node_id", ""),
            "task_id": usage.get("task_id", ""),
            "compact_threshold": compact_threshold,
            "utilization": utilization,
            "updated_at": usage.get("updated_at"),
            "message_count": len(msgs),
            # [AutoC 2026-08-24] 完整 usage 子字典：前端 updateContextUsage 的
            # 缓存命中率 EMA 读 payload.usage.cached_prompt_tokens；快照路径
            # 此前不携带 usage，重载后缓存率指标消失。
            "usage": dict(usage) if usage else None,
        }

    @staticmethod
    def _estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
        """从消息列表估算 token 数（约 3 字符/token，适用于中英混合内容）。"""
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total_chars += len(part["text"])
        return total_chars // 3
