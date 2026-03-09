from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NodeOutcome:
    """一个 AI 节点的执行结果。"""

    node_id: str
    outcome: str  # "reply" | "completed" | "failed"
    text: str = ""
    instruction: str = ""
    summary: str = ""
    context_ref: str = ""  # 节点局部上下文持久化路径
