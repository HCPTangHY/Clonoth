from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from clonoth_runtime import (
    fetch_openai_secret,
    get_bool,
    get_float,
    get_int,
    get_str,
    load_runtime_config,
    normalize_openai_secret,
)
from toolbox.registry import ToolRegistry
from providers.openai import OpenAIProvider

from .ai_step import run_ai_node
from .context import RunContext
from .graph import Workflow, load_workflow, next_node, handoff_target
from .model import resolve_provider
from .node import Node, load_node
from .protocol import NodeOutcome


# ---------------------------------------------------------------------------
#  下游能力收集：告诉入口节点下游能做什么
# ---------------------------------------------------------------------------

def _collect_own_tool_capabilities(
    node: "Node", registry: ToolRegistry,
) -> str:
    """收集节点自身的直接工具能力描述，作为动态上下文注入。"""
    all_specs = registry.list_specs()
    mode = (node.tool_access.mode or "none").lower()
    if mode == "all":
        denied = set(node.tool_access.deny)
        tool_specs = [s for s in all_specs if s["name"] not in denied] if denied else list(all_specs)
    elif mode == "allowlist":
        allowed = set(node.tool_access.allow)
        tool_specs = [s for s in all_specs if s["name"] in allowed]
    else:
        tool_specs = []

    if not tool_specs:
        return ""

    lines: list[str] = []
    for s in tool_specs:
        name = s["name"]
        desc = s.get("description", "")
        lines.append(f"- {name}：{desc}" if desc else f"- {name}")

    return (
        "## 你的直接工具\n\n"
        "以下工具已通过 function calling 提供给你，你可以直接调用，无需 handoff：\n\n"
        + "\n".join(lines)
    )


def _collect_downstream_capabilities(
    workspace_root: Path, workflow: Workflow, registry: ToolRegistry,
) -> str:
    """收集入口节点的直接下游节点能力描述，作为动态上下文注入 orchestrator。"""
    entry = workflow.entry_node
    edges = workflow.edges.get(entry, {})
    handoffs = workflow.handoffs.get(entry, {})
    merged = {**edges, **handoffs}
    all_specs = registry.list_specs()
    all_tool_names = [s["name"] for s in all_specs]

    lines: list[str] = []
    seen_nodes: set[str] = set()

    for outcome_name, target_id in merged.items():
        if not target_id or target_id.startswith("$"):
            continue
        if target_id in seen_nodes:
            continue
        seen_nodes.add(target_id)

        node = load_node(workspace_root, target_id)
        if node is None:
            continue

        mode = (node.tool_access.mode or "none").lower()
        if mode == "all":
            denied = set(node.tool_access.deny)
            tool_names = [n for n in all_tool_names if n not in denied] if denied else all_tool_names
        elif mode == "allowlist":
            tool_names = [n for n in node.tool_access.allow if n in {s["name"] for s in all_specs}]
        else:
            tool_names = []

        desc = node.description or node.name
        tool_list = ", ".join(tool_names) if tool_names else "（无工具）"
        lines.append(f"- {node.name}（{node.id}）：{desc}\n  可用工具：{tool_list}")

    if not lines:
        return ""
    return "## 下游节点能力\n\n通过 handoff 你可以调用以下下游节点：\n\n" + "\n".join(lines)



# ---------------------------------------------------------------------------
#  子链执行：从指定节点开始，沿 workflow 执行到终止
# ---------------------------------------------------------------------------

MAX_HANDOFF_DEPTH = 4  # 防止 handoff 无限嵌套


