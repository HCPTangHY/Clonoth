from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SafetyLevel(str, Enum):
    auto = "auto"
    approval_required = "approval_required"
    deny = "deny"


class ApprovalStatus(str, Enum):
    pending = "pending"
    allowed = "allowed"
    denied = "denied"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TaskKind(str, Enum):
    node = "node"
    tool = "tool"


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


class InboundMessageIn(BaseModel):
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True
    workflow_id: str | None = None


class InboundMessageOut(BaseModel):
    session_id: str
    accepted: bool = True


class OutboundMessageIn(BaseModel):
    text: str
    source_inbound_seq: int | None = None
    attachments: list[dict[str, Any]] | None = None


class OutboundMessageOut(BaseModel):
    ok: bool = True


class InboundWorkItem(BaseModel):
    inbound_seq: int
    session_id: str
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True
    workflow_id: str | None = None


class InboundAckIn(BaseModel):
    worker_id: str


class InboundAckOut(BaseModel):
    ok: bool = True


class Task(BaseModel):
    task_id: str
    session_id: str
    session_generation: int = 1
    workflow_id: str
    kind: TaskKind
    node_id: str | None = None
    tool_name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    source_inbound_seq: int | None = None
    parent_task_id: str | None = None
    status: TaskStatus = TaskStatus.pending
    cancel_requested: bool = False
    worker_id: str | None = None
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class TaskCompleteIn(BaseModel):
    worker_id: str
    result: dict[str, Any] = Field(default_factory=dict)


class SessionEventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


HandoffEventIn = SessionEventIn


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
    target: Literal["engine", "all"]
    reason: str | None = None
    approval_id: str | None = None


class RestartOut(BaseModel):
    scheduled: bool
    target: str


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    started_at: datetime
    uptime_seconds: float


class AdminStateOut(BaseModel):
    sessions: int
    approvals: dict[str, int]
    tasks: dict[str, int] = Field(default_factory=dict)
    pending_approvals: list[Approval] = Field(default_factory=list)
    engine_runtime: dict[str, Any] = Field(default_factory=dict)


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
