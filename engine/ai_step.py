from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import format_skill_discovery_message
from providers.openai import OpenAIProvider

from .context_store import load_context_snapshot, save_context_snapshot, write_context_snapshot
from .graph import Workflow, allowed_outcomes
from .node import Node
from .prompt import assemble_prompt
from .protocol import TaskResult
from .tool_step import format_tool_trace
from clonoth_runtime import get_int, load_runtime_config

if TYPE_CHECKING:
    from .context import RunContext


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("input_schema", {"type": "object", "properties": {}, "required": []}),
        },
    } for s in specs]


def _select_outcome_spec(outcomes: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "select_outcome",
            "description": "选择当前节点的处理结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string", "description": "结果类型", "enum": outcomes},
                    "instruction": {"type": "string", "description": "给下游节点或后续处理的说明"},
                    "text": {"type": "string", "description": "回复文本（如果 outcome 是 reply）"},
                },
                "required": ["outcome"],
            },
        },
    }


def _filter_tool_specs(node: Node, all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mode = (node.tool_access.mode or "none").lower()
    if mode == "all":
        denied = set(node.tool_access.deny)
        if denied:
            return [s for s in all_specs if s.get("name") not in denied]
        return list(all_specs)
    if mode == "allowlist":
        allowed = set(node.tool_access.allow)
        return [s for s in all_specs if s.get("name") in allowed]
    return []


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "...<truncated>"


class _StreamBuffer:
    def __init__(self, rctx: "RunContext", node_id: str, kind: str) -> None:
        self._rctx = rctx
        self._node_id = node_id
        self._kind = kind
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self.flushed_any = False

    async def push(self, chunk: str) -> None:
        self._buf.append(chunk)
        now = time.monotonic()
        buf_len = sum(len(s) for s in self._buf)
        if now - self._last_flush >= 0.15 or buf_len >= 60:
            await self.flush()

    async def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()
        self.flushed_any = True
        await self._rctx.emit_event("stream_delta", {
            "node_id": self._node_id,
            "type": self._kind,
            "content": text,
        })


def _sanitize_context_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "ctx").strip() or "ctx")[:80]


def _persist_node_context(
    workspace_root: Path,
    session_id: str,
    task_id: str,
    node_id: str,
    messages: list[dict[str, Any]],
    *,
    step_count: int,
    context_ref: str = "",
) -> str:
    snapshot = {
        "version": 1,
        "node_id": node_id,
        "messages": messages,
        "step_count": int(step_count),
    }
    if context_ref:
        return write_context_snapshot(workspace_root, context_ref, snapshot)
    context_id = _sanitize_context_id(f"{task_id}_{node_id}")
    return save_context_snapshot(workspace_root, session_id, snapshot, context_id=context_id)


def _build_resume_message(resume_data: dict[str, Any]) -> dict[str, str] | None:
    rtype = str(resume_data.get("type") or "").strip()
    if rtype == "tool_results":
        entries = resume_data.get("tool_results")
        if not isinstance(entries, list):
            entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            return {"role": "assistant", "content": format_tool_trace(entries)}
        return None

    if rtype == "handoff_result":
        child_node_id = str(resume_data.get("child_node_id") or "")
        child_outcome = str(resume_data.get("child_outcome") or "")
        summary = str(resume_data.get("summary") or "")
        text = str(resume_data.get("text") or resume_data.get("result") or "")
        lines = [
            "下游节点已经完成。",
            f"节点：{child_node_id}",
            f"outcome：{child_outcome}",
        ]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append("结果：")
            lines.append(text)
        return {"role": "user", "content": "\n".join(lines).strip()}

    return None