async def _run_subchain(
    *,
    rctx: RunContext,
    registry: ToolRegistry,
    workflow: Workflow,
    start_node_id: str,
    instruction: str,
    history: list[dict[str, Any]],
    run_id: str = "",
    depth: int = 0,
) -> NodeOutcome:
    """执行一条子链（executor → reviewer → ...），返回最终 NodeOutcome。

    子链中的节点如果在 workflow 中声明了 handoffs，也会触发嵌套子链调用。
    depth 参数用于防止无限嵌套。
    """

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    current_node_id = start_node_id
    current_instruction = instruction
    node_summaries: list[dict[str, Any]] = []

    for hop in range(20):
        # 检查点：取消检测
        if await rctx.check_cancelled():
            return NodeOutcome(node_id=current_node_id, outcome="cancelled", text="任务已被用户取消。")

        node = load_node(rctx.workspace_root, current_node_id)
        if node is None:
            return NodeOutcome(node_id=current_node_id, outcome="failed", text=f"节点未找到：{current_node_id}")

        await rctx.emit_event("node_started", {
            "node_id": node.id, "node_name": node.name, "hop": hop, "workflow_id": workflow.id,
        })

        rp = resolve_provider(rctx.workspace_root, runtime_cfg, node, rctx.default_model)
        provider = OpenAIProvider(
            http=rctx.llm_http, api_key=rp.api_key or rctx.api_key,
            base_url=rp.base_url or rctx.base_url or None, model=rp.model,
        )
        print(f"[engine] subchain node={node.id} model={rp.model} hop={hop} depth={depth}", flush=True)

        # 为子链节点构建 handoff 回调（如果深度允许且 workflow 中声明了 handoffs）
        sub_handoff = _make_subchain_handoff(
            rctx=rctx, registry=registry, workflow=workflow,
            node_id=current_node_id, history=history, run_id=run_id, depth=depth,
        )

        outcome = await run_ai_node(
            rctx=rctx, provider=provider, registry=registry, workflow=workflow,
            node=node, instruction=current_instruction, history=history,
            run_id=run_id, upstream_summaries=node_summaries,
            on_handoff=sub_handoff,
        )

        await rctx.emit_event("node_completed", {
            "node_id": outcome.node_id, "node_name": node.name,
            "outcome": outcome.outcome, "summary": outcome.summary, "workflow_id": workflow.id,
        })

        node_summaries.append({
            "node_id": outcome.node_id, "outcome": outcome.outcome,
            "summary": outcome.summary, "context_ref": outcome.context_ref,
        })

        # 查下一跳
        nxt = next_node(workflow, current_node_id, outcome.outcome)

        # 终止条件：回到入口节点、$reply、$end 或空
        if not nxt or nxt == "$reply" or nxt == "$end" or nxt == workflow.entry_node:
            return outcome

        current_instruction = outcome.instruction or outcome.text or current_instruction
        current_node_id = nxt

    return NodeOutcome(node_id=current_node_id, outcome="failed", text="子链超过最大跳转数。")


def _make_subchain_handoff(
    *,
    rctx: RunContext,
    registry: ToolRegistry,
    workflow: Workflow,
    node_id: str,
    history: list[dict[str, Any]],
    run_id: str,
    depth: int,
) -> Any:
    """为子链节点构建 handoff 回调。

    检查 workflow 中该节点的 handoffs 声明。如果 outcome 在 handoffs 中，
    触发嵌套子链；否则返回 None（表示非 handoff，按正常 flow 返回 outcome）。
    """
    node_handoffs = workflow.handoffs.get(node_id, {})
    if not node_handoffs or depth >= MAX_HANDOFF_DEPTH:
        return None  # 没有 handoff 声明或深度超限，不提供回调

    async def _handler(outcome_name: str, instruction: str) -> str | None:
        target = node_handoffs.get(outcome_name, "")
        if not target or target.startswith("$"):
            return None  # 此 outcome 不是 handoff

        await rctx.emit_event("handoff_progress", {
            "message": f"[{node_id}] handoff → {target} (depth={depth + 1})",
        })

        ho_outcome = await _run_subchain(
            rctx=rctx, registry=registry, workflow=workflow,
            start_node_id=target, instruction=instruction,
            history=history, run_id=run_id, depth=depth + 1,
        )

        await rctx.emit_event("handoff_progress", {
            "message": f"[{node_id}] handoff ← {ho_outcome.node_id}: {ho_outcome.outcome}",
        })

        return ho_outcome.text or ho_outcome.summary or "（回调节点未产出内容）"

    return _handler


# ---------------------------------------------------------------------------
#  图执行：入口节点持续运行，handoff 作为子调用
# ---------------------------------------------------------------------------

