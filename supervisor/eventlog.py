from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEM_SESSION_ID = "__system__"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventAppendResult:
    event: dict[str, Any]


class EventLog:
    """Append-only JSONL event log.

    - 一行一个 JSON（UTF-8）
    - 维护 seq 单调递增（用于轮询 after_seq）
    - 进程内缓存 events，用于快速查询
    """

    def __init__(self, path: Path, run_id: str):
        self._path = path
        self._run_id = run_id
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq: int = 0

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    @property
    def path(self) -> Path:
        return self._path

    def _load_existing(self) -> None:
        if not self._path.exists():
            return

        max_seq = 0
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._events.append(evt)
                seq = evt.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq

        self._seq = max_seq

    def append(
        self,
        *,
        session_id: str,
        component: str,
        type_: str,
        payload: dict[str, Any] | None = None,
        transient: bool = False,
    ) -> dict[str, Any]:
        payload = payload or {}

        with self._lock:
            self._seq += 1
            seq = self._seq

            evt = {
                "schema_version": 1,
                "seq": seq,
                "event_id": str(uuid.uuid4()),
                "ts": _now().isoformat(),
                "run_id": self._run_id,
                "session_id": session_id,
                "component": component,
                "type": type_,
                "payload": payload,
            }

            if not transient:
                line = json.dumps(evt, ensure_ascii=False)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")

            self._events.append(evt)
            return evt

    def list_events(self, *, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        # 简单线性过滤（MVP）。后续可按 session 建索引或 snapshot。
        return [
            e
            for e in self._events
            if e.get("session_id") == session_id and int(e.get("seq", 0)) > after_seq
        ]

    def list_all_events(self, *, after_seq: int = 0) -> list[dict[str, Any]]:
        """返回所有 session 中 seq > after_seq 的事件。"""
        return [
            e for e in self._events
            if int(e.get("seq", 0)) > after_seq
        ]

    def last_event(self) -> dict[str, Any] | None:
        return self._events[-1] if self._events else None

    def last_boot_run_id(self) -> str | None:
        # 从尾部倒序找到最近一次 boot 事件
        for e in reversed(self._events):
            if e.get("type") == "boot":
                return e.get("run_id")
        return None
