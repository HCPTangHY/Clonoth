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
    suspended = "suspended"
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
    # [AutoC 2026-06-18] Optional direct target session for web operators.
    # Why: the System tab can browse all sessions, including internal child sessions
    # whose conversation_key may point back to a parent. How: allow an authenticated
    # /v1/inbound caller to target an existing session_id directly. Purpose: temporary
    # session entry can send into the selected session without creating a sidebar row.
    session_id: str | None = None
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True
    entry_node_id: str | None = None
    # [2026-05-28] 异步 dispatch 统一走 inbound：新增 3 个可选字段。
    # 为什么：异步子节点委派改为 POST /v1/inbound，需要在 payload 中携带回调元数据。
    # dispatch_origin: 回调目标信息（parent_session_id, caller_node_id）
    # dispatch_context_mode: 上下文模式（accumulate/fresh/fork）
    # dispatch_fork_from_session: fork 模式的源 session ID
    dispatch_origin: dict[str, Any] | None = None
    dispatch_context_mode: str | None = None
    dispatch_fork_from_session: str | None = None
    # [AutoC 2026-07-05] Why: fork/merge per inbound is unnecessary overhead for
    # serial conversation channels (web, DM) where only one message is in flight
    # at a time. How: let callers opt out of entry branch isolation. When False,
    # the task runs directly on the main session without fork or merge. Purpose:
    # reduce I/O (full history copy per message) and eliminate branch cleanup for
    # channels that never produce concurrent inbounds.
    use_branch: bool | None = None
    # [2026-06-16] Why: welcome page model selection had no way to reach the
    # session before the first task started. How: pass the override through the
    # inbound request so the supervisor applies it immediately after session
    # creation, before the entry task is dispatched. Purpose: the model chosen on
    # the welcome page takes effect on the very first response, with no race.
    provider_override: dict[str, Any] | None = None


class InboundMessageOut(BaseModel):
    session_id: str
    inbound_seq: int = 0
    accepted: bool = True


class OutboundMessageIn(BaseModel):
    text: str
    source_inbound_seq: int | None = None
    attachments: list[dict[str, Any]] | None = None
    # [AutoC 2026-06-04] Why: manual outbound API calls may also be used to finalize
    # an existing live request card. How: accept the same request-level id that engine
    # task results carry. Purpose: the API contract remains consistent across outbound
    # producers.
    llm_request_id: str | None = None


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
    entry_node_id: str | None = None


class InboundAckIn(BaseModel):
    worker_id: str


class InboundAckOut(BaseModel):
    ok: bool = True


class Task(BaseModel):
    task_id: str
    session_id: str
    session_generation: int = 1
    kind: TaskKind
    node_id: str | None = None
    tool_name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    source_inbound_seq: int | None = None
    caller_task_id: str | None = None
    batch_id: str | None = None
    batch_index: int = 0
    waiting_for_task_id: str | None = None
    status: TaskStatus = TaskStatus.pending
    cancel_requested: bool = False
    preempt_requested: bool = False
    preempted_context_ref: str = ""
    preempt_message: str = ""
    preempt_attachments: list = Field(default_factory=list)
    worker_id: str | None = None
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    # [AutoC 2026-06-08] Why: output chain 需要在多个同步下游 task 之间保留入口和原始调用方。
    # How: 在 Task 快照中持久化 entry/source/path/depth 元数据，旧快照缺字段时由默认值补齐。
    # Purpose: Supervisor 能检测循环和深度，并在 __source__ 处把最终结果穿透回原始 caller。
    output_chain_entry_task_id: str | None = None
    output_chain_source_task_id: str | None = None
    output_chain_source_session_id: str | None = None
    output_chain_source_dispatch_origin: dict[str, Any] = Field(default_factory=dict)
    output_chain_path: list[str] = Field(default_factory=list)
    output_chain_depth: int = 0
    # [AutoC 2026-06-04] Runtime-only task activity tracking. Not persisted.
    # Updated by supervisor when transient events (stream_delta, tool_call_*,
    # approval_*) arrive. Read by GET /v1/admin/tasks/active.
    current_phase: str = ""
    current_detail: str = ""
    # [AutoC 2026-06-09] Why: updated_at is refreshed by renew_lease heartbeat
    # (every ~60s), making it useless for detecting "alive but stuck" tasks.
    # How: dedicated field only updated by real production events (stream,
    # tool calls), never by heartbeat. Purpose: compare last_activity_at vs
    # lease_expires_at to distinguish "producing" from "stuck".
    last_activity_at: datetime | None = None


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
    # [AutoC 2026-05-31] Why: approval prompts now render inside the matching
    # ToolCallCard instead of a standalone block. How: accept optional tool and
    # execution identity fields from callers that know them. Purpose: legacy callers
    # remain valid while new callers can anchor approvals to tool executions.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


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
    # [AutoC 2026-05-31] Why: the frontend needs to merge approval state into the
    # existing ToolExecution card. How: include the provider tool_call_id and the
    # execution origin in the approval event payload. Purpose: approval_requested
    # and approval_decided can update the correct tool card instead of creating an
    # independent ApprovalBlock.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