async def run_graph(
    *,
    rctx: RunContext,
    registry: ToolRegistry,
    workflow: Workflow,
    run_id: str = "",
) -> tuple[str, str]:
    """执行一次完整的图：入口节点持有上下文，handoff 是子调用。返回 (final_text, status)。"""

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    entry_node = load_node(rctx.workspace_root, workflow.entry_node)
    if entry_node is None:
        return f"入口节点未找到：{workflow.entry_node}", "failed"

    own_tool_caps = _collect_own_tool_capabilities(entry_node, registry)
    downstream_caps = _collect_downstream_capabilities(
        rctx.workspace_root, workflow, registry,
    )
    caps_parts = [p for p in [own_tool_caps, downstream_caps] if p]
    all_caps = "\n\n".join(caps_parts)

    history = await _fetch_history(rctx)

    entry_handoff_map = workflow.handoffs.get(workflow.entry_node, {})

    # handoff 回调：入口节点选择非 reply 的 outcome 时触发
    async def on_handoff(outcome_name: str, instruction: str) -> str | None:
        """被入口节点的 select_outcome 触发。

        先检查 handoffs 声明，再检查 flow edges。
        返回 str 表示 handoff 成功（结果注入上下文），None 表示非 handoff。
        """
        # 检查点：取消检测
        if await rctx.check_cancelled():
            return "任务已被用户取消。"

        # 1. 检查显式 handoff 声明
        ho_target = entry_handoff_map.get(outcome_name, "")
        if ho_target and not ho_target.startswith("$"):
            await rctx.emit_event("handoff_progress", {
                "message": f"入口节点 handoff → {ho_target}",
            })
            sub_outcome = await _run_subchain(
                rctx=rctx, registry=registry, workflow=workflow,
                start_node_id=ho_target, instruction=instruction,
                history=history, run_id=run_id, depth=0,
            )
            await rctx.emit_event("handoff_progress", {
                "message": f"handoff 完成: {sub_outcome.node_id} → {sub_outcome.outcome}",
            })
            return sub_outcome.text or sub_outcome.summary or "（下游节点未产出内容）"

        # 2. 检查 flow edges（入口节点把 flow edges 也当作 handoff 使用）
        downstream_id = next_node(workflow, workflow.entry_node, outcome_name)
        if not downstream_id or downstream_id.startswith("$"):
            return f"workflow 中 outcome='{outcome_name}' 没有有效的下游节点。"

        await rctx.emit_event("handoff_progress", {
            "message": f"入口节点将处理移交给下游: {downstream_id}",
        })

        sub_outcome = await _run_subchain(
            rctx=rctx, registry=registry, workflow=workflow,
            start_node_id=downstream_id, instruction=instruction,
            history=history, run_id=run_id, depth=0,
        )

        await rctx.emit_event("handoff_progress", {
            "message": f"下游链完成: {sub_outcome.node_id} → {sub_outcome.outcome}",
        })

        return sub_outcome.text or sub_outcome.summary or "（下游节点未产出内容）"

    # 执行入口节点
    await rctx.emit_event("node_started", {
        "node_id": entry_node.id, "node_name": entry_node.name, "hop": 0, "workflow_id": workflow.id,
    })

    rp = resolve_provider(rctx.workspace_root, runtime_cfg, entry_node, rctx.default_model)
    provider = OpenAIProvider(
        http=rctx.llm_http, api_key=rp.api_key or rctx.api_key,
        base_url=rp.base_url or rctx.base_url or None, model=rp.model,
    )
    print(f"[engine] entry node={entry_node.id} model={rp.model}", flush=True)

    outcome = await run_ai_node(
        rctx=rctx, provider=provider, registry=registry, workflow=workflow,
        node=entry_node, instruction=rctx.user_text, history=history,
        run_id=run_id, on_handoff=on_handoff,
        downstream_capabilities=all_caps,
        streaming=get_bool(runtime_cfg, "engine.streaming", False),
    )

    await rctx.emit_event("node_completed", {
        "node_id": outcome.node_id, "node_name": entry_node.name,
        "outcome": outcome.outcome, "summary": outcome.summary, "workflow_id": workflow.id,
    })

    return outcome.text, outcome.outcome


