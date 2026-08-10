"""Clonoth SDK 配置。

Phase 1 (2026-04-17): 初始创建。
BotConfig 以 dataclass 注入方式提供 Bot 接入 Clonoth 所需的配置项。
设计理念：配置注入优于继承，避免基类锁定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BotConfig:
    """Bot 接入 Clonoth 所需的配置。

    Attributes:
        base_url: Supervisor HTTP API 地址，如 "http://127.0.0.1:8765"
        secret: 认证密钥（保留字段，当前 Supervisor 未启用认证）
        entry_node_id: 默认入口节点 ID，如 "my_main_node"
        conversation_key_prefix: 会话键前缀，如 "discord" 使生成的 key 为 "discord:{channel_id}"
        poll_interval: 事件轮询间隔（秒）
        max_history: 频道历史队列最大长度
        auto_approve_internal: 工作区内操作自动放行，基于 supervisor trust_level
    """
    base_url: str
    secret: str | None = None
    entry_node_id: str = ""
    conversation_key_prefix: str = ""
    poll_interval: float = 0.8
    max_history: int = 50
    auto_approve_internal: bool = False
