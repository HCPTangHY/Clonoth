"""Clonoth OneBot 11 适配器配置。

所有敏感/实例特定的参数通过环境变量注入，避免硬编码。
"""
from __future__ import annotations

import os

# Clonoth Supervisor API 地址
CLONOTH_BASE_URL = os.environ.get("CLONOTH_BASE_URL", "http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）
CLONOTH_WORKSPACE = os.environ.get("CLONOTH_WORKSPACE", "/www/wwwroot/Clonoth")

# 入口节点 ID
ENTRY_NODE_ID = os.environ.get("CLONOTH_ENTRY_NODE", "main")

# QQ 自定义表情名称索引文件路径（可选）
BQBS_PATH = os.environ.get("CLONOTH_BQBS_PATH", "")

# 允许接入的群号列表（逗号分隔）；空值表示全部群均可接入
_raw_groups = os.environ.get("CLONOTH_ALLOWED_GROUPS", "")
ALLOWED_GROUPS: list[int] = [int(g) for g in _raw_groups.split(",") if g.strip()]
