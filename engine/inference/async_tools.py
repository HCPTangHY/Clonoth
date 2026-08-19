"""异步工具生命周期：启动、跟踪、结果投递。

[AutoC 2026-08-18] 从 ai_step.py 抽出。这组函数构成一个自洽的子主题：
当工具 spec 标记 async_mode=True，或 execute_command 超过自适应阈值时，
工具在后台 asyncio.Task 中执行，完成后通过 supervisor 的
/v1/sessions/{id}/async_tool_result 接口将结果注入回对话流。

模块级字典 _async_tool_tasks 是异步工具状态的唯一存储，ai_step 与
本模块共享同一引用（import 的是同一对象，直接原地修改）。
"""

from __future__ import annotations

import asyncio  # noqa: F401  (仅用于类型注解 "asyncio.Task")
import json
import logging
import time
from dataclasses import replace
from typing import Any

from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

from clonoth_runtime import get_bool, get_float

from ..signals import get_bus
from ..tool_step import (
    artifact_enabled,
    get_tool_inline_limit,
    result_to_raw,
    summarize_result,
    truncate_tool_result,
    write_artifact,
)
from .loop_state import _short

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  异步工具跟踪表（Async Tool Tracking）
#  [2026-04-23] 从 commit 7d10197 恢复，在 864a333 大扫除中被误删。
#  key = async_tool_id (8 位 hex), value = 状态字典
#  用于关联异步工具的启动占位消息与 preempt 回传结果。
#  done/failed 条目保留 5 分钟后自动清理，防止无限增长。
# ---------------------------------------------------------------------------
_async_tool_tasks: dict[str, dict] = {}

# 清理阈值：done/failed 条目保留秒数
_ASYNC_TRACK_RETAIN_SEC = 300  # 5 minutes


def _cleanup_async_tracker() -> None:
    """清理已完成超过 _ASYNC_TRACK_RETAIN_SEC 的条目。

    在每次新增 tracking 条目时调用，避免 map 无限增长。
    只清理 status 为 done 或 failed 且 finished_at 已过期的条目。
    """
    now = time.monotonic()
    expired = [
        k for k, v in _async_tool_tasks.items()
        if v.get("status") in ("done", "failed")
        and now - v.get("finished_at", now) > _ASYNC_TRACK_RETAIN_SEC
    ]
    for k in expired:
        del _async_tool_tasks[k]


def get_async_tool_tasks() -> list[dict]:
    """导出当前所有异步工具跟踪条目，供外部查询。

    返回列表，每项包含 async_id, tool_name, status, elapsed 等字段。
    """
    result = []
    now = time.monotonic()
    for aid, info in _async_tool_tasks.items():
        entry = {"async_id": aid, **info}
        # 对 running 状态补算已经过的时间
        if info.get("status") == "running" and "started_at" in info:
            entry["elapsed"] = round(now - info["started_at"], 1)
        result.append(entry)
    return result


def _snapshot_tool_context(tool_ctx: ToolContext) -> ToolContext:
    """Return an immutable-enough ToolContext snapshot for background tool work."""
    # [AutoC 2026-06-27] Why: _execute_real_tools reuses one ToolContext and rewrites
    # tool_call_id for every tool in the batch. How: copy dataclass fields and the
    # node-specific dynamic attributes before scheduling background work. Purpose:
    # async execute_command callbacks keep the approval and artifact identity of the
    # original tool call.
    snapshot = replace(tool_ctx)
    for attr in ("_node_id", "_node_extra"):
        if hasattr(tool_ctx, attr):
            setattr(snapshot, attr, getattr(tool_ctx, attr))
    return snapshot


def _execute_command_async_upgrade_threshold(
    tool_name: str,
    tool_args: dict[str, Any],
    runtime_cfg: dict[str, Any] | None,
) -> float | None:
    """Return the adaptive async threshold for execute_command, or None."""
    # [AutoC 2026-06-27] Why: execute_command has its own hard timeout that must
    # remain the absolute kill ceiling, while the engine may stop waiting earlier.
    # How: enable only for execute_command and compute min(threshold, timeout*0.8),
    # skipping calls whose timeout is too close to the configured threshold. Purpose:
    # slow shell commands continue in the background without changing tool syntax.
    if str(tool_name or "") != "execute_command":
        return None
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    if not get_bool(cfg, "meta.execute_command.async_upgrade.enabled", True):
        return None
    threshold_sec = get_float(
        cfg,
        "meta.execute_command.async_upgrade.threshold_sec",
        60.0,
        min_value=0.01,
        max_value=3600.0,
    )
    default_timeout_sec = get_float(
        cfg,
        "meta.execute_command.default_timeout_sec",
        90.0,
        min_value=1.0,
        max_value=3600.0,
    )
    timeout_raw = (tool_args or {}).get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)
    if timeout_sec <= threshold_sec + 1.0:
        return None
    effective = min(float(threshold_sec), float(timeout_sec) * 0.8)
    return effective if effective > 0 else None


async def _execute_registry_tool_with_span(
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_ctx: ToolContext,
    *,
    async_call: bool = False,
) -> Any:
    """Run one registry tool call inside the existing SignalBus span."""
    # [AutoC 2026-06-27] Why: synchronous, declared-async, and adaptive-async paths
    # must keep the same tool.call signal semantics. How: centralize registry.execute
    # under one span helper. Purpose: refactoring async delivery does not silently
    # drop signal metadata or duplicate execution logic.
    _args_summary = _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200)
    payload: dict[str, Any] = {"tool": tool_name, "args_summary": _args_summary}
    if async_call:
        payload["async"] = True
    with get_bus().span('tool.call', payload=payload):
        return await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx)


