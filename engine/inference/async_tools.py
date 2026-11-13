"""异步工具生命周期：启动、跟踪、结果投递。

[AutoC 2026-08-18] 从 ai_step.py 抽出。这组函数构成一个自洽的子主题：
当工具 spec 标记 async_mode=True，或 execute_command 超过自适应阈值时，
工具在后台 asyncio.Task 中执行，完成后通过 supervisor 的
/v1/sessions/{id}/async_tool_result 接口将结果注入回对话流。

模块级字典 _async_tool_tasks 保存执行侧状态；Supervisor 保存后台责任与投递确认。
ai_step 与本模块共享同一引用（import 的是同一对象，直接原地修改）。
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
    truncate_tool_result,
    write_artifact,
)
from .loop_state import _short

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  异步工具跟踪表（Async Tool Tracking）
#  [2026-04-23] 从 commit 7d10197 恢复，在 864a333 大扫除中被误删。
#  key = async_tool_id (UUID hex), value = 状态字典
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
        if v.get("status") in ("done", "failed", "cancelled")
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
        entry = {"async_id": aid, **{k: v for k, v in info.items() if not k.startswith("_")}}
        # 对 running 状态补算已经过的时间
        if info.get("status") == "running" and "started_at" in info:
            entry["elapsed"] = round(now - info["started_at"], 1)
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
#  Supervisor 生命周期上报（2026-09-02）
#  Why: 异步工具状态过去只存在 engine 进程内存，supervisor/前端完全不可见。
#  How: 启动登记确认后才脱离主循环；计算结束与结果投递分别确认。
#  Purpose: supervisor 持有影子清单，可查询、可在引擎重启时标记 lost。
# ---------------------------------------------------------------------------

async def _post_ack(http: Any, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Retry only the same idempotent payload; an HTTP response alone is not an ACK."""
    import httpx

    for attempt in range(3):
        try:
            response = await http.post(url, json=payload, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError(f"async lifecycle not acknowledged: {data!r}")
            return data
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == 2:
                raise
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
        await asyncio.sleep(0.1 * (attempt + 1))
    raise RuntimeError("async lifecycle acknowledgement exhausted")


async def report_async_tool_lifecycle(
    http: Any,
    supervisor_url: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Confirm a lifecycle update; callers must retain unfinished delivery on failure."""
    return await _post_ack(http, f"{supervisor_url.rstrip('/')}/v1/async_tools{path}", payload)


async def report_async_tool_started(
    *,
    rctx: Any,
    node_id: str,
    async_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    upgraded_from: str = "",
) -> dict[str, Any]:
    """Register before detaching work and return its immutable delivery identity."""
    payload = {
        "async_id": async_id,
        "tool_name": tool_name,
        "session_id": getattr(rctx, "parent_session_id", "") or getattr(rctx, "session_id", ""),
        "task_id": getattr(rctx, "task_id", ""),
        "node_id": node_id,
        "worker_id": getattr(rctx, "worker_id", ""),
        "execution_id": getattr(rctx, "execution_id", ""),
        "args_summary": _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200),
        "upgraded_from": upgraded_from,
    }
    acknowledgement = await report_async_tool_lifecycle(rctx.http, rctx.supervisor_url, "", payload)
    identity = {
        "execution_id": payload["execution_id"],
        "boot_id": acknowledgement.get("boot_id", ""),
        "target_generation": acknowledgement.get("target_generation", 0),
    }
    if payload["execution_id"] and (
        not isinstance(identity["boot_id"], str) or not identity["boot_id"]
        or type(identity["target_generation"]) is not int or identity["target_generation"] < 1
    ):
        raise ValueError("async registration omitted boot or target generation identity")
    return identity


async def settle_inline_registration(
    *, rctx: Any, node_id: str, async_id: str, tool_name: str,
    tool_args: dict[str, Any], upgraded_from: str = "", status: str = "cancelled",
    error: str = "", elapsed_sec: float = 0.0,
) -> bool:
    """Reconcile an uncertain start using its original id, without executing work again.

    The result is returned by the original synchronous tool call. The supervisor
    can close this receipt only when that source execution completes normally.
    """
    try:
        identity = await report_async_tool_started(
            rctx=rctx, node_id=node_id, async_id=async_id, tool_name=tool_name,
            tool_args=tool_args, upgraded_from=upgraded_from,
        )
        await report_async_tool_lifecycle(
            rctx.http, rctx.supervisor_url, f"/{async_id}/finish",
            {**identity, "status": status, "error": error[:500], "elapsed_sec": elapsed_sec,
             "delivered_inline": True, "carrier_task_id": getattr(rctx, "task_id", "")},
        )
        return True
    except Exception as exc:
        logger.warning("Uncertain async registration remains unresolved: %s: %s", async_id, exc)
        return False


_CANCELLED_ASYNC_IDS: set[str] = set()


def mark_async_tool_cancelled(async_id: str) -> None:
    """登记一个取消意图；engine 侧执行体与 runner 轮询都会消费。"""
    aid = (async_id or "").strip()
    if aid:
        _CANCELLED_ASYNC_IDS.add(aid)
        # [fix 2026-09-03] Why: the mark alone only stopped execute_command (its
        # 0.2s loop polls the registry); MCP tools and other async_mode tools ran
        # to completion, so cancel was label-only and saved nothing. How: the
        # runners already register their asyncio task handle under _task — cancel
        # it so the underlying coroutine is actually interrupted; the runners'
        # CancelledError handlers report a proper cancelled finish. Purpose:
        # cancel really stops the work, not just the label.
        entry = _async_tool_tasks.get(aid)
        task = entry.get("_task") if isinstance(entry, dict) else None
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()


def is_async_tool_cancelled(async_id: str) -> bool:
    """查询某异步工具是否被请求取消。"""
    return (async_id or "") in _CANCELLED_ASYNC_IDS


def _clear_cancel_mark(async_id: str) -> None:
    _CANCELLED_ASYNC_IDS.discard(async_id)


def _snapshot_tool_context(tool_ctx: ToolContext) -> ToolContext:
    """Return an immutable-enough ToolContext snapshot for background tool work."""
    # [AutoC 2026-06-27] Why: _execute_real_tools reuses one ToolContext and rewrites
    # tool_call_id for every tool in the batch. How: copy dataclass fields and the
    # node-specific dynamic attributes before scheduling background work. Purpose:
    # async execute_command callbacks keep the approval and artifact identity of the
    # original tool call.
    snapshot = replace(tool_ctx)
    for attr in ("_node_id", "_node_extra", "_async_id"):
        if hasattr(tool_ctx, attr):
            setattr(snapshot, attr, getattr(tool_ctx, attr))
    return snapshot


def _execute_command_async_upgrade_threshold(
    tool_name: str,
    tool_args: dict[str, Any],
    runtime_cfg: dict[str, Any] | None,
) -> float | None:
    """Return the adaptive async threshold for execute_command / remote_exec / read_media, or None."""
    # [AutoC 2026-06-27] Why: execute_command (and remote_exec / read_media) has its own
    # hard timeout that must remain the absolute kill ceiling, while the engine may stop
    # waiting earlier. How: compute min(threshold, timeout*0.8), skipping calls whose
    # timeout is too close to the configured threshold. Purpose: slow shell commands or
    # long video analyses continue in the background without changing tool syntax.
    tname = str(tool_name or "")
    if tname not in ("execute_command", "remote_exec", "read_media"):
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
    default_timeout_sec = 300.0 if tname == "read_media" else (120.0 if tname == "remote_exec" else get_float(
        cfg,
        "meta.execute_command.default_timeout_sec",
        90.0,
        min_value=1.0,
        max_value=3600.0,
    ))
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
    authorized: bool = False,
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
        return await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx, _authorized=authorized)


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
    error: BaseException | None = None,
    registration: dict[str, Any] | None = None,
) -> None:
    """Compute outcome once, then confirm finish and delivery without conflating failures."""
    identity = dict(registration or {})
    elapsed = round(time.monotonic() - started_at, 1)
    attachments: list[str] = []
    status = "done"
    failure = ""
    try:
        if error is not None:
            raise error
        # [fix 2026-09-03] Why: a cancel that lands after the work finished but
        # before delivery used to ship as done and overwrite the supervisor's
        # cancel_requested marker — label and reality diverged. How: honor the
        # cancel mark at delivery time; the cancel API contract is that the
        # result will not be delivered. Purpose: consistent cancelled state on
        # both engine tracker and supervisor shadow list.
        if is_async_tool_cancelled(async_tool_id):
            raise asyncio.CancelledError()
        _elapsed = time.monotonic() - started_at
        _tool_spec = registry.get_spec(tool_name)
        _fmt, raw = result_to_raw(tool_name, result, tool_spec=_tool_spec)
        if isinstance(raw, str):
            _limit = get_tool_inline_limit(tool_name, runtime_cfg)
            _ref = ""
            if artifact_enabled(runtime_cfg) and len(raw) > _limit:
                _ref = write_artifact(tool_ctx.workspace_root, task_id, step, index, tool_name, tool_call_id or async_tool_id, raw)
            raw, _was_truncated = truncate_tool_result(tool_name, raw, _limit, _ref, config=runtime_cfg)

        preempt_text = (
            f'✅ Async tool "{tool_name}" (id: {async_tool_id}) completed in {_elapsed:.1f}s.'
            f'\nResult:\n{raw}'
        )

        if isinstance(result, dict):
            if result.get("ok") is False or result.get("error"):
                status = "failed"
                failure = str(result.get("error") or "tool returned an unsuccessful result")
                preempt_text = f'❌ Async tool "{tool_name}" (id: {async_tool_id}) failed:\n{raw}'
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            source_attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else result.get("attachments")
            if isinstance(source_attachments, list):
                for a in source_attachments:
                    if isinstance(a, dict) and a.get("path"):
                        attachments.append(str(a["path"]))
                    elif isinstance(a, str):
                        attachments.append(a)

    except (Exception, asyncio.CancelledError) as exc:
        cancelled = is_async_tool_cancelled(async_tool_id) or isinstance(exc, asyncio.CancelledError)
        status = "cancelled" if cancelled else "failed"
        failure = "" if cancelled else str(exc)
        attachments = []
        preempt_text = (
            f'⛔ Async tool "{tool_name}" (id: {async_tool_id}) was cancelled.'
            if cancelled else f'❌ Async tool "{tool_name}" (id: {async_tool_id}) failed: {exc}'
        )

    entry = _async_tool_tasks.setdefault(async_tool_id, {})
    entry.update(tool_name=tool_name, status="result_pending_delivery", compute_status=status,
                 task_id=task_id, started_at=started_at, elapsed=elapsed, error=failure)
    # Cancellation during finish reporting gets one cancellation-only delivery attempt.
    # A transport failure never generates another (incorrect) tool-failure result.
    for cancellation_attempt in range(2):
        try:
            if is_async_tool_cancelled(async_tool_id) and status != "cancelled":
                raise asyncio.CancelledError()
            await report_async_tool_lifecycle(
                http, supervisor_url, f"/{async_tool_id}/finish",
                {**identity, "status": status, "elapsed_sec": elapsed, "error": failure[:500]},
            )
            if is_async_tool_cancelled(async_tool_id) and status != "cancelled":
                raise asyncio.CancelledError()
            payload = {
                **identity, "async_id": async_tool_id, "delivery_id": f"{async_tool_id}:result",
                "message": preempt_text, "task_id": task_id,
            }
            if attachments:
                payload["attachment_paths"] = attachments
            await _post_ack(http, f"{supervisor_url.rstrip('/')}/v1/sessions/{session_id}/async_tool_result", payload)
        except asyncio.CancelledError as exc:
            if not cancellation_attempt:
                status, failure, attachments = "cancelled", "", []
                preempt_text = f'⛔ Async tool "{tool_name}" (id: {async_tool_id}) was cancelled.'
                entry.update(compute_status=status, error="")
                continue
            entry.update(status="delivery_failed", delivery_error="delivery cancelled", finished_at=time.monotonic())
            logger.warning("Async result delivery cancelled: %s", async_tool_id)
            return
        except Exception as exc:
            entry.update(status="delivery_failed", delivery_error=str(exc), finished_at=time.monotonic())
            logger.warning("Async result delivery unconfirmed: %s: %s", async_tool_id, exc)
            return
        else:
            entry.update(status=status, delivery_status="queued", finished_at=time.monotonic())
            return


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
    async_tool_id = str(delivery_kwargs.get("async_tool_id") or "")
    if isinstance(_async_tool_tasks.get(async_tool_id), dict):
        _async_tool_tasks[async_tool_id]["_task"] = asyncio.current_task()
    try:
        if is_async_tool_cancelled(async_tool_id):
            exec_task.cancel()
            await asyncio.gather(exec_task, return_exceptions=True)
            raise asyncio.CancelledError()
        result = await exec_task
    except asyncio.CancelledError as e:
        # [fix 2026-09-03] hard cancel from mark_async_tool_cancelled lands here;
        # route it through the normal cancelled-report delivery instead of dying.
        await _deliver_async_result(error=e, **delivery_kwargs)
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
    registration: dict[str, Any] | None = None,
    authorized: bool = False,
) -> None:
    """后台执行异步工具，完成后通过路由 session 的 API 注入结果。"""
    # [Fork/Merge 2026-05-12] session_id here is the event/user-facing route session.
    # Why: async tool results create a new inbound and must attach to the parent session when
    # the original task is running on a branch. How: callers pass parent_session_id when present.
    # Purpose: branch-local ConversationStore writes remain isolated while async callbacks still
    # reach the SDK conversation_key mapping.
    _started = time.monotonic()
    if isinstance(_async_tool_tasks.get(async_tool_id), dict):
        _async_tool_tasks[async_tool_id]["_task"] = asyncio.current_task()
    try:
        if is_async_tool_cancelled(async_tool_id):
            raise asyncio.CancelledError()
        result = await _execute_registry_tool_with_span(
            registry,
            tool_name,
            tool_args,
            tool_ctx,
            async_call=True,
            authorized=authorized,
        )
    except (Exception, asyncio.CancelledError) as exc:
        result, error = None, exc
    else:
        error = None
    await _deliver_async_result(
        registry=registry, http=http, supervisor_url=supervisor_url, task_id=task_id,
        session_id=session_id, tool_name=tool_name, tool_args=tool_args, tool_ctx=tool_ctx,
        async_tool_id=async_tool_id, started_at=_started, runtime_cfg=runtime_cfg,
        step=step, index=index, tool_call_id=tool_call_id, registration=registration,
        result=result, error=error,
    )
