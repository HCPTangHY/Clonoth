"""Task-level structured records — the P0 foundation for Dream, Extractor, and Compactor.

ECS-style: TaskRecord is a pure Entity index (no content duplication).
Full message chain lives in data/conversations/{session_id}.jsonl,
tagged by source_task_id. This record is the lightweight pointer + aggregates.

Storage: data/transcripts/{session_id}.jsonl
Each line is one TaskRecord serialized as JSON.

Created: 2026-04-24 — P0 Task 内核化
Revised: 2026-04-25 — 精简为纯索引+指针，干掉内容副本
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# [AutoC 2026-05-13] Why: L2 snip summaries need one shared marker after the
# L3 pure-concatenation rollback. How: keep only the marker used by snip_history
# and remove the aggregate-specific constants/helpers. Purpose: avoid retaining
# a broken summary-merge path while preserving existing L2 behavior.
_SNIP_MARKER = "[Task summary — original messages snipped]"


@dataclass
class TaskRecord:
    """Lightweight index for a single task execution.

    Points into data/conversations/{session_id}.jsonl via message IDs.
    Time is derived from the pointed messages' created_at field.
    Content is retrieved by filtering conversations on source_task_id.
    """
    task_id: str
    session_id: str
    node_id: str
    action: str = ""                # finish / fail / cancelled / preempted / dispatch
    first_message_id: str = ""      # -> Message.id of first message in this task
    last_message_id: str = ""       # -> Message.id of last message in this task
    step_count: int = 0
    tool_call_count: int = 0
    token_usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens, total_tokens}
    summary: str = ""               # TaskAction.summary
    error: str = ""                 # error message if failed
    child_session_id: str = ""      # if this was a child session task
    compressed: bool = False         # marked True when task messages replaced by summary

    def to_dict(self) -> dict:
        """Serialize, omitting empty/default fields to reduce JSONL size."""
        d: dict = {"task_id": self.task_id, "session_id": self.session_id, "node_id": self.node_id}
        if self.action:
            d["action"] = self.action
        if self.first_message_id:
            d["first_message_id"] = self.first_message_id
        if self.last_message_id:
            d["last_message_id"] = self.last_message_id
        if self.step_count:
            d["step_count"] = self.step_count
        if self.tool_call_count:
            d["tool_call_count"] = self.tool_call_count
        if self.token_usage:
            d["token_usage"] = self.token_usage
        if self.summary:
            d["summary"] = self.summary
        if self.error:
            d["error"] = self.error
        if self.child_session_id:
            d["child_session_id"] = self.child_session_id
        if self.compressed:
            d["compressed"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        """Deserialize from a JSON dict. Missing fields get defaults."""
        return cls(
            task_id=data.get("task_id", ""),
            session_id=data.get("session_id", ""),
            node_id=data.get("node_id", ""),
            action=data.get("action", ""),
            first_message_id=data.get("first_message_id", ""),
            last_message_id=data.get("last_message_id", ""),
            step_count=data.get("step_count", 0),
            tool_call_count=data.get("tool_call_count", 0),
            token_usage=data.get("token_usage", {}),
            summary=data.get("summary", ""),
            error=data.get("error", ""),
            child_session_id=data.get("child_session_id", ""),
            compressed=data.get("compressed", False),
        )


def write_task_record(ws_root: Path, record: TaskRecord) -> None:
    """Append a TaskRecord to data/transcripts/{session_id}.jsonl."""
    transcript_dir = ws_root / "data" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{record.session_id}.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("Failed to write task record %s: %s", record.task_id, e)


def snip_history(
    messages: list[dict],
    records: list["TaskRecord"],
    *,
    keep_recent_tasks: int = 3,
    max_snip: int = 2,
) -> tuple[list[dict], int, set[str]]:
    """Replace old task message chains with their summaries (incremental).

    Each compaction trigger replaces at most *max_snip* oldest eligible tasks.
    The caller should treat any snip_count > 0 as "compaction done for this
    round" and skip LLM compaction.  Only when snip_count == 0 (all eligible
    tasks already snipped) should the caller fall through to LLM compaction.

    Args:
        messages: History dicts (from ConversationStore → _message_to_history_dict).
        records: TaskRecords for this session.
        keep_recent_tasks: Number of most recent tasks to keep unsnipped.
        max_snip: Maximum number of tasks to snip per invocation.

    Returns:
        (snipped_messages, snip_count, snipped_task_ids)
    """
    if not records or not messages:
        return messages, 0, set()

    # Build lookup: task_id → summary (only for tasks that have one)
    summaries: dict[str, str] = {}
    for r in records:
        if r.summary and r.task_id:
            summaries[r.task_id] = r.summary

    if not summaries:
        return messages, 0, set()

    # Identify task_ids to keep (most recent N by order of appearance in records)
    recent_task_ids: set[str] = set()
    for r in records[-keep_recent_tasks:]:
        recent_task_ids.add(r.task_id)

    # Collect eligible task_ids in order of appearance (oldest first)
    # Also exclude tasks that have already been snipped (content starts with marker)
    already_snipped: set[str] = set()
    for msg in messages:
        content = msg.get("content", "")
        meta = msg.get("_meta", {})
        if not isinstance(meta, dict):
            meta = {}

        # 场景1：显式标记为已摘要的消息块
        if isinstance(content, str) and content.startswith(_SNIP_MARKER):
            tid = str(meta.get("source_task_id") or "")
            if tid:
                already_snipped.add(tid)

        # [AutoC 2026-05-13] 场景2：任意压缩摘要块继承的 task id。
        # 原因：LLM 压缩摘要会覆盖多条旧 task 消息，单条消息上的 source_task_id
        # 会消失。做法：统一读取 meta.compressed_task_ids。目的：L2 后续扫描不会
        # 重复处理已经被摘要覆盖的 task。
        c_tids = meta.get("compressed_task_ids")
        if isinstance(c_tids, list):
            for _ctid in c_tids:
                already_snipped.add(str(_ctid))

    # Collect task_ids that actually have messages in this message list.
    # Child tasks (child_coder, system.compactor, etc.) write their messages
    # to child session JSONLs, not the main session — their task_ids won't
    # appear in messages, so snipping them would waste max_snip slots and
    # always fall through to LLM compaction.
    present_task_ids: set[str] = set()
    for msg in messages:
        _pmeta = msg.get("_meta", {})
        if isinstance(_pmeta, dict):
            _ptid = str(_pmeta.get("source_task_id") or "")
            if _ptid:
                present_task_ids.add(_ptid)

    eligible_ordered: list[str] = []
    _seen_elig: set[str] = set()
    for r in records:
        tid = r.task_id
        if (tid and tid in summaries
                and tid not in recent_task_ids
                and tid not in already_snipped
                and tid not in _seen_elig
                and tid in present_task_ids):
            eligible_ordered.append(tid)
            _seen_elig.add(tid)

    if not eligible_ordered:
        return messages, 0, set()

    # Incremental: snip at most max_snip oldest tasks
    to_snip = eligible_ordered[:max_snip]
    to_snip_set = set(to_snip)

    result: list[dict] = []
    seen_snipped: set[str] = set()
    for msg in messages:
        meta = msg.get("_meta", {})
        msg_tid = ""
        if isinstance(meta, dict):
            msg_tid = str(meta.get("source_task_id") or "")
        if msg_tid in to_snip_set:
            if msg_tid not in seen_snipped:
                seen_snipped.add(msg_tid)
                result.append({
                    "role": "user",
                    "content": f"[Task summary \u2014 original messages snipped]\n{summaries[msg_tid]}",
                    "_meta": {"source_task_id": msg_tid},
                })
        else:
            result.append(msg)

    snip_count = len(seen_snipped)
    if snip_count:
        log.info("snip_history: incremental snip %d/%d eligible tasks, %d → %d messages",
                 snip_count, len(eligible_ordered), len(messages), len(result))

    return result, snip_count, seen_snipped


def snip_store(
    store_messages: list,
    records: list["TaskRecord"],
    snipped_task_ids: set[str],
) -> list:
    """Apply snip to ConversationStore Message list for persistence.

    Mirrors the replacement done by snip_history but operates on
    ConversationStore Message objects so the result can be written
    back via replace_all.
    """
    from engine.conversation_store import Message
    from uuid import uuid4

    summaries = {r.task_id: r.summary for r in records if r.summary and r.task_id}
    result = []
    seen: set[str] = set()
    for msg in store_messages:
        tid = msg.source_task_id or ""
        if tid in snipped_task_ids:
            if tid not in seen:
                seen.add(tid)
                result.append(Message(
                    id=str(uuid4()),
                    role="user",
                    content=f"[Task summary — original messages snipped]\n{summaries.get(tid, '')}",
                    message_type="summary",
                    source_task_id=tid,
                ))
        else:
            result.append(msg)
    return result


def load_task_records(ws_root: Path, session_id: str) -> list[TaskRecord]:
    """Load all TaskRecords for a session. Returns empty list if file missing."""
    path = ws_root / "data" / "transcripts" / f"{session_id}.jsonl"
    records: list[TaskRecord] = []
    if not path.exists():
        return records
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(TaskRecord.from_dict(json.loads(line)))
                    except Exception:
                        pass
    except Exception as e:
        log.error("Failed to load task records for %s: %s", session_id, e)
    return records
