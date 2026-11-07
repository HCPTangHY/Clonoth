"""Session-level conversation store.

Phase 0 + Phase 1 of the Session Conversation Store design.
Provides a typed Message model and a JSONL-backed ConversationStore
that serves as the session-level single source of truth for conversation history.

See: data/session_conversation_store_design.md
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
#  MessageType: 消息细分类型常量
#  与 LLM API 的 role 分离，用于去重、剥离、格式转换等内部逻辑。
#  例如 role=user + message_type=tool_result 表示工具结果而非真实用户输入。
# ---------------------------------------------------------------------------

class MessageType:
    USER_INPUT = "user_input"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    SUMMARY = "summary"
    DYNAMIC_CONTEXT = "dynamic_context"


def is_dynamic_context(message: Any) -> bool:
    """Recognize persisted and in-memory dynamic context without relying on role."""
    if isinstance(message, dict):
        meta = message.get("_meta") or message.get("meta") or {}
        meta = meta if isinstance(meta, dict) else {}
        return bool(
            message.get("_dynamic")
            or message.get("message_type") == MessageType.DYNAMIC_CONTEXT
            or meta.get("message_type") == MessageType.DYNAMIC_CONTEXT
            or meta.get("_dynamic")
        )
    meta = getattr(message, "meta", None)
    return bool(
        getattr(message, "message_type", "") == MessageType.DYNAMIC_CONTEXT
        or (isinstance(meta, dict) and meta.get("_dynamic"))
    )


# ---------------------------------------------------------------------------
#  Message: 标准化消息对象，替代裸 dict
#  所有消息（user/assistant/system/tool_result）统一用此类表示。
#  to_dict / from_dict 实现 JSONL 序列化/反序列化。
# ---------------------------------------------------------------------------

@dataclass
class Message:
    id: str                          # UUID，全局唯一
    # [2026-05-01] role 增加 tool。
    # 原因：真 native formatter 会生成 role=tool 消息；存储层必须原样保存，
    # 目的：下一轮恢复历史时仍能满足原生工具调用的 tool_call_id 配对要求。
    role: str                        # user / assistant / system / tool
    # [AutoC 2026-06-01] Why: task-local image retention requires persisted
    # user messages to keep provider-style multimodal blocks. How: allow either
    # plain text or a list of content-part dictionaries. Purpose: JSONL history
    # can round-trip images for the active task while runner strips them for
    # cross-task reloads.
    content: str | list[dict[str, Any]]  # 消息文本内容或多模态内容块
    message_type: str = ""           # MessageType 常量，细分类型
    created_at: str = ""             # ISO 8601 时间戳
    meta: dict[str, Any] = field(default_factory=dict)  # 元数据（provider, tool_mode 等），用 dict 存储不依赖 MessageMeta 类
    source_node_id: str = ""         # 产生该消息的节点 ID
    source_task_id: str = ""         # 产生该消息的 task ID
    ephemeral: bool = False          # 临时消息（dynamic context 等），不持久化到 store
    tool_calls: list = field(default_factory=list)  # assistant 消息的工具调用列表
    # [2026-05-01] 新增 role=tool 的原生工具结果字段。
    # 怎么改：把 tool_call_id 与 name 作为 Message 的一等字段参与 JSONL 序列化。
    # 目的：ConversationStore 读取后可以还原 OpenAI/Responses 等原生工具历史。
    tool_call_id: str = ""           # role=tool 消息关联的 assistant.tool_calls[].id
    name: str = ""                   # role=tool 消息关联的工具名
    # Host-only metadata; not assistant content or provider replay state.
    response_info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为存储格式。仅包含非空字段以减小 JSONL 体积。"""
        # [AutoC 2026-06-01] Why: list content is already JSON-serializable and
        # must not be coerced to text before storage. How: assign self.content
        # unchanged to the dict. Purpose: from_dict can restore multimodal user
        # input for same-task history reconstruction.
        d: dict[str, Any] = {"id": self.id, "role": self.role, "content": self.content}
        if self.message_type:
            d["message_type"] = self.message_type
        if self.created_at:
            d["created_at"] = self.created_at
        if self.meta:
            d["meta"] = self.meta
        if self.source_node_id:
            d["source_node_id"] = self.source_node_id
        if self.source_task_id:
            d["source_task_id"] = self.source_task_id
        if self.ephemeral:
            d["ephemeral"] = True
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        # [2026-05-01] 持久化原生 role=tool 结果的配对字段。
        # 原因：只保存 role/content 会丢失 tool_call_id，下一轮发送给 LLM 时无法配对。
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        if self.response_info:
            d["response_info"] = self.response_info
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """从 JSONL 行反序列化。对缺失字段提供安全默认值。"""
        return cls(
            id=data.get("id", ""),
            role=data.get("role", "user"),
            # [AutoC 2026-06-01] Why: stored multimodal content must survive a
            # JSONL reload. How: pass through str or list content unchanged;
            # invalid/missing content still falls back to an empty string via
            # data.get. Purpose: Message remains compatible with legacy text
            # records and new attachment records.
            content=data.get("content", ""),
            message_type=data.get("message_type", ""),
            created_at=data.get("created_at", ""),
            meta=data.get("meta", {}),
            source_node_id=data.get("source_node_id", ""),
            source_task_id=data.get("source_task_id", ""),
            ephemeral=data.get("ephemeral", False),
            tool_calls=data.get("tool_calls", []),
            # [2026-05-01] 反序列化 role=tool 的配对字段。
            # 目的：从 JSONL 恢复的历史与运行时内存消息保持同一结构。
            tool_call_id=str(data.get("tool_call_id") or ""),
            name=str(data.get("name") or ""),
            response_info=dict(data["response_info"]) if isinstance(data.get("response_info"), dict) else {},
        )