async def run_ai_node(
    *,
    rctx: RunContext,
    streaming: bool = False,
    provider: OpenAIProvider,
    registry: ToolRegistry,
    workflow: Workflow,
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    run_id: str = "",
    context_ref: str = "",
    resume_data: dict[str, Any] | None = None,
    downstream_capabilities: str = "",
) -> TaskResult:
    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)

    step_count = 0
    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if snapshot and isinstance(snapshot.get("messages"), list):
        messages = list(snapshot.get("messages") or [])
        try:
            step_count = int(snapshot.get("step_count") or 0)
        except Exception:
            step_count = 0
    else:
        system_prompt = assemble_prompt(rctx.workspace_root, node)
        skills_msg = format_skill_discovery_message(
            rctx.workspace_root,
            instruction_text=instruction,
            skill_mode=node.skill_access.mode,
            skill_allow=node.skill_access.allow,
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if skills_msg:
            messages.append({"role": "system", "content": skills_msg})
        if downstream_capabilities:
            messages.append({"role": "system", "content": downstream_capabilities})
        messages.extend(history)
        ctx_lines = [f"当前节点={node.id}", f"请完成指令：{instruction}"]
        messages.append({"role": "user", "content": "\n".join(ctx_lines)})

    if resume_data:
        extra = _build_resume_message(resume_data)
        if extra is not None:
            messages.append(extra)

    tool_specs = _filter_tool_specs(node, registry.list_specs())
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []
    outcomes = allowed_outcomes(workflow, node.id)
    if outcomes:
        openai_tools.append(_select_outcome_spec(outcomes))

    use_stream = streaming

    for step in range(step_count, max_steps):
        if await rctx.check_cancelled():
            await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
            return TaskResult(node_id=node.id, status="cancelled", outcome="cancelled", text="任务已被用户取消。")

        text_buf: _StreamBuffer | None = None
        think_buf: _StreamBuffer | None = None
        tools_arg = openai_tools if openai_tools else None

        resp = None

        if use_stream:
            text_buf = _StreamBuffer(rctx, node.id, "text")
            think_buf = _StreamBuffer(rctx, node.id, "thinking")
            stream_task = asyncio.create_task(
                provider.chat_stream(
                    messages=messages,
                    tools=tools_arg,
                    on_text=text_buf.push,
                    on_thinking=think_buf.push,
                )
            )
            while True:
                done, _ = await asyncio.wait({stream_task}, timeout=0.3)
                if stream_task in done:
                    resp = stream_task.result()
                    break
                if await rctx.check_cancelled():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await text_buf.flush()
                    await think_buf.flush()
                    if text_buf.flushed_any or think_buf.flushed_any:
                        await rctx.emit_event("stream_end", {"node_id": node.id, "has_text": text_buf.flushed_any, "has_thinking": think_buf.flushed_any})
                    await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
                    return TaskResult(node_id=node.id, status="cancelled", outcome="cancelled", text="任务已被用户取消。")
            await text_buf.flush()
            await think_buf.flush()
            if text_buf.flushed_any or think_buf.flushed_any:
                await rctx.emit_event("stream_end", {
                    "node_id": node.id,
                    "has_text": text_buf.flushed_any,
                    "has_thinking": think_buf.flushed_any,
                })
            if resp.ok and resp.tool_calls:
                use_stream = False
        else:
            # 把 LLM 调用包装成可取消的：每 0.3 秒检查一次取消状态
            llm_task = asyncio.create_task(
                provider.chat(messages=messages, tools=tools_arg)
            )
            while True:
                done, _ = await asyncio.wait({llm_task}, timeout=0.3)
                if llm_task in done:
                    resp = llm_task.result()
                    break
                if await rctx.check_cancelled():
                    llm_task.cancel()
                    try:
                        await llm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
                    return TaskResult(node_id=node.id, status="cancelled", outcome="cancelled", text="任务已被用户取消。")

        assert resp is not None

        if not resp.ok:
            ctx_ref = _persist_node_context(
                rctx.workspace_root,
                rctx.session_id,
                rctx.task_id or run_id or node.id,
                node.id,
                messages,
                step_count=step + 1,
                context_ref=context_ref,
            )
            return TaskResult(
                node_id=node.id,
                kind="final",
                status="failed",
                outcome="failed",
                text=resp.error or "LLM 调用失败",
                summary=_short(resp.error or "LLM 调用失败", 240),
                context_ref=ctx_ref,
            )

        if resp.tool_calls:
            select_call = None
            real_tool_calls: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                if tc.name == "select_outcome":
                    select_call = tc
                else:
                    real_tool_calls.append({
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": dict(tc.arguments or {}),
                    })

            if select_call is not None:
                args = select_call.arguments or {}
                outcome_name = str(args.get("outcome") or "completed").strip() or "completed"
                text = str(args.get("text") or args.get("instruction") or "").strip()
                instr = str(args.get("instruction") or "").strip()
                ctx_ref = _persist_node_context(
                    rctx.workspace_root,
                    rctx.session_id,
                    rctx.task_id or run_id or node.id,
                    node.id,
                    messages,
                    step_count=step + 1,
                    context_ref=context_ref,
                )
                if outcome_name == "reply":
                    return TaskResult(
                        node_id=node.id,
                        kind="final",
                        status="completed",
                        outcome="reply",
                        text=text,
                        summary=_short(text, 240),
                        context_ref=ctx_ref,
                    )
                return TaskResult(
                    node_id=node.id,
                    kind="final",
                    status="completed",
                    outcome=outcome_name,
                    text=text,
                    instruction=instr,
                    summary=_short(text or instr or outcome_name, 240),
                    context_ref=ctx_ref,
                )

            if real_tool_calls:
                await rctx.emit_event("handoff_progress", {
                    "message": f"[{node.id}] 请求执行 {len(real_tool_calls)} 个工具",
                    "node_id": node.id,
                    "task_id": rctx.task_id,
                })
                ctx_ref = _persist_node_context(
                    rctx.workspace_root,
                    rctx.session_id,
                    rctx.task_id or run_id or node.id,
                    node.id,
                    messages,
                    step_count=step + 1,
                    context_ref=context_ref,
                )
                return TaskResult(
                    node_id=node.id,
                    kind="yield_tool",
                    status="completed",
                    result_type="yield_tool",
                    outcome="yield_tool",
                    summary=f"yield {len(real_tool_calls)} tool calls",
                    context_ref=ctx_ref,
                    tool_calls=real_tool_calls,
                )

        text = (resp.text or "").strip()
        if text:
            ctx_ref = _persist_node_context(
                rctx.workspace_root,
                rctx.session_id,
                rctx.task_id or run_id or node.id,
                node.id,
                messages,
                step_count=step + 1,
                context_ref=context_ref,
            )
            return TaskResult(
                node_id=node.id,
                kind="final",
                status="completed",
                outcome="completed",
                text=text,
                summary=_short(text, 240),
                context_ref=ctx_ref,
            )

    ctx_ref = _persist_node_context(
        rctx.workspace_root,
        rctx.session_id,
        rctx.task_id or run_id or node.id,
        node.id,
        messages,
        step_count=max_steps,
        context_ref=context_ref,
    )
    return TaskResult(
        node_id=node.id,
        kind="final",
        status="failed",
        outcome="failed",
        text="达到最大步数限制。",
        summary="max_steps reached",
        context_ref=ctx_ref,
    )
