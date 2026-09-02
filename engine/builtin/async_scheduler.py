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
    report_async_tool_started,
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


def _async_marker(async_id: str, raw_inline: str, handoff_message: str) -> dict[str, Any]:
    """Build the async_started resolution the loop renders as a placeholder."""
    return {
        "async_started": True,
        "async_id": async_id,
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
            # [AutoC 2026-09-02] 异步工具级取消：快照携带 async_id，
            # execute_command 的轮询循环凭它查询取消登记表。
            try:
                snapshot._async_id = async_id
            except AttributeError:
                pass
            # [AutoC 2026-08-28] 声明式异步同样授权前置：先审批，获批后再
            # 宣称"已启动"。拒绝时同步返回错误结果，不出现占位。
            denied = await ls.registry.authorize(name=tool_name, arguments=tool_args, ctx=snapshot)
            if denied is not None:
                return hook_result(modified=True, channels={"execution": denied})
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
            report_async_tool_started(
                rctx=rctx, node_id=str(getattr(getattr(ls, "node", None), "id", "") or ""), async_id=async_id,
                tool_name=tool_name, tool_args=tool_args,
            )
            raw_inline = (
                f'⏳ Async tool "{tool_name}" started (id: {async_id}). '
                f'Result will be delivered via preempt when ready.'
            )
            return hook_result(
                modified=True,
                channels={"execution": _async_marker(async_id, raw_inline, "异步执行已启动")},
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

        # [AutoC 2026-08-28] 授权先行。Why: 审批卡点（request_guard）在执行
        # 任务内部，asyncio.wait 的阈值计时把审批等待也算了进去——审批弹窗
        # 挂超过阈值，命令未经批准就被标成"已自动转为异步"。How: 创建执行
        # 任务前先调 registry.authorize()（只跑策略与审批，不执行）；获批后
        # 才 create_task（authorized=True 跳过二次审批）并启动阈值计时。
        # Purpose: 拒绝的命令零执行；阈值只计量获批后的真实执行时长，即
        # "审批后执行截断一分钟进入异步"。
        async def _adaptive() -> Any:
            exec_ctx = _snapshot_tool_context(tool_ctx)
            denied = await ls.registry.authorize(name=tool_name, arguments=tool_args, ctx=exec_ctx)
            if denied is not None:
                return denied
            exec_task = asyncio.create_task(
                _execute_registry_tool_with_span(ls.registry, tool_name, tool_args, exec_ctx, authorized=True),
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
            try:
                exec_ctx._async_id = async_id
            except AttributeError:
                pass
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
            report_async_tool_started(
                rctx=rctx, node_id=str(getattr(getattr(ls, "node", None), "id", "") or ""), async_id=async_id,
                tool_name=tool_name, tool_args=tool_args,
                upgraded_from="sync_timeout",
            )
            raw_inline = (
                f'⏳ Tool "{tool_name}" exceeded {threshold:.1f}s and was '
                f'auto-upgraded to async (id: {async_id}). Result will be delivered via preempt when ready. You can finish this task or continue working on other things.'
            )
            return _async_marker(async_id, raw_inline, "已自动转为异步执行")

        return hook_result(modified=True, channels={"execution": _adaptive()})