async def _deliver_async_result(
    *,
    registry: ToolRegistry,
    http: Any,
    supervisor_url: str,
    task_id: str,
    session_id: str,
    tool_name: str,
    tool_args: dict,
    tool_ctx: ToolContext,
    async_tool_id: str,
    started_at: float,
    runtime_cfg: dict[str, Any] | None = None,
    step: int = 0,
    index: int = 0,
    tool_call_id: str = "",
    result: Any = None,
    error: Exception | None = None,
) -> None:
    """Format, track, and deliver an async tool result through supervisor."""
    # [AutoC 2026-06-27] Why: declared async tools and adaptive execute_command share
    # the same callback protocol. How: move result shaping, truncation, tracker
    # updates, attachment extraction, and async_tool_result POST into one delivery
    # function. Purpose: adaptive upgrade can await an already-started process without
    # copying the existing callback implementation.
    try:
        if error is not None:
            raise error
        _elapsed = time.monotonic() - started_at
        _summary = summarize_result(tool_name, result, args=tool_args)
        _tool_spec = registry.get_spec(tool_name)
        _fmt, raw = result_to_raw(tool_name, result, tool_spec=_tool_spec)
        if isinstance(raw, str):
            _limit = get_tool_inline_limit(tool_name, runtime_cfg)
            _ref = ""
            if artifact_enabled(runtime_cfg) and len(raw) > _limit:
                _ref = write_artifact(tool_ctx.workspace_root, task_id, step, index, tool_name, tool_call_id or async_tool_id, raw)
            raw, _was_truncated = truncate_tool_result(tool_name, raw, _limit, _ref, config=runtime_cfg)

        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "done",
            "task_id": task_id,
            "started_at": started_at,
            "finished_at": time.monotonic(),
            "elapsed": round(_elapsed, 1),
        }

        preempt_text = (
            f'✅ Async tool "{tool_name}" (id: {async_tool_id}) completed in {_elapsed:.1f}s.'
            f'\nSummary: {_summary}\nResult:\n{raw}'
        )

        attachments: list[str] = []
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            source_attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else result.get("attachments")
            if isinstance(source_attachments, list):
                for a in source_attachments:
                    if isinstance(a, dict) and a.get("path"):
                        attachments.append(str(a["path"]))
                    elif isinstance(a, str):
                        attachments.append(a)

        payload: dict = {"message": preempt_text, "task_id": task_id}
        if attachments:
            payload["attachment_paths"] = attachments

        await http.post(
            f"{supervisor_url}/v1/sessions/{session_id}/async_tool_result",
            json=payload,
        )
    except Exception as e:
        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "failed",
            "task_id": task_id,
            "started_at": started_at,
            "finished_at": time.monotonic(),
            "elapsed": round(time.monotonic() - started_at, 1),
            "error": str(e),
        }
        try:
            await http.post(
                f"{supervisor_url}/v1/sessions/{session_id}/async_tool_result",
                json={"message": f'❌ Async tool "{tool_name}" (id: {async_tool_id}) failed: {e}', "task_id": task_id},
            )
        except Exception:
            pass


async def _deliver_started_async_task(
    exec_task: "asyncio.Task[Any]",
    **delivery_kwargs: Any,
) -> None:
    """Await an already-started tool task and deliver its final async result."""
    # [AutoC 2026-06-27] Why: adaptive execute_command begins as a normal registry
    # execution before the threshold proves it is slow. How: await that existing task
    # instead of starting the command again, then delegate to _deliver_async_result.
    # Purpose: the subprocess remains single-instance and execute_command's internal
    # timeout still owns the eventual kill behavior.
    try:
        result = await exec_task
    except Exception as e:
        await _deliver_async_result(error=e, **delivery_kwargs)
    else:
        await _deliver_async_result(result=result, **delivery_kwargs)


async def _run_async_tool(
    registry: ToolRegistry,
    http: Any,
    supervisor_url: str,
    task_id: str,
    session_id: str,
    tool_name: str,
    tool_args: dict,
    tool_ctx: ToolContext,
    async_tool_id: str,
    runtime_cfg: dict[str, Any] | None = None,
    step: int = 0,
    index: int = 0,
    tool_call_id: str = "",
) -> None:
    """后台执行异步工具，完成后通过路由 session 的 API 注入结果。"""
    # [Fork/Merge 2026-05-12] session_id here is the event/user-facing route session.
    # Why: async tool results create a new inbound and must attach to the parent session when
    # the original task is running on a branch. How: callers pass parent_session_id when present.
    # Purpose: branch-local ConversationStore writes remain isolated while async callbacks still
    # reach the SDK conversation_key mapping.
    _started = time.monotonic()
    try:
        result = await _execute_registry_tool_with_span(
            registry,
            tool_name,
            tool_args,
            tool_ctx,
            async_call=True,
        )
    except Exception as e:
        await _deliver_async_result(
            registry=registry,
            http=http,
            supervisor_url=supervisor_url,
            task_id=task_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_ctx=tool_ctx,
            async_tool_id=async_tool_id,
            started_at=_started,
            runtime_cfg=runtime_cfg,
            step=step,
            index=index,
            tool_call_id=tool_call_id,
            error=e,
        )
    else:
        await _deliver_async_result(
            registry=registry,
            http=http,
            supervisor_url=supervisor_url,
            task_id=task_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_ctx=tool_ctx,
            async_tool_id=async_tool_id,
            started_at=_started,
            runtime_cfg=runtime_cfg,
            step=step,
            index=index,
            tool_call_id=tool_call_id,
            result=result,
        )
