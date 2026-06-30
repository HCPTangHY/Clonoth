"""Clonoth SDK — 纯协议层，封装 Clonoth Supervisor 协议逻辑。

Phase 1 (2026-04-17): 初始包骨架，包含 ClonothClient + 基础类型 + 审批策略。
Phase 2 (2026-04-17): 新增 SessionState + 状态数据类。
Phase 3 (2026-04-17): 新增 EventRouter + AdapterCallbacks，事件轮询主循环与适配器回调接口。

SDK 边界（参见 data/sdk_refactor_plan_final.md 一、核心原则）：
  进 SDK：
    - ClonothClient — Supervisor HTTP API 通信
    - EventRouter — 事件轮询 + 协议分发
    - AdapterCallbacks — 适配器回调接口（Protocol）
    - SessionState — session/trigger/watermark 状态管理
    - 审批策略 — 去重、路径分类、自动放行
    - BotConfig — 配置注入
  不进 SDK：
    - [SPLIT] 消息分段、[REACT:xxx] 表情提取、[BOT_RESTART] 信号
    - TextProcessor / 协议标记清理（Bot 自定义标记部分）
    - Discord / Telegram 等平台库依赖

使用方式::

    import sys
    sys.path.insert(0, '/www/wwwroot/Clonoth')
    from clonoth_sdk import ClonothClient, EventRouter, SessionState, BotConfig

    client = ClonothClient("http://127.0.0.1:8765")
    config = BotConfig(base_url="http://127.0.0.1:8765", entry_node_id="my_bot")
    state = SessionState()
    router = EventRouter(client, state, my_callbacks, config)
    await router.run()
"""

from .client import ClonothClient
from .config import BotConfig
from .types import Event, HealthInfo, InboundResult, OpenAIConfig, RunningTask
from .approval import ApprovalTracker, auto_approve, is_external_operation
# Phase 2 (2026-04-17): 新增 SessionState + 状态数据类
from .state import (
    ChildTaskState,
    MainTaskState,
    SessionState,
    TriggerInfo,
)
# Phase 3 (2026-04-17): 新增 EventRouter 事件轮询主循环 + AdapterCallbacks 适配器回调接口
from .callbacks import AdapterCallbacks
from .event_router import EventRouter, strip_protocol_markers

__all__ = [
    # 核心客户端
    "ClonothClient",
    # 事件路由 (Phase 3)
    "EventRouter",
    "AdapterCallbacks",
    # 配置
    "BotConfig",
    # 数据类型
    "Event",
    "HealthInfo",
    "InboundResult",
    "OpenAIConfig",
    "RunningTask",
    # 状态管理 (Phase 2)
    "SessionState",
    "TriggerInfo",
    "MainTaskState",
    "ChildTaskState",

    # 审批
    "ApprovalTracker",
    "auto_approve",
    "is_external_operation",
]
