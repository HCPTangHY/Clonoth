"""Clonoth SDK 数据类型定义。

Phase 1 (2026-04-17): 初始创建，从 bot_adapter.py 协议交互中提取。
包含所有与 Supervisor HTTP API 交互所需的数据结构。
使用 dataclass 而非 pydantic，减少外部依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundResult:
    """POST /v1/inbound 的返回结果。

    提取自 bot_adapter.py _submit_inbound() 返回的 (session_id, inbound_seq) 元组，
    增加 accepted 字段与 Supervisor 响应保持一致。
    """
    session_id: str
    inbound_seq: int
    accepted: bool = True


@dataclass
class Event:
    """Supervisor 事件流中的单个事件。

    对应 GET /v1/events 和 WS /v1/ws 返回的事件对象。
    ts 保留为 ISO 格式字符串，调用方可按需用 datetime.fromisoformat() 解析。
    """
    seq: int
    event_id: str
    ts: str
    run_id: str
    session_id: str
    component: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    # [SDK WS 2026-05-19] Why: the Supervisor event schema includes a version
    # field on both HTTP and WebSocket rows. How: keep it as a defaulted trailing
    # dataclass field so existing positional construction remains compatible.
    # Purpose: raw-event hooks can inspect the schema version when needed.
    schema_version: int = 1

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        """从 API JSON 响应字典构造 Event 实例。

        对字段做防御性类型转换，避免 Supervisor 返回意外类型时崩溃。
        """
        return cls(
            seq=int(d.get("seq", 0)),
            event_id=str(d.get("event_id", "")),
            ts=str(d.get("ts", "")),
            run_id=str(d.get("run_id", "")),
            session_id=str(d.get("session_id", "")),
            component=str(d.get("component", "")),
            type=str(d.get("type", "")),
            payload=dict(d.get("payload") or {}),
            schema_version=int(d.get("schema_version", 1) or 1),
        )


@dataclass
class RunningTask:
    """会话中活跃任务的信息。

    对应 GET /v1/sessions/{sid}/running_tasks 返回的单个任务对象。
    is_user_entry 标识此任务是否由用户 inbound 直接创建（非子节点/非异步dispatch）。
    """
    task_id: str
    node_id: str
    status: str
    created_at: str
    caller_task_id: str
    is_user_entry: bool
    source_inbound_seq: int | None = None


@dataclass
class HealthInfo:
    """Supervisor 健康状态信息。

    对应 GET /v1/health 返回。workspace_root 可用于启动时动态获取工作区路径。
    """
    status: str
    run_id: str
    workspace_root: str
    started_at: str
    uptime_seconds: float


@dataclass
class OpenAIConfig:
    """OpenAI 兼容 API 的公开配置信息。

    对应 GET /v1/config/openai 返回。api_key 字段为脱敏后的值。
    """
    base_url: str
    model: str
    api_key_present: bool
    api_key: str = ""
