from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEM_SESSION_ID = "__system__"
_MAX_MEMORY_EVENTS = 5000
_MAX_MEMORY_EVENT_BYTES = 64 * 1024 * 1024
_TAIL_READ_BLOCK_BYTES = 1024 * 1024


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
        # [WS events 2026-05-17] Why: WebSocket clients need live updates while
        # EventLog remains the durable source of truth. How: keep per-session
        # asyncio.Queue subscribers beside the append-only memory buffer. Purpose:
        # append() can fan out each new event without changing persistence or the
        # existing polling API.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # [WS events 2026-05-19] Why: the web client needs an all-session stream
        # in addition to the existing per-session stream. How: keep a separate
        # subscriber list that append() fans out to after the session-specific
        # queues. Purpose: add global observation without changing session
        # isolation for subscribe()/unsubscribe().
        self._global_subscribers: list[asyncio.Queue] = []

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
        recent_events: list[dict[str, Any]] = []
        # Why: ClonothZX can have hundreds of megabytes of historical events,
        # including old windows with very large task snapshots. How: read only a
        # bounded tail of the JSONL file, then parse that tail. Purpose: avoid the
        # startup RSS spike caused by parsing every old line or by sliding a
        # 5000-event window across historical large snapshots.
        for line in self._read_recent_lines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            recent_events.append(evt)
            seq = evt.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq

        self._seq = max_seq
        self._events = recent_events[-_MAX_MEMORY_EVENTS:]

    def _read_recent_lines(self) -> list[str]:
        """Return raw JSONL lines from the bounded file tail."""
        chunks: list[bytes] = []
        total_read = 0
        newline_count = 0
        with self._path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            while pos > 0 and newline_count <= _MAX_MEMORY_EVENTS and total_read < _MAX_MEMORY_EVENT_BYTES:
                read_size = min(_TAIL_READ_BLOCK_BYTES, pos, _MAX_MEMORY_EVENT_BYTES - total_read)
                if read_size <= 0:
                    break
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                chunks.append(chunk)
                total_read += len(chunk)
                newline_count += chunk.count(b"\n")

            # Why: if the bounded tail starts in the middle of a line, parsing it
            # would create a spurious JSON error and could displace a real event.
            # How: check the byte just before the buffer; if it is not a newline,
            # drop the first split line. Purpose: keep only complete JSONL rows.
            starts_with_complete_line = pos == 0
            if pos > 0:
                f.seek(pos - 1)
                starts_with_complete_line = f.read(1) == b"\n"

        data = b"".join(reversed(chunks))
        raw_lines = data.splitlines()
        if not starts_with_complete_line and raw_lines:
            raw_lines = raw_lines[1:]
        raw_lines = raw_lines[-_MAX_MEMORY_EVENTS:]
        return [line.decode("utf-8", errors="replace") for line in raw_lines]

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
            # Trim with hysteresis to prevent unbounded memory growth
            if len(self._events) > _MAX_MEMORY_EVENTS + 500:
                self._events = self._events[-_MAX_MEMORY_EVENTS:]
            # [WS events 2026-05-17] Why: broadcasting while holding the threading
            # lock would make subscriber callbacks part of the critical section.
            # How: copy the current session queues and fan out after leaving the
            # lock. Purpose: keep append() fast and avoid awaiting under this lock.
            subscribers = list(self._subscribers.get(session_id, []))
            # [WS events 2026-05-19] Why: /v1/ws must see every EventLog row.
            # How: snapshot global subscribers under the same lock used for
            # per-session subscribers, then fan out outside the critical section.
            # Purpose: avoid races with subscribe_global()/unsubscribe_global()
            # while preserving the existing append() lock behavior.
            global_subscribers = list(self._global_subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                # The current queues are unbounded, but this keeps future bounded
                # queues from breaking event persistence if a client falls behind.
                continue
        for queue in global_subscribers:
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                # [WS events 2026-05-19] Why: global observers are optional
                # consumers and must not block persistence. How: mirror the
                # per-session overflow behavior. Purpose: a slow global client
                # cannot affect EventLog writes or session-specific streams.
                continue
        return evt

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Subscribe to new events for one session.

        [WS events 2026-05-17] Why: the WebSocket endpoint needs a live channel
        but EventLog must remain the only event source. How: callers receive an
        asyncio.Queue that append() fills with matching session events. Purpose:
        consumers can combine catch-up reads with live delivery.
        """
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        """Remove a queue previously returned by subscribe()."""
        # [WS events 2026-05-17] Why: disconnected WebSocket clients must not
        # keep receiving events or retain memory. How: remove the exact queue and
        # drop the session list when it becomes empty. Purpose: make cleanup
        # deterministic even when multiple clients watch the same session.
        with self._lock:
            queues = self._subscribers.get(session_id)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                return
            if not queues:
                self._subscribers.pop(session_id, None)

    def subscribe_global(self) -> asyncio.Queue:
        """Subscribe to all new events across all sessions."""
        queue: asyncio.Queue = asyncio.Queue()
        # [WS events 2026-05-19] Why: global subscribers must not be mixed into
        # the per-session mapping. How: append their queues to a dedicated list.
        # Purpose: unsubscribe_global() can clean up without knowing a session id.
        with self._lock:
            self._global_subscribers.append(queue)
        return queue

    def unsubscribe_global(self, queue: asyncio.Queue) -> None:
        """Remove a queue previously returned by subscribe_global()."""
        # [WS events 2026-05-19] Why: disconnected global WebSocket clients should
        # not retain queues. How: remove the exact queue if it is still present.
        # Purpose: keep cleanup deterministic and independent of session streams.
        with self._lock:
            try:
                self._global_subscribers.remove(queue)
            except ValueError:
                pass

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

    def last_boot_run_id(self) -> str | None:
        # 从尾部倒序找到最近一次 boot 事件
        for e in reversed(self._events):
            if e.get("type") == "boot":
                return e.get("run_id")
        return None