# ---------------------------------------------------------------------------
#  ConversationStore: Session 级别的 JSONL 对话存储
#  每个 session 一个 .jsonl 文件，追加写入，不覆盖。
#  带内存缓存，cache miss 时从文件加载。
# ---------------------------------------------------------------------------

class ConversationStore:
    """Session-level conversation store backed by JSONL files.

    Storage layout:  {data_dir}/{session_id}.jsonl
    Each line is one Message serialized as JSON.

    Cache model: one bounded LRU per data_dir, shared by every instance in this
    process, validated by the file's stat signature. A cached entry is reused
    only while that signature still matches the file on disk. Large files are
    read in full but are not retained in the cache.

    Isolation rules:
    - Every successful write drops the cached entry. Blindly extending a cached
      list would lose messages another writer appended between our read and our
      write, and would also cache ephemeral rows.
    - load() always returns a deep copy, so a caller editing content/meta in
      place cannot pollute the shared cache used by other callers.
    - The cache is only populated when the file signature did not change while
      it was being read, so a mid-read rewrite never becomes cached truth.
    """

    _MAX_CACHED_SESSIONS = 16
    _MAX_CACHEABLE_FILE_BYTES = 5 * 1024 * 1024
    _SHARED_CACHES: dict[str, dict[str, list[Message]]] = {}
    # (st_mtime_ns, st_size, st_ino): the inode is part of the signature because
    # an atomic replace can produce a same-size file with an unchanged mtime.
    _SHARED_CACHE_SIGS: dict[str, dict[str, tuple[int, int, int]]] = {}

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        # 确保存储目录存在
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            cache_key = str(self._data_dir.resolve())
        except OSError:
            cache_key = str(self._data_dir)
        # session_id -> list[Message] 的进程内共享 LRU（按 data_dir 分桶，配合文件 stat 校验跨实例/跨进程一致性）
        self._cache: dict[str, list[Message]] = self._SHARED_CACHES.setdefault(cache_key, {})
        self._cache_sigs: dict[str, tuple[int, int, int]] = self._SHARED_CACHE_SIGS.setdefault(cache_key, {})

    def _path(self, session_id: str) -> Path:
        return self._data_dir / f"{session_id}.jsonl"

    def _file_sig(self, path: Path) -> tuple[int, int, int] | None:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _drop_cache(self, session_id: str) -> None:
        self._cache.pop(session_id, None)
        self._cache_sigs.pop(session_id, None)

    def _put_cache(
        self, session_id: str, messages: list[Message], sig: tuple[int, int, int] | None = None,
    ) -> None:
        self._drop_cache(session_id)
        # Bound retained memory without truncating the history returned by load().
        if sig is not None and sig[1] > self._MAX_CACHEABLE_FILE_BYTES:
            return
        while len(self._cache) >= self._MAX_CACHED_SESSIONS:
            evicted = next(iter(self._cache))
            self._drop_cache(evicted)
        self._cache[session_id] = messages
        if sig is not None:
            self._cache_sigs[session_id] = sig

    def _get_cache(self, session_id: str, sig: tuple[int, int, int]) -> list[Message] | None:
        """Return the cached list only when it still matches *sig*; else None."""
        if self._cache_sigs.get(session_id) != sig or session_id not in self._cache:
            return None
        msgs = self._cache.pop(session_id)
        self._cache[session_id] = msgs  # refresh LRU position
        return msgs

    @staticmethod
    def _serialize(messages: list[Message]) -> str:
        """Serialize messages to JSONL text before touching the file.

        Why: serialization errors must not leave a half-written file behind, and
        ephemeral rows must never reach disk. How: build the whole payload first.
        Purpose: a failed write leaves the previous file content (and the cache)
        untouched.
        """
        return "".join(
            json.dumps(msg.to_dict(), ensure_ascii=False) + "\n"
            for msg in messages
            if not msg.ephemeral
        )

    # ── 写入 ──

    def append(self, session_id: str, message: Message) -> None:
        """追加一条消息到 session 的 JSONL 文件。

        ephemeral 消息跳过写入（不持久化）。
        写入成功后一律失效缓存：其他进程/实例可能在本进程 stat 与 write 之间追加，
        盲目把缓存列表延长并更新签名会把那条消息永久丢掉。运行期消息本身已有
        LoopState 内存副本，重读代价可接受。
        """
        if message.ephemeral:
            return
        path = self._path(session_id)
        payload = self._serialize([message])
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload)
        self._drop_cache(session_id)

    def append_batch(self, session_id: str, messages: list[Message]) -> None:
        """批量追加（一次文件 I/O）。写入后使缓存失效以保证一致性。"""
        path = self._path(session_id)
        payload = self._serialize(messages)
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload)
        self._drop_cache(session_id)

    # ── 读取 ──

    def load(self, session_id: str) -> list[Message]:
        """加载 session 的全部消息（始终是完整历史）。

        Why: 压缩/合并必须看到完整历史，按文件尾部截断会静默丢掉前部消息，让压缩
        误判旧 segment 仍存在。How: 缓存签名与文件 stat 一致时复用缓存，否则整
        文件重读；读取后再校验签名，中途被改写的内容不入缓存。Purpose: 返回值既
        完整又与共享缓存隔离（深拷贝），调用方原地改 content/meta 不影响别人。
        """
        path = self._path(session_id)
        sig = self._file_sig(path)
        if sig is None:
            self._drop_cache(session_id)
            return []
        cached = self._get_cache(session_id, sig)
        if cached is not None:
            return copy.deepcopy(cached)
        messages: list[Message] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(Message.from_dict(json.loads(line)))
                    except Exception:
                        pass  # 跳过损坏行，不中断加载
        if self._file_sig(path) == sig:
            self._put_cache(session_id, messages, sig=sig)
        else:
            # 读取期间文件被改写 → 这份内容不稳定，不能作为缓存事实
            self._drop_cache(session_id)
        return copy.deepcopy(messages)

    # ── 清理 ──

    def delete(self, session_id: str) -> None:
        """删除 session 的对话存储文件和缓存。用于 context_reset reason=clear。"""
        path = self._path(session_id)
        if path.exists():
            path.unlink()
        self._drop_cache(session_id)

    def replace_all(self, session_id: str, messages: list[Message]) -> None:
        """原子替换 session 的全部消息。

        Step 2（2026-04-16）修复 compact：主节点/child session 切到 ConversationStore
        后，压缩需要用 summary 消息替换旧消息，而不是追加。
        [2026-10-31] Why: delete + append_batch 会在中途失败时把整个会话历史丢光。
        How: 先在同一目录写临时文件并 fsync，再 os.replace 原子替换；失败则删除临时
        文件并原样保留旧文件与缓存。Purpose: 压缩结果要么整体生效，要么完全不变。
        写入成功后失效缓存（含 ephemeral 过滤后的内容以磁盘为准）。
        """
        path = self._path(session_id)
        payload = self._serialize(messages)
        if not payload:
            # 与原 delete + append_batch 语义一致：空列表 = 无历史
            self.delete(session_id)
            return
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._data_dir), prefix=f".{session_id}.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._drop_cache(session_id)

    def remove_messages(self, session_id: str, message_ids: set[str]) -> int:
        """Remove selected rows atomically, preserving all other bytes, including malformed rows.

        Like replace_all, callers must serialize mutations of the same session.
        This streams the complete file instead of rebuilding it from parsed history.
        """
        path = self._path(session_id)
        if not message_ids or not path.exists():
            return 0
        fd, temporary = tempfile.mkstemp(
            dir=str(self._data_dir), prefix=f".{session_id}.", suffix=".tmp",
        )
        removed = 0
        try:
            with os.fdopen(fd, "wb") as dest, path.open("rb") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        selected = isinstance(row, dict) and row.get("id") in message_ids
                    except (ValueError, TypeError):
                        selected = False
                    if selected:
                        removed += 1
                    else:
                        dest.write(line)
                dest.flush()
                os.fsync(dest.fileno())
            if removed:
                os.replace(temporary, path)
                self._drop_cache(session_id)
            else:
                os.unlink(temporary)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return removed

    def update_message(self, session_id: str, msg_id: str, new_data: dict) -> bool:
        """按 id 替换 session 中的一条消息。

        [AutoC 2026-06-18] Why: output chain reject 需要把原 finish "ok" result
        更新为 reject reason。How: load → 找到 msg_id → 替换 → replace_all 写回。
        Purpose: JSONL 中的 finish result 能正确反映 reject 状态。
        返回 True 表示替换成功，False 表示未找到。
        """
        stored = self.load(session_id)
        for i, m in enumerate(stored):
            if m.id == msg_id:
                if isinstance(new_data, dict):
                    # Merge old message's durable fields into new_data so
                    # the replacement doesn't lose id/timestamps/source info.
                    merged = dict(new_data)
                    merged.setdefault("id", m.id)
                    merged.setdefault("created_at", m.created_at)
                    merged.setdefault("source_node_id", m.source_node_id)
                    merged.setdefault("source_task_id", m.source_task_id)
                    merged.setdefault("message_type", m.message_type)
                    merged.setdefault("response_info", m.response_info)
                    # Runtime _meta → storage meta
                    if "_meta" in merged and "meta" not in merged:
                        merged["meta"] = merged.pop("_meta")
                    stored[i] = Message.from_dict(merged)
                else:
                    stored[i] = new_data
                self.replace_all(session_id, stored)
                return True
        return False

    # ── 查询 ──

    def message_count(self, session_id: str) -> int:
        """返回 session 中的消息总数。"""
        return len(self.load(session_id))

    def fork(self, source_session_id: str, target_session_id: str) -> int:
        """将 source session 的非 system 消息复制到 target session。

        用于 context_mode=fork 场景：子节点从父 session 复制历史后独立演化。
        target session 必须是空的（新创建的），已有内容则跳过，防止重复复制。
        返回实际复制的消息数量。
        """
        if self.message_count(target_session_id) > 0:
            return 0  # target 已有内容，不重复复制
        source_msgs = self.load(source_session_id)
        if not source_msgs:
            return 0
        # 过滤 system 消息——子节点有自己的 system prompt，不需要父节点的
        non_system = [m for m in source_msgs if m.role != "system" and not is_dynamic_context(m)]
        if non_system:
            self.append_batch(target_session_id, non_system)
        return len(non_system)

    def invalidate_cache(self, session_id: str) -> None:
        """手动使缓存失效，下次 load 时强制从文件重新读取。"""
        self._drop_cache(session_id)
