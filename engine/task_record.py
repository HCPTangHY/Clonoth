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
