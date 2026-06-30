"""流式输出缓冲。

从 ai_step.py 中拆出。只依赖 RunContext 的 emit_event 接口。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import RunContext


class _StreamBuffer:
    def __init__(self, rctx: "RunContext", node_id: str, kind: str, *, request_id: str = "") -> None:
        self._rctx = rctx
        self._node_id = node_id
        self._kind = kind
        # [AutoC 2026-06-04] Why: buffered stream chunks may flush after other task
        # bookkeeping has happened. How: capture the request id when the buffer is
        # created and emit it with every chunk. Purpose: stream cards are keyed by the
        # provider request that produced them, not by the wider task.
        self._request_id = request_id
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self.flushed_any = False
        # [thinking-time 2026-06-01] Record wall-clock time of first token arrival
        # for precise thinking duration calculation.
        self.first_push_at: float | None = None  # time.monotonic()
        self._first_push_wall: float | None = None  # time.time()

    async def push(self, chunk: str) -> None:
        if self.first_push_at is None:
            self.first_push_at = time.monotonic()
            self._first_push_wall = time.time()
        self._buf.append(chunk)
        now = time.monotonic()
        buf_len = sum(len(s) for s in self._buf)
        if now - self._last_flush >= 0.15 or buf_len >= 60:
            await self.flush()

    @property
    def is_empty(self) -> bool:
        """缓冲区是否为空（无待 flush 内容）。"""
        return not self._buf

    @property
    def first_push_iso(self) -> str | None:
        """首次 push 的 wall-clock 时间，ISO 8601 UTC 格式。"""
        if self._first_push_wall is None:
            return None
        import datetime
        return datetime.datetime.fromtimestamp(
            self._first_push_wall, tz=datetime.timezone.utc
        ).isoformat()

    async def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()
        self.flushed_any = True
        await self._rctx.emit_event("stream_delta", {
            "node_id": self._node_id,
            "task_id": self._rctx.task_id,
            "llm_request_id": self._request_id,
            "type": self._kind,
            "content": text,
        })
