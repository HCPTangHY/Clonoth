"""审批策略逻辑 — 去重与自动放行分类。

Phase 1 (2026-04-17): 初始创建，从 bot_adapter.py 审批处理逻辑中提取。

核心职责：
1. 审批 ID 去重（ApprovalTracker）：防止同一审批被多次处理
   提取自 bot_adapter.py 全局 _handled_approval_ids 集合
2. 路径分类（classify_path / is_external_operation）：
   判断审批操作目标是否在工作区外部
   提取自 bot_adapter.py 内联的 clonoth_runtime.classify_path
3. 自动审批（auto_approve）：对内部操作自动放行，带重试
   提取自 bot_adapter.py _auto_approve()
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .client import ClonothClient


class ApprovalTracker:
    """审批去重追踪器。

    维护已处理的审批 ID 集合，防止同一审批被 outbound_poller 多次处理。
    提取自 bot_adapter.py 的全局 _handled_approval_ids 集合。
    当集合超过 max_size 时自动清空，与原始行为一致（L1290 清理逻辑）。
    """

    def __init__(self, max_size: int = 500):
        self._handled: set[str] = set()
        self._max_size = max_size

    def is_handled(self, approval_id: str) -> bool:
        """检查审批是否已被处理过。"""
        return approval_id in self._handled

    def mark_handled(self, approval_id: str) -> None:
        """标记审批为已处理。超过上限时清空集合防止无限增长。"""
        if len(self._handled) > self._max_size:
            self._handled.clear()
        self._handled.add(approval_id)

    def clear(self) -> None:
        """手动清空去重集合。"""
        self._handled.clear()

    def __len__(self) -> int:
        return len(self._handled)


def classify_path(
    workspace_root: Path,
    extra_roots: list[Path],
    path_str: str,
) -> tuple[Path | None, str, bool]:
    """解析并分类文件系统路径。

    提取自 bot_adapter.py 内联的 clonoth_runtime.classify_path (L58-82)。
    判断给定路径是在工作区/信任路径内（内部），还是在外部。

    Args:
        workspace_root: Clonoth 工作区根目录
        extra_roots: 信任的外部根路径列表（从 policy.yaml extra_roots 加载）
        path_str: 待分类的路径字符串

    Returns:
        (resolved_path, display_path, is_external) 三元组：
        - resolved_path: 解析后的绝对路径；路径无效时为 None
        - display_path: 用于展示的路径字符串
        - is_external: True 表示路径在工作区和信任路径之外
    """
    try:
        raw = Path(path_str)
        p = raw.resolve() if raw.is_absolute() else (workspace_root / path_str).resolve()
    except Exception as e:
        return None, f"invalid path: {e}", False

    ws = workspace_root.resolve()
    # 检查是否在工作区内
    try:
        rel = p.relative_to(ws)
        return p, rel.as_posix(), False
    except ValueError:
        pass
    # 检查是否在信任的外部路径内
    for r in extra_roots:
        try:
            p.relative_to(r.resolve())
            return p, p.as_posix(), False
        except ValueError:
            continue
    # 绝对路径且不在任何信任区域内 → 外部路径
    if raw.is_absolute():
        return p, p.as_posix(), True
    # 相对路径解析后逃逸出工作区 → 视为外部路径，需要人工审批
    # fix: 原返回 False（内部）不正确，逃逸工作区的路径应归类为外部（True）
    return None, "path escapes workspace root", True


def is_external_operation(
    details: dict[str, Any],
    workspace_root: Path,
    extra_roots: list[Path],
) -> bool:
    """判断审批操作是否指向工作区外部路径。

    提取自 bot_adapter.py _is_external_operation() (L605-612)。
    外部路径的操作（如写入 /etc 下的文件）需要人工审批，
    工作区内部路径可自动放行。

    Args:
        details: 审批详情 dict（来自 approval_requested 事件 payload.details）
        workspace_root: Clonoth 工作区根目录
        extra_roots: 信任的外部根路径列表

    Returns:
        True 表示操作目标在工作区外部，需要人工审批
    """
    path_str = details.get("path", "")
    if not path_str:
        return False
    _, _, is_ext = classify_path(workspace_root, extra_roots, path_str)
    return is_ext


async def auto_approve(
    client: ClonothClient,
    approval_id: str,
    *,
    retries: int = 2,
    comment: str = "auto-approved by SDK",
) -> bool:
    """自动放行审批，失败重试。

    提取自 bot_adapter.py _auto_approve() (L928-942)。
    对判定为内部操作的审批自动提交 allow 决策。

    Args:
        client: ClonothClient 实例
        approval_id: 审批请求 ID
        retries: 最大尝试次数（含首次）
        comment: 审批附加说明

    Returns:
        True 表示审批成功放行
    """
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
