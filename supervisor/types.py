from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SafetyLevel(str, Enum):
    auto = "auto"
    approval_required = "approval_required"
    deny = "deny"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class ApprovalStatus(str, Enum):
    pending = "pending"
    allowed = "allowed"
    denied = "denied"


class Event(BaseModel):
    schema_version: int = 1
    seq: int
    event_id: str
    ts: datetime
    run_id: str
    session_id: str
    component: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class InboundMessageIn(BaseModel):
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    # Whether Shell Orchestrator should include conversation history when routing this message.
    # External channel adapters can set this to false to make the orchestrator stateless.
    use_context: bool = True


class InboundMessageOut(BaseModel):
    session_id: str
    accepted: bool = True


class OutboundMessageIn(BaseModel):
    """Append an outbound assistant message into a session.

    Used by Shell Orchestrator when it decides to answer directly (without creating a task).
    """

    text: str
    # Optional idempotency key: when provided, Supervisor will dedupe outbound replies
    # produced for the same inbound message.
    source_inbound_seq: int | None = None

    # Optional idempotency key: when provided, Supervisor will dedupe outbound messages
    # produced as the *final response* for a completed Kernel task.
    #
    # This is used by Shell (chat AI) to post-process Kernel task results and then
    # send exactly-once user-facing replies.
    source_task_id: str | None = None


class OutboundMessageOut(BaseModel):
    ok: bool = True


# ----------------------------
# Inbound routing queue (Shell Orchestrator Worker)
# ----------------------------


class InboundWorkItem(BaseModel):
    """A pending inbound message to be processed by Shell Orchestrator."""

    inbound_seq: int
    session_id: str

    # Original inbound payload
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True


class InboundAckIn(BaseModel):
    worker_id: str


class InboundAckOut(BaseModel):
    ok: bool = True


class CreateTaskIn(BaseModel):
    session_id: str
    instruction: str
    workflow_id: str | None = None
    priority: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    # Optional idempotency key: when provided, Supervisor will dedupe task creation
    # for the same inbound message.
    source_inbound_seq: int | None = None
    use_context: bool = True


class Task(BaseModel):
    task_id: str
    session_id: str
    instruction: str
    workflow_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    source_inbound_seq: int | None = None
    use_context: bool = True
    status: TaskStatus
    priority: int = 0
    created_at: datetime
    updated_at: datetime
    assigned_to: str | None = None
    result: dict[str, Any] | None = None


class TaskEventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskCompleteIn(BaseModel):
    status: TaskStatus = TaskStatus.done
    result: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequestIn(BaseModel):
    session_id: str
    operation: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionIn(BaseModel):
    decision: Literal["allow", "deny"]
    comment: str | None = None


class Approval(BaseModel):
    approval_id: str
    session_id: str
    operation: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus
    fingerprint: str
    requested_at: datetime
    decided_at: datetime | None = None
    decision: Literal["allow", "deny"] | None = None
    comment: str | None = None


class OpRequestIn(BaseModel):
    session_id: str
    op: Literal["read_file", "write_file", "execute_command", "restart"]
    parameters: dict[str, Any] = Field(default_factory=dict)


class OpRequestOut(BaseModel):
    safety_level: SafetyLevel
    reason: str
    approval_id: str | None = None


class RestartIn(BaseModel):
    target: Literal["shell", "kernel", "all"]
    reason: str | None = None
    approval_id: str | None = None


class RestartOut(BaseModel):
    scheduled: bool
    target: Literal["shell", "kernel", "all"]


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    started_at: datetime
    uptime_seconds: float


class AdminStateOut(BaseModel):
    sessions: int
    tasks: dict[str, int]
    approvals: dict[str, int]
    pending_approvals: list[Approval] = Field(default_factory=list)


# ----------------------------
# Config models (YAML-backed)
# ----------------------------


class OpenAIConfigSecret(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"


class OpenAIConfigUpdateIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class OpenAIConfigPublic(BaseModel):
    base_url: str
    model: str
    api_key_present: bool
    api_key: str


class AppConfigSecret(BaseModel):
    version: int = 1
    provider: str = "openai"
    openai: OpenAIConfigSecret = Field(default_factory=OpenAIConfigSecret)


class AppConfigPublic(BaseModel):
    version: int = 1
    provider: str = "openai"
    openai: OpenAIConfigPublic


class ConfigReloadOut(BaseModel):
    ok: bool = True
    config: AppConfigPublic