async def _fetch_history(rctx: RunContext, limit: int = 40) -> list[dict[str, Any]]:
    try:
        r = await rctx.http.get(
            f"{rctx.supervisor_url}/v1/sessions/{rctx.session_id}/messages",
            params={"limit": limit},
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return [
                    {"role": str(m.get("role")), "content": str(m.get("content", ""))}
                    for m in data
                    if isinstance(m, dict) and m.get("role") in {"user", "assistant", "system"}
                ]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
#  Worker 主循环
# ---------------------------------------------------------------------------

async def wait_supervisor(
    http: httpx.AsyncClient, base_url: str, *, timeout: float = 2.0, interval: float = 0.5
) -> None:
    print(f"[engine] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await http.get(f"{base_url}/v1/health", timeout=timeout)
            if r.status_code == 200:
                print(f"[engine] supervisor connected", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(interval)


async def worker_loop(*, supervisor_url: str, workspace_root: Path, worker_id: str = "") -> None:
    """统一 worker：轮询 inbound，执行图。"""

    wid = worker_id or str(uuid.uuid4())
    runtime_cfg = load_runtime_config(workspace_root)
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")

    sup_timeout = get_float(runtime_cfg, "engine.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    poll_sec = get_float(runtime_cfg, "engine.poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)
    default_wf_id = get_str(runtime_cfg, "shell.workflow_id", "bootstrap.default_chat").strip() or "bootstrap.default_chat"

    async with (
        httpx.AsyncClient(timeout=sup_timeout, trust_env=False) as http,
        httpx.AsyncClient(timeout=llm_timeout, trust_env=False) as llm_http,
    ):
        await wait_supervisor(http, supervisor_url)

        # 加载 MCP 工具为一等工具
        mcp_count = await registry.load_mcp_tools()
        if mcp_count:
            print(f"[engine] loaded {mcp_count} MCP tools", flush=True)

        while True:
            try:
                # 轮询 inbound
                nr = await http.get(f"{supervisor_url}/v1/inbound/next", params={"worker_id": wid})
                if nr.status_code == 200:
                    item = nr.json()
                    if isinstance(item, dict) and item.get("text"):
                        await _handle_inbound(http, llm_http, supervisor_url, workspace_root, registry, item, default_wf_id, wid)
                        continue

            except Exception as e:
                print(f"[engine] error: {e}", flush=True)

            await asyncio.sleep(poll_sec)


async def _handle_inbound(
    http: httpx.AsyncClient, llm_http: httpx.AsyncClient,
    sup_url: str, ws_root: Path, registry: ToolRegistry,
    item: dict[str, Any], default_wf_id: str, worker_id: str,
) -> None:
    session_id = str(item.get("session_id") or "").strip()
    inbound_seq = item.get("inbound_seq")
    user_text = str(item.get("text") or "").strip()
    wf_id = str(item.get("workflow_id") or "").strip() or default_wf_id

    # 立即 ACK，避免重启后重复处理
    await http.post(
        f"{sup_url}/v1/inbound/{inbound_seq}/ack",
        json={"worker_id": worker_id},
    )

    # 清除上一轮的取消标记，避免新任务被误杀
    try:
        await http.post(f"{sup_url}/v1/sessions/{session_id}/cancel/clear")
    except Exception:
        pass

    cfg_raw = await fetch_openai_secret(http, sup_url)
    api_key, base_url, default_model = normalize_openai_secret(cfg_raw)

    if not api_key:
        await http.post(f"{sup_url}/v1/sessions/{session_id}/outbound",
            json={"text": "OpenAI api_key 未配置。", "source_inbound_seq": inbound_seq})
        return

    wf = load_workflow(ws_root, wf_id)
    if wf is None:
        await http.post(f"{sup_url}/v1/sessions/{session_id}/outbound",
            json={"text": f"Workflow 未找到：{wf_id}", "source_inbound_seq": inbound_seq})
        return

    rctx = RunContext(
        workspace_root=ws_root, supervisor_url=sup_url, session_id=session_id,
        worker_id=worker_id, http=http, llm_http=llm_http,
        api_key=api_key, base_url=base_url, default_model=default_model, user_text=user_text,
    )

    final_text, status = await run_graph(rctx=rctx, registry=registry, workflow=wf)

    # cancelled 时不发 outbound，避免覆盖后续新任务的输出
    if status != "cancelled":
        await http.post(f"{sup_url}/v1/sessions/{session_id}/outbound",
            json={"text": final_text or "（无输出）", "source_inbound_seq": inbound_seq})