class OpRequestIn(BaseModel):
    session_id: str
    op: Literal["read_file", "write_file", "execute_command", "restart"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    # [AutoC 2026-05-31] Why: policy approvals are requested while a tool is
    # executing. How: carry optional tool_call_id/node_id/task_id through the ops
    # request. Purpose: create_approval can emit enough identity data for the web
    # reducer to update ToolCallCard in place.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


class OpRequestOut(BaseModel):
    safety_level: SafetyLevel
    reason: str
    approval_id: str | None = None


class RestartIn(BaseModel):
    target: Literal["engine", "all"]
    reason: str | None = None
    approval_id: str | None = None
    session_id: str | None = None


class RestartOut(BaseModel):
    scheduled: bool
    target: str


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    workspace_root: str = ""
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
    # [2026-06-07] Why: the frontend Header needs to resolve the default model
    # for any provider (not just the active one) when a node sets provider but
    # leaves model empty. How: include a mapping of provider name → model.
    # Purpose: displayModel can show the correct provider-specific model.
    provider_models: dict[str, str] = Field(default_factory=dict)


class ProviderConfigPublic(BaseModel):
    """单个渠道的公开配置（api_key 脱敏）"""
    base_url: str = ""
    model: str = ""
    api_key_present: bool = False
    api_key_redacted: str = ""


class ProvidersResponse(BaseModel):
    """所有渠道配置响应"""
    active_provider: str = "openai"
    providers: dict[str, ProviderConfigPublic] = Field(default_factory=dict)
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)


class ProviderUpdateIn(BaseModel):
    """更新渠道请求（部分更新）"""
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ActiveProviderIn(BaseModel):
    """切换活跃渠道"""
    provider: str


class FallbacksUpdateIn(BaseModel):
    """更新 fallback 链"""
    fallbacks: list[dict[str, Any]]


class ConfigReloadOut(BaseModel):
    ok: bool = True
    config: AppConfigPublic


class RemoteToolInfo(BaseModel):
    # [AutoC 2026-08-01] REST shape for tools provided by remote workers.
    # Why: engine-side registry will discover remote tools through Supervisor HTTP
    # rather than through the worker WebSocket. How: expose the same core fields as
    # local tools plus a remote metadata block. Purpose: remote tools can be loaded
    # into ToolRegistry without changing model-visible schemas later.
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: float | None = None
    remote: dict[str, Any] = Field(default_factory=dict)


class RemoteToolsOut(BaseModel):
    ok: bool = True
    tools: list[RemoteToolInfo] = Field(default_factory=list)


class RemoteCallCreateIn(BaseModel):
    registered_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: float | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class RemoteCallCreateOut(BaseModel):
    ok: bool = True
    call_id: str
    worker_id: str
    status: str


class RemoteCallCancelIn(BaseModel):
    reason: str = "task cancelled"


class RemoteCallCancelOut(BaseModel):
    ok: bool = True
    call_id: str
    status: str


class RemoteCallResultOut(BaseModel):
    ok: bool = True
    call_id: str | None = None
    worker_id: str | None = None
    status: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    cancelled: bool = False
    elapsed_ms: float | None = None


class RemoteWorkerInfo(BaseModel):
    connection_id: str
    worker_id: str
    worker_name: str = ""
    namespace: str = ""
    connected_at: datetime
    last_seen_at: datetime
    max_concurrency: int = 1
    running_calls: int = 0
    load: float | None = None
    tools: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    stale: bool = False
