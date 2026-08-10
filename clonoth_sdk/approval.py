"""审批策略逻辑 — 去重与自动放行。

核心职责：
1. 审批 ID 去重（ApprovalTracker）：防止同一审批被多次处理
2. 自动审批（auto_approve）：对内部操作自动放行，带重试

路径分类已移至 supervisor policy 层（trust_level），SDK 不再做二次判断。
"""
from __future__ import annotations

import asyncio

from .client import ClonothClient


class ApprovalTracker:
    """审批去重追踪器。

    维护已处理的审批 ID 集合，防止同一审批被多次处理。
    当集合超过 max_size 时自动清空。
    """

    def __init__(self, max_size: int = 500):
        self._handled: set[str] = set()
        self._max_size = max_size

    def is_handled(self, approval_id: str) -> bool:
        return approval_id in self._handled

    def mark_handled(self, approval_id: str) -> None:
        if len(self._handled) > self._max_size:
            self._handled.clear()
        self._handled.add(approval_id)

    def clear(self) -> None:
        self._handled.clear()

    def __len__(self) -> int:
        return len(self._handled)


async def auto_approve(
    client: ClonothClient,
    approval_id: str,
    *,
    retries: int = 2,
    comment: str = "auto-approved by SDK",
) -> bool:
    """自动放行审批，失败重试。"""
    for attempt in range(retries):
        try:
            ok = await client.approve(
                approval_id, decision="allow", comment=comment,
            )
            if ok:
                return True
        except Exception:
            pass
        if attempt < retries - 1:
            await asyncio.sleep(1)
    return False
