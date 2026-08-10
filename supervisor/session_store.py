"""Persistent session registry — data/sessions.json.

将 session 信息独立持久化，不再依赖 eventlog 的 session_created 事件回放。
即使 eventlog 因文件轮转或内存截断丢失了 session_created 事件，
session 信息仍可从此文件完整恢复。

文件格式：
{
  "af4fdfc5-...": {
    "session_id": "af4fdfc5-...",
    "channel": "discord_dm",
    "conversation_key": "discord:1491668801836548166",
    "created_at": "2026-04-15T...",
    "reset": false,
    "entry_node_id": ""
  },
  ...
}

写入策略：
- 内存中维护完整 registry 副本，写入时直接序列化内存数据
- 原子写入：先写临时文件，再 os.replace 覆盖目标文件
- 线程安全：所有写入方法由调用方在 SupervisorState._lock 内调用
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ._helpers import SessionInfo, _now

logger = logging.getLogger(__name__)


class SessionStore:
    """管理 data/sessions.json 的读写。

    线程安全：写入方法（on_session_created / on_session_reset / remove_session）
    由调用方在 SupervisorState._lock 内调用，无需自带锁。
    """

    def __init__(self, path: Path):
        self._path = path
        # 内存中保存完整的 raw dict 副本，避免每次写入前重新读文件
        self._registry: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    #  启动时加载
    # ------------------------------------------------------------------ #

    def load(self) -> tuple[dict[str, SessionInfo], dict[str, str], dict[tuple[str, str, str], str], dict[str, set[str]]]:
        """从 sessions.json 加载 session 注册表。

        Returns:
            (sessions, conversation_map, child_session_map, parent_children)
            - sessions: session_id -> SessionInfo（仅活跃 session，不含 reset）
            - conversation_map: conversation_key -> session_id
            - child_session_map: (parent_sid, node_id, context_key) -> child_session_id
            - parent_children: parent_session_id -> set of child_session_ids

        Child Session 隔离（Phase A）：启动时从 is_child=true 的 entry 重建映射。
        如果文件不存在或损坏，优雅降级为空 dict。
        """
        if not self._path.exists():
            logger.info("sessions.json not found; will be created on first session")
            # Child Session 隔离（Phase A）：返回 4-tuple 与签名一致
            return {}, {}, {}, {}

        try:
            raw = self._path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}, {}, {}, {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning(
                    "sessions.json: root is %s, expected dict; skipping",
                    type(data).__name__,
                )
                return {}, {}, {}, {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("sessions.json: load failed (%s); skipping", exc)
            return {}, {}, {}, {}

        sessions: dict[str, SessionInfo] = {}
        conv_map: dict[str, str] = {}
        # Child Session 隔离（Phase A）：重建 child session 映射
        child_map: dict[tuple[str, str, str], str] = {}
        parent_children: dict[str, set[str]] = {}

        for sid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # 无论 reset 与否，都保留到内存 registry（完整镜像）
            self._registry[sid] = entry

            # reset 的 session 不加入活跃集合
            if entry.get("reset"):
                continue

            # Child Session 隔离：重建 child session 映射表
            # [2026-06-06] Why: dispatch sessions created via /v1/inbound before
            # the is_child patch only have parent_session_id but no is_child flag.
            # How: treat any entry with parent_session_id as a child session.
            # Purpose: both old and new dispatch sessions are correctly classified.
            _has_parent = bool(str(entry.get("parent_session_id") or "").strip())
            if entry.get("is_child") or _has_parent:
                parent_sid = str(entry.get("parent_session_id") or "")
                node_id = str(entry.get("node_id") or "")
                ctx_key = str(entry.get("context_key") or "")
                if parent_sid:
                    map_key = (parent_sid, node_id, ctx_key)
                    child_map[map_key] = sid
                    parent_children.setdefault(parent_sid, set()).add(sid)
                # [AutoC 2026-06-09] Why: /v1/inbound dispatch uses
                # get_or_create_session (conversation_map lookup) to find
                # existing agent sessions. After restart, accumulate child
                # sessions were excluded from conversation_map, causing
                # duplicate session creation. How: let accumulate child
                # sessions fall through to sessions/conv_map registration.
                # Purpose: persistent nodes (scout, smith) are singleton
                # after restart.
                _ctx_mode = str(entry.get("context_mode") or "").strip()
                if _ctx_mode != "accumulate":
                    continue  # fresh/fork child sessions 不加入 sessions/conv_map

            try:
                created_str = entry.get("created_at")
                created_at = (
                    datetime.fromisoformat(created_str)
                    if isinstance(created_str, str)
                    else _now()
                )
                updated_str = entry.get("updated_at")
                updated_at = (
                    datetime.fromisoformat(updated_str)
                    if isinstance(updated_str, str) and updated_str
                    else created_at
                )
                info = SessionInfo(
                    session_id=str(entry.get("session_id") or sid),
                    channel=str(entry.get("channel") or ""),
                    conversation_key=str(entry.get("conversation_key") or ""),
                    created_at=created_at,
                    updated_at=updated_at,
                    # Why: older sessions.json files do not have entry_node_id.
                    # How: normalize a missing or empty value to "" during load.
                    # Purpose: schema extension remains backward compatible while
                    # still restoring per-session routing when the field exists.
                    entry_node_id=str(entry.get("entry_node_id") or ""),
                    # [AutoC 2026-06-01] Why: provider_override is a new optional
                    # sessions.json field. How: accept only dict values and copy
                    # them into SessionInfo. Purpose: old rows load as empty
                    # overrides and malformed rows cannot leak non-dict data.
                    provider_override=dict(entry.get("provider_override") or {}) if isinstance(entry.get("provider_override"), dict) else {},
                    workspace=dict(entry.get("workspace") or {}) if isinstance(entry.get("workspace"), dict) else {},
                )
                sessions[info.session_id] = info
                if info.conversation_key:
                    conv_map[info.conversation_key] = info.session_id
            except Exception as exc:
                logger.warning("sessions.json: bad entry %s (%s); skipping", sid, exc)

        logger.info(
            "sessions.json: loaded %d active sessions, %d child sessions (%d total entries)",
            len(sessions),
            len(child_map),
            len(self._registry),
        )
        return sessions, conv_map, child_map, parent_children

    # ------------------------------------------------------------------ #
    #  写入接口（调用方须持有 _lock）
    # ------------------------------------------------------------------ #

    def on_session_created(self, info: SessionInfo) -> None:
        """持久化一个新创建的 session。"""
        self._registry[info.session_id] = {
            "session_id": info.session_id,
            "channel": info.channel,
            "conversation_key": info.conversation_key,
            "created_at": info.created_at.isoformat(),
            "updated_at": info.updated_at.isoformat() if info.updated_at else info.created_at.isoformat(),
            "reset": False,
            # Why: session creation can already know the desired entry node in
            # restored or specialized flows. How: serialize the SessionInfo field
            # beside the existing metadata. Purpose: restart recovery can route
            # inbound callbacks without relying on volatile supervisor memory.
            "entry_node_id": info.entry_node_id,
            # [AutoC 2026-06-01] Why: session-scoped provider selection must
            # survive supervisor restart. How: serialize the SessionInfo override
            # dict beside other session metadata. Purpose: engine can recover the
            # same provider/model choice through the supervisor API after restart.
            "provider_override": dict(info.provider_override or {}),
            # [AutoC 2026-08-10] Why: session workspace must survive restart too.
            # How: serialize {name, path} beside provider_override. Purpose: engine
            # resolves the same active workspace after supervisor restart.
            "workspace": dict(info.workspace or {}),
        }
        self._flush()

    def update_provider_override(self, session_id: str, provider_override: dict[str, Any]) -> None:
        """更新 session 的 provider_override 并落盘。"""
        entry = self._registry.get(session_id)
        if entry is not None:
            # [AutoC 2026-06-01] Why: provider_override changes are requested after
            # session creation through admin APIs. How: replace only this field in
            # the in-memory registry and flush atomically. Purpose: preserve all
            # other session metadata while making the override durable.
            entry["provider_override"] = dict(provider_override or {})
            self._flush()

    def update_workspace(self, session_id: str, workspace: dict[str, Any]) -> None:
        """更新 session 的 workspace 并落盘。"""
        entry = self._registry.get(session_id)
        if entry is not None:
            entry["workspace"] = dict(workspace or {})
            self._flush()

    def update_entry_node(self, session_id: str, entry_node_id: str) -> None:
        """更新 session 的入口节点并落盘。"""
        entry = self._registry.get(session_id)
        if entry is not None:
            # Why: switch_node and first inbound routing can change the effective
            # session entry after the session row was created. How: update only
            # the entry_node_id field on the in-memory registry and flush the
            # complete sessions.json atomically. Purpose: preserve all other
            # session metadata while making the new route survive restarts.
            entry["entry_node_id"] = entry_node_id
            self._flush()

    def on_session_reset(self, session_id: str) -> None:
        """将 session 标记为已重置。"""
        entry = self._registry.get(session_id)
        if entry is not None:
            entry["reset"] = True
            self._flush()

    def remove_session(self, session_id: str) -> None:
        """从 sessions.json 中物理删除一个 session 条目。

        [AutoC 2026-05-30] Why: branch 和 fresh/fork child session 完成后只标记 reset
        不删除，导致 sessions.json 无限膨胀（18000+ 条目）。
        How: 从 registry 中 pop 并 flush。
        Purpose: 立即释放已完成 branch 和已过期 child 的注册记录。
        """
        if session_id in self._registry:
            self._registry.pop(session_id)
            self._flush()

    def on_child_session_created(
        self,
        child_session_id: str,
        parent_session_id: str,
        node_id: str,
        context_key: str,
    ) -> None:
        """持久化一个新创建的 child session。

        Child Session 隔离（Phase A）：将 child session 信息写入 sessions.json，
        包含 is_child、parent_session_id、node_id、context_key 等字段，
        供 load() 时重建 child_session_map。
        """
        now_str = _now().isoformat()
        # Inherit parent's conversation_key so child tasks can resolve
        # the originating channel (e.g. for approval UI routing).
        parent_info = self._registry.get(parent_session_id)
        parent_conv_key = parent_info.get("conversation_key", "") if parent_info else ""
        self._registry[child_session_id] = {
            "session_id": child_session_id,
            "channel": "internal",
            "conversation_key": parent_conv_key,
            "created_at": now_str,
            "reset": False,
            "is_child": True,
            "parent_session_id": parent_session_id,
            "node_id": node_id,
            "context_key": context_key,
            # [AutoC 2026-05-30] Why: stale cleanup must distinguish fresh/fork
            # children from accumulate children. How: persist a placeholder here
            # and let the caller set the concrete context_mode immediately after
            # creation. Purpose: 24h TTL cleanup can skip accumulate sessions.
            "context_mode": "",
            "last_active_at": now_str,
        }
        self._flush()

    def touch_updated_at(self, session_id: str, updated_at: datetime) -> None:
        """Sync updated_at into the registry (no immediate flush).

        [AutoC 2026-06-04] Why: _apply_inbound/outbound_message sets
        si.updated_at in memory, but the registry dict is never refreshed,
        so sessions.json keeps updated_at=None. On restart, load() falls back
        to created_at and sidebar ordering is stuck at creation time.
        How: update the registry entry in memory; periodic _flush persists it.
        Purpose: updated_at survives restarts without per-message disk writes.
        """
        entry = self._registry.get(session_id)
        if entry is not None and not entry.get("is_child") and not entry.get("reset"):
            entry["updated_at"] = updated_at.isoformat()

    def update_last_active(self, child_session_id: str) -> None:
        """更新 child session 的 last_active_at 时间戳。

        Child Session 隔离（Phase A）：在 child session 被 accumulate 模式复用时，
        以及子 task 创建/完成时调用，用于 TTL 过期判定。
        """
        entry = self._registry.get(child_session_id)
        if entry is not None and entry.get("is_child"):
            entry["last_active_at"] = _now().isoformat()
            self._flush()

    # ------------------------------------------------------------------ #
    #  内部：原子写入
    # ------------------------------------------------------------------ #

    def _flush(self) -> None:
        """将内存 registry 原子写入 sessions.json。

        策略：先写临时文件，再 os.replace 覆盖目标文件。
        即使写入过程中进程崩溃，也不会损坏已有文件。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".sessions_",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._registry, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(self._path))
            tmp_path = None  # rename 成功，不需要清理
        except Exception as exc:
            logger.error("sessions.json: atomic write failed: %s", exc)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
