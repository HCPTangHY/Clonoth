from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
# Why: the scheduler builds on the async tool lifecycle helpers extracted from
# the inference loop. How: import the helpers as module-level names so tests can
# patch this plugin's references directly. Purpose: the loop no longer knows how
# execution strategies are implemented, and the strategy stays independently
# testable.
from ..inference.async_tools import (
    _async_tool_tasks,
    _cleanup_async_tracker,
    _deliver_started_async_task,
    _execute_command_async_upgrade_threshold,
    _execute_registry_tool_with_span,
    _run_async_tool,
    _snapshot_tool_context,
)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "AsyncScheduler",
    "hook_points": [
        ("execute_tool", "handle"),
    ],
    "priority": 100,
    "description": "Tool execution scheduling: async_mode background dispatch and adaptive execute_command async upgrade.",
}


def _async_marker(async_id: str, summary: str, raw_inline: str, handoff_message: str) -> dict[str, Any]:
    """Build the async_started resolution the loop renders as a placeholder."""
    return {
        "async_started": True,
        "async_id": async_id,
        "summary": summary,
        "raw_inline": raw_inline,
        "handoff_message": handoff_message,
    }


class AsyncScheduler:
    """Supply execution strategies for real tool calls.

    Why: whether a tool is awaited synchronously, dispatched to a background
    task, or adaptively upgraded after a threshold is scheduling policy, not
    loop mechanics. How: answer the execute_tool point — spec-declared
    async_mode tools are dispatched immediately with a placeholder resolution;
    execute_command runs under an adaptive threshold and upgrades to background
    delivery when exceeded. Purpose: the inference loop stays strategy-free and
    alternative schedulers can be mounted instead.
    """

    name = "async_scheduler"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        ls = ctx.extra.get("loop_state")
        tool_ctx = ctx.extra.get("tool_ctx")
        if ls is None or tool_ctx is None:
            return None
        tool_name = str(ctx.extra.get("tool_name") or "")
        tool_args = ctx.extra.get("tool_args") or {}
        raw_spec = ctx.extra.get("spec")
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        rctx = ls.rctx
        step = ctx.step
        index = int(ctx.extra.get("call_index") or 0)
        tool_call_id = str(getattr(ctx.tool_call, "id", "") or "")
        t0 = float(ctx.extra.get("t0") or time.monotonic())

        if spec.get("async_mode", False):
            # [Fork/Merge 2026-05-12] Route async callbacks through the parent
            # session: rctx.session_id may be an entry branch used only for
            # runtime history, so async tool results must create follow-up
            # inbound messages in the SDK-visible session.
            _cleanup_async_tracker()
            async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[async_id] = {
                "tool_name": tool_name,
                "status": "running",
                "started_at": t0,
                "task_id": rctx.task_id,
            }
            # [AutoC 2026-06-27] Why: the loop reuses tool_ctx and rewrites
            # tool_call_id for later calls. How: pass a snapshot to the
            # background task. Purpose: callback artifacts and approvals keep
            # the current tool call identity.
            snapshot = _snapshot_tool_context(tool_ctx)
            asyncio.create_task(
                _run_async_tool(
                    registry=ls.registry,
                    http=rctx.http,
                    supervisor_url=rctx.supervisor_url,
                    task_id=rctx.task_id,
                    session_id=rctx.parent_session_id or rctx.session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_ctx=snapshot,
                    async_tool_id=async_id,
                    runtime_cfg=ls.runtime_cfg,
                    step=step,
                    index=index,
                    tool_call_id=tool_call_id or async_id,
                ),
                name=f"async_tool_{tool_name}_{async_id}",
            )
            summary = f"异步执行已启动 (id: {async_id})，结果将通过 preempt 自动回传"
            raw_inline = (
                f'⏳ Async tool "{tool_name}" started (id: {async_id}). '
                f'Result will be delivered via preempt when ready.'
            )
            return hook_result(
                modified=True,
                channels={"execution": _async_marker(async_id, summary, raw_inline, "异步执行已启动")},
            )

        # [AutoC 2026-06-27] Why: execute_command may run longer than the model
        # should wait, but its own timeout_sec must remain the hard kill limit.
        # How: start the normal registry execution with a ToolContext snapshot,
        # wait only until the effective threshold, then deliver the same task in
        # the background if it is still pending. Purpose: the model receives an
        # immediate tool result placeholder while the subprocess keeps running.
        threshold = _execute_command_async_upgrade_threshold(tool_name, tool_args, ls.runtime_cfg)
        if threshold is None:
            return None  # default synchronous execution

        # [AutoC 2026-08-28] 惰性协程：task 在第一次 await 时才创建，审批与
        # 阈值定时由协程体内控制。Why: 旧形态在任务创建时即启动真实执行，
        # 审批（before_tool_call）出现在执行开始之后，命令未经批准已在跑。
        # How: 协程体内先等阈值计时器、再做 before_tool_call 检查、最后才
        # create_task 启动执行。Purpose: 审批拒绝的命令零执行，审批时间不算
        # 入升级阈值——阈值表示"已批准的实际执行超过 X 秒才升级"。
        async def _adaptive() -> Any:
            exec_ctx = _snapshot_tool_context(tool_ctx)

            # 审批（升级前阻塞语义）：执行开始前检查，拒绝则直接返回拒绝标记
            approval_result = await hook_registry.afire("before_tool_call", ctx)
            if approval_result.action is not None:
                return {"approval_blocked": True, "action": approval_result.action}
            if approval_result.block or approval_result.skip_step:
                reason = (
                    approval_result.error_message
                    or approval_result.reason
                    or "Tool call blocked by before_tool_call approval."
                )
                return {
                    "approval_blocked": True,
                    "blocked": True,
                    "reason": reason,
                    "summary": reason[:200],
                }

            # 审批通过后才启动执行与阈值定时
            exec_task = asyncio.create_task(
                _execute_registry_tool_with_span(ls.registry, tool_name, tool_args, exec_ctx),
                name=f"execute_command_adaptive_{tool_call_id[:24] or 'call'}",
            )
            done, _pending = await asyncio.wait({exec_task}, timeout=float(threshold))
            if exec_task in done:
                return exec_task.result()
            _cleanup_async_tracker()
            async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[async_id] = {
                "tool_name": tool_name,
                "status": "running",
                "started_at": t0,
                "task_id": rctx.task_id,
                "upgraded_from": "sync_timeout",
            }
            asyncio.create_task(
                _deliver_started_async_task(
                    exec_task,
                    registry=ls.registry,
                    http=rctx.http,
                    supervisor_url=rctx.supervisor_url,
                    task_id=rctx.task_id,
                    session_id=rctx.parent_session_id or rctx.session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_ctx=exec_ctx,
                    async_tool_id=async_id,
                    started_at=t0,
                    runtime_cfg=ls.runtime_cfg,
                    step=step,
                    index=index,
                    tool_call_id=tool_call_id or async_id,
                ),
                name=f"async_upgrade_{tool_name}_{async_id}",
            )
            summary = f"执行超过 {threshold:.1f}s，已自动转为异步 (id: {async_id})，结果将通过 preempt 自动回传"
            raw_inline = (
                f'⏳ Tool "{tool_name}" exceeded {threshold:.1f}s and was '
                f'auto-upgraded to async (id: {async_id}). Result will be delivered via preempt when ready.'
            )
            return _async_marker(async_id, summary, raw_inline, "已自动转为异步执行")

        return hook_result(modified=True, channels={"execution": _adaptive()})
