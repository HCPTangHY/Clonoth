from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from pathlib import Path
from typing import Any

import httpx

# 桥接信号系统：让所有 emit_event 调用自动走 SignalBus
from engine.signals import Signal, get_bus


@dataclass
class RunContext:
    """单个 task 执行片段的运行上下文。"""

    workspace_root: Path
    supervisor_url: str
    session_id: str
    worker_id: str
    http: httpx.AsyncClient
    llm_http: httpx.AsyncClient
    # [Fork/Merge 2026-05-12] parent_session_id separates event routing from runtime storage.
    # Why: entry tasks now run on a branch session, but SDK adapters know the
    # user-facing conversation_key through the parent session. How: keep
    # session_id as the ConversationStore/runtime session and use this optional
    # parent for supervisor event POSTs. Purpose: branch history stays isolated
    # while events still reach the channel-level SDK route.
    parent_session_id: str = ""
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-4o-mini"
    user_text: str = ""
    task_id: str = ""
    session_generation: int = 0
    source_inbound_seq: int | None = None
    task_context: dict = field(default_factory=dict)
    # Child Session 隔离：dispatch 子节点使用独立 session ID。
    # 非空时，_shadow_write 和 ConversationStore 操作写入此 session 而非 parent session_id。
    # 主节点和无 child session 的场景下为空字符串。
    child_session_id: str = ""
    # P0 Task 内核化：task 级数据采集字段
    tool_call_log: list = field(default_factory=list)   # [{name: str, args_summary: str}]
    total_usage: dict = field(default_factory=dict)      # accumulated {prompt_tokens, completion_tokens, total_tokens}
    first_shadow_message_id: str = ""   # UUID of first message written to ConversationStore
    last_shadow_message_id: str = ""    # UUID of last message written to ConversationStore
    completed_steps: int = 0             # actual step count from ai_step loop
    # [AutoC 2026-06-04] Why: one visible streaming card must map to one concrete LLM
    # request, not to the whole task. How: keep the current request id on RunContext
    # and let emit_event copy it into stream/tool/outbound-related payloads. Purpose:
    # frontend reducers can replace the exact card that produced the request.
    current_llm_request_id: str = ""
    llm_request_index: int = 0
    # [AutoC 2026-08-10] Session-level workspace override for file/command tools.
    # Set by set_workspace tool; persists across LLM rounds within the same task.
    workspace: Path | None = None

    def begin_llm_request(self) -> str:
        """Start a new request-scoped id for one provider call."""
        # [AutoC 2026-06-04] Why: task_id is too coarse because one task can make
        # several provider requests. How: combine task id, a monotonic per-task counter,
        # and a short uuid suffix. Purpose: every stream card has a stable backend key
        # for final replacement without depending on event order heuristics.
        self.llm_request_index += 1
        base = self.task_id or self.session_id or "llm"
        self.current_llm_request_id = f"{base}:llm:{self.llm_request_index}:{uuid.uuid4().hex[:8]}"
        return self.current_llm_request_id

    @property
    def is_system_task(self) -> bool:
        """Whether this task was created as a system-internal task."""
        return bool((self.task_context or {}).get("is_system_task"))

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.source_inbound_seq is not None:
            payload.setdefault("source_inbound_seq", self.source_inbound_seq)
        # [AutoC 2026-06-15] Why: frontend needs is_system_task on every event
        # to filter background task rendering events out of the chat stream.
        # How: inject the flag from task_context into the payload. Purpose:
        # metadata-driven isolation without string heuristics on node_id.
        if self.is_system_task:
            payload.setdefault("is_system_task", True)
        if self.current_llm_request_id:
            # [AutoC 2026-06-04] Why: realtime stream/tool events and the later
            # outbound_message must share a request-level identity. How: attach the
            # current LLM request id unless a caller supplied a more specific value.
            # Purpose: frontend replacement targets the exact streaming card.
            payload.setdefault("llm_request_id", self.current_llm_request_id)
        # [Fork/Merge 2026-05-12] Events are delivered through the parent session
        # when this task is running on an entry branch. Why: SDK session maps are
        # keyed by the user-facing parent session, while self.session_id remains
        # the branch used for ConversationStore reads/writes. How: post to
        # parent_session_id when present and annotate the payload with both IDs.
        # Purpose: preserve exact SDK routing without writing branch events to an
        # unmapped internal session.
        route_session_id = self.parent_session_id or self.session_id
        if self.parent_session_id and self.parent_session_id != self.session_id:
            payload.setdefault("parent_session_id", self.parent_session_id)
            payload.setdefault("branch_session_id", self.session_id)
        # Bridge emit_event → SignalBus，用 dict(payload) 复制避免后续修改影响信号订阅方
        get_bus().emit(Signal(name=event_type, payload=dict(payload)))
        try:
            await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{route_session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception:
            pass

    async def check_cancelled(self) -> bool:
        # [硬取消] 显式 2s 超时，防止 supervisor 无响应时拖延 cancel 检测。
        # 工具执行和 LLM 流式轮询每 0.2-0.3s 调用一次 check_cancelled，
        # 如果 HTTP 请求无超时限制，单次调用可能阻塞数十秒，导致 cancel 响应延迟。
        # 超时后 except 兜底返回 False，下次轮询再试。
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.session_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def check_preempted(self) -> dict:
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempted"
                )
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {"preempted": False, "message": "", "attachments": []}

    async def consume_preempt(self) -> None:
        """通知 supervisor 已消费 preempt message，防止重复注入。"""
        try:
            if self.task_id:
                await self.http.post(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempt_consumed"
                )
        except Exception:
            pass
