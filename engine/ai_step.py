from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import format_skill_discovery_message
from providers.openai import OpenAIProvider

from .context import RunContext
from .graph import Workflow, allowed_outcomes
from .node import Node
from .prompt import assemble_prompt
from .protocol import NodeOutcome
from .tool_step import format_tool_trace, result_to_raw, summarize_result, write_artifact
from clonoth_runtime import get_int, load_runtime_config
from clonoth_runtime import get_bool, get_str
import logging

if TYPE_CHECKING:
    from .context import RunContext


# handoff 回调类型：(outcome_name, instruction) -> 下游结果文本 或 None（表示非 handoff）
OnHandoff = Callable[[str, str], Awaitable[str | None]]


def _persist_node_context(
    workspace_root: Path, run_id: str, node_id: str, messages: list[dict],
) -> str:
    if not run_id:
        return ""
    d = workspace_root / "data" / "node_contexts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{node_id}.json"
    p.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p.relative_to(workspace_root))


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
                    "instruction": {"type": "string", "description": "给下游节点的说明"},
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
    """流式输出缓冲。累积 delta 文本，按时间或大小批量发射事件。"""

    def __init__(self, rctx: "RunContext", node_id: str, kind: str) -> None:
        self._rctx = rctx
        self._node_id = node_id
        self._kind = kind  # "text" | "thinking"
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
    upstream_summaries: list[dict[str, Any]] | None = None,
    on_handoff: OnHandoff | None = None,
    downstream_capabilities: str = "",
) -> NodeOutcome:
    """执行一个 AI 节点。

    LLM 循环中：
    - tool_call → 执行 tool 节点（父子关系），结果注入消息历史，继续循环
    - select_outcome(reply) → 返回最终文本
    - select_outcome(X) + on_handoff 返回 str → handoff 回调成功，结果注入消息历史，继续循环
    - select_outcome(X) + on_handoff 返回 None → 非 handoff，正常返回 outcome
    - select_outcome(X) + on_handoff 为 None → 正常返回 outcome
    - 纯文本 → 返回 completed
    """

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)
    max_inline = get_int(runtime_cfg, "engine.tool_trace.max_inline_chars", 8000, min_value=500, max_value=100000)
    approval_poll = float(runtime_cfg.get("engine", {}).get("approval_poll_interval_sec", 0.5) or 0.5)

    system_prompt = assemble_prompt(rctx.workspace_root, node)
    skills_msg = format_skill_discovery_message(
        rctx.workspace_root, instruction_text=instruction,
        skill_mode=node.skill_access.mode, skill_allow=node.skill_access.allow,
    )

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if skills_msg:
        messages.append({"role": "system", "content": skills_msg})
    if downstream_capabilities:
        messages.append({"role": "system", "content": downstream_capabilities})
    messages.extend(history)

    ctx_lines = [f"当前节点={node.id}", f"请完成指令：{instruction}"]
    if upstream_summaries:
        ctx_lines.append("上游节点结果：")
        for us in upstream_summaries:
            ctx_lines.append(f"  - {us.get('node_id', '?')}: {us.get('summary', '')}")
    messages.append({"role": "user", "content": "\n".join(ctx_lines)})

    tool_specs = _filter_tool_specs(node, registry.list_specs())
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []
    outcomes = allowed_outcomes(workflow, node.id)
    if outcomes:
        openai_tools.append(_select_outcome_spec(outcomes))

    kctx = ToolContext(
        supervisor_url=rctx.supervisor_url, session_id=rctx.session_id,
        run_id=run_id, worker_id=rctx.worker_id,
        workspace_root=rctx.workspace_root, http=rctx.http,
        registry=registry, approval_poll_interval_sec=approval_poll,
    )

    use_stream = streaming  # 仅入口节点由 runner 传入 True

    for _step in range(max_steps):
        text_buf: _StreamBuffer | None = None
        think_buf: _StreamBuffer | None = None

        tools_arg = openai_tools if openai_tools else None

        if use_stream:
            text_buf = _StreamBuffer(rctx, node.id, "text")
            think_buf = _StreamBuffer(rctx, node.id, "thinking")
            resp = await provider.chat_stream(
                messages=messages, tools=tools_arg,
                on_text=text_buf.push, on_thinking=think_buf.push,
            )
            # 刷残余
            await text_buf.flush()
            await think_buf.flush()
            # 发送 stream_end
            if text_buf.flushed_any or think_buf.flushed_any:
                await rctx.emit_event("stream_end", {
                    "node_id": node.id,
                    "has_text": text_buf.flushed_any,
                    "has_thinking": think_buf.flushed_any,
                })
            # 流式回来后如果有 tool_calls，后续循环降级为非流式
            if resp.ok and resp.tool_calls:
                use_stream = False
        else:
            resp = await provider.chat(messages=messages, tools=tools_arg)

        if not resp.ok:
            return NodeOutcome(node_id=node.id, outcome="failed", text=resp.error or "LLM 调用失败")

        if resp.tool_calls:
            select_call = None
            real_tool_calls = []
            for tc in resp.tool_calls:
                if tc.name == "select_outcome":
                    select_call = tc
                else:
                    real_tool_calls.append(tc)

            # select_outcome 处理
            if select_call:
                args = select_call.arguments or {}
                outcome_name = str(args.get("outcome") or "completed").strip()
                text = str(args.get("text") or args.get("instruction") or "").strip()
                instr = str(args.get("instruction") or "").strip()

                # reply → 直接返回
                if outcome_name == "reply":
                    ctx_ref = _persist_node_context(rctx.workspace_root, run_id, node.id, messages)
                    return NodeOutcome(
                        node_id=node.id, outcome="reply", text=text,
                        summary=_short(text, 240), context_ref=ctx_ref,
                    )

                # 尝试 handoff 回调
                if on_handoff is not None:
                    sub_result = await on_handoff(outcome_name, instr or text)
                    if sub_result is not None:
                        # handoff 成功：结果注入消息历史，继续循环
                        messages.append({"role": "assistant", "content": f"[select_outcome: {outcome_name}] {instr or text}"})
                        messages.append({"role": "user", "content": f"下游节点执行结果：\n{sub_result}"})
                        continue
                    # sub_result 为 None → 非 handoff，落到下方正常返回

                # 没有回调 / 非 handoff → 直接返回 outcome
                ctx_ref = _persist_node_context(rctx.workspace_root, run_id, node.id, messages)
                return NodeOutcome(
                    node_id=node.id, outcome=outcome_name, text=text, instruction=instr,
                    summary=_short(text or instr, 240), context_ref=ctx_ref,
                )

            # 真实 tool 调用（父子关系）
            if real_tool_calls:
                trace_entries: list[dict[str, Any]] = []
                for tc in real_tool_calls:
                    func = registry._tool_funcs.get(tc.name)
                    if func is None:
                        trace_entries.append({
                            "name": tc.name, "args": tc.arguments, "format": "json",
                            "raw_inline": json.dumps({"ok": False, "error": f"工具不存在: {tc.name}"}, ensure_ascii=False),
                            "truncated": False, "ref": "", "summary": f"工具不存在: {tc.name}",
                        })
                        continue

                    result = await func(tc.arguments or {}, kctx)
                    summary = summarize_result(tc.name, result)
                    await rctx.emit_event("handoff_progress", {
                        "message": f"[{node.id}] 工具 {tc.name}: {summary}",
                    })
                    fmt, raw = result_to_raw(tc.name, result)
                    ref = ""
                    truncated = len(raw) > max_inline
                    if truncated and run_id:
                        ref = await write_artifact(rctx.workspace_root, run_id, tc.id, tc.name, fmt, raw)
                    raw_inline = raw if not truncated else raw[:max_inline] + "\n...<truncated>"
                    trace_entries.append({
                        "name": tc.name, "args": tc.arguments, "format": fmt,
                        "raw_inline": raw_inline, "truncated": truncated, "ref": ref, "summary": summary,
                    })

                messages.append({"role": "assistant", "content": format_tool_trace(trace_entries)})
                continue

        # 纯文本输出
        text = (resp.text or "").strip()
        if text:
            ctx_ref = _persist_node_context(rctx.workspace_root, run_id, node.id, messages)
            return NodeOutcome(
                node_id=node.id, outcome="completed", text=text,
                summary=_short(text, 240), context_ref=ctx_ref,
            )

    ctx_ref = _persist_node_context(rctx.workspace_root, run_id, node.id, messages)
    return NodeOutcome(
        node_id=node.id, outcome="failed", text="达到最大步数限制。",
        summary="max_steps reached", context_ref=ctx_ref,
    )
