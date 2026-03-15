from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import (
    fetch_openai_secret,
    get_bool,
    get_float,
    get_str,
    load_runtime_config,
    normalize_openai_secret,
)
from providers.openai import OpenAIProvider
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

from .ai_step import run_ai_node
from .context import RunContext
from .graph import Workflow, load_workflow
from .model import resolve_provider
from .node import Node, load_node
from .tool_step import result_to_raw, summarize_result, write_artifact


def _collect_own_tool_capabilities(node: "Node", registry: ToolRegistry) -> str:
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
    lines = []
    for s in tool_specs:
        name = s["name"]
        desc = s.get("description", "")
        lines.append(f"- {name}：{desc}" if desc else f"- {name}")
    return (
        "## 你的直接工具\n\n"
        "以下工具已通过 function calling 提供给你，你可以直接发起调用。调用后系统会把它们调度为 task 执行，再把结果返回给你。\n\n"
        + "\n".join(lines)
    )


def _collect_downstream_capabilities(workspace_root: Path, workflow: Workflow, registry: ToolRegistry, node_id: str) -> str:
    edges = workflow.edges.get(node_id, {})
    handoffs = workflow.handoffs.get(node_id, {})
    merged = {**edges, **handoffs}
    all_specs = registry.list_specs()
    all_tool_names = [s["name"] for s in all_specs]
    lines: list[str] = []
    seen_nodes: set[str] = set()
    all_names = {s["name"] for s in all_specs}
    for _outcome_name, target_id in merged.items():
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
            tool_names = [n for n in node.tool_access.allow if n in all_names]
        else:
            tool_names = []
        desc = node.description or node.name
        tool_list = ", ".join(tool_names) if tool_names else "（无工具）"
        lines.append(f"- {node.name}（{node.id}）：{desc}\n  可用工具：{tool_list}")
    if not lines:
        return ""
    return "## 下游节点能力\n\n通过 outcome 你可以把处理交给以下下游节点：\n\n" + "\n".join(lines)


async def _fetch_history(rctx: RunContext, limit: int = 40) -> list[dict[str, Any]]:
    try:
        r = await rctx.http.get(
            f"{rctx.supervisor_url}/v1/sessions/{rctx.session_id}/messages",
            params={"limit": limit},
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                result = []
                for m in data:
                    if not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "system"}:
                        continue
                    content = m.get("content", "")
                    if isinstance(content, list):
                        result.append({"role": str(m.get("role")), "content": content})
                    else:
                        result.append({"role": str(m.get("role")), "content": str(content)})
                return result
    except Exception:
        pass
    return []


async def wait_supervisor(
    http: httpx.AsyncClient,
    base_url: str,
    *,
    timeout: float = 2.0,
    interval: float = 0.5,
) -> None:
    print(f"[engine] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await http.get(f"{base_url}/v1/health", timeout=timeout)
            if r.status_code == 200:
                print("[engine] supervisor connected", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(interval)


async def worker_loop(*, supervisor_url: str, workspace_root: Path, worker_id: str = "") -> None:
    wid = worker_id or str(uuid.uuid4())
    runtime_cfg = load_runtime_config(workspace_root)
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")
    _last_reload_seq = 0

    sup_timeout = get_float(runtime_cfg, "engine.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    poll_sec = get_float(runtime_cfg, "engine.poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)

    async with (
        httpx.AsyncClient(timeout=sup_timeout, trust_env=False) as http,
        httpx.AsyncClient(timeout=llm_timeout, trust_env=False) as llm_http,
    ):
        await wait_supervisor(http, supervisor_url)
        mcp_count = await registry.load_mcp_tools()
        if mcp_count:
            print(f"[engine] loaded {mcp_count} MCP tools", flush=True)

        while True:
            try:
                # 检查工具热重载信号
                try:
                    rr = await http.get(f"{supervisor_url}/v1/tools/reload-seq")
                    if rr.status_code == 200:
                        new_seq = int(rr.json().get("seq", 0))
                        if new_seq > _last_reload_seq:
                            _last_reload_seq = new_seq
                            count = registry.reload()
                            print(f"[engine] tools reloaded ({count} tools)", flush=True)
                except Exception:
                    pass

                nr = await http.get(f"{supervisor_url}/v1/tasks/next", params={"worker_id": wid})
                if nr.status_code == 200:
                    item = nr.json()
                    if isinstance(item, dict) and item.get("task_id"):
                        await _handle_task(http, llm_http, supervisor_url, workspace_root, registry, item, wid)
                        continue
            except Exception as e:
                print(f"[engine] error: {e}", flush=True)
            await asyncio.sleep(poll_sec)


async def _handle_task(
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    item: dict[str, Any],
    worker_id: str,
) -> None:
    task_id = str(item.get("task_id") or "").strip()
    kind = str(item.get("kind") or "").strip()
    session_id = str(item.get("session_id") or "").strip()
    session_generation = int(item.get("session_generation") or 0)
    workflow_id = str(item.get("workflow_id") or "bootstrap.default_chat").strip() or "bootstrap.default_chat"
    source_inbound_seq = item.get("source_inbound_seq")

    cfg_raw = await fetch_openai_secret(http, sup_url)
    api_key, base_url, default_model = normalize_openai_secret(cfg_raw)

    if kind == "node":
        result = await _run_node_task(
            http=http,
            llm_http=llm_http,
            sup_url=sup_url,
            ws_root=ws_root,
            registry=registry,
            task=item,
            worker_id=worker_id,
            session_id=session_id,
            session_generation=session_generation,
            workflow_id=workflow_id,
            api_key=api_key,
            base_url=base_url,
            default_model=default_model,
        )
    elif kind == "tool":
        result = await _run_tool_task(
            http=http,
            sup_url=sup_url,
            ws_root=ws_root,
            registry=registry,
            task=item,
            worker_id=worker_id,
            session_id=session_id,
            session_generation=session_generation,
            task_id=task_id,
        )
    else:
        result = {"status": "failed", "outcome": "failed", "text": f"未知 task kind: {kind}"}

    await http.post(
        f"{sup_url}/v1/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "result": result},
    )


async def _run_node_task(
    *,
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    workflow_id: str,
    api_key: str,
    base_url: str,
    default_model: str,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    node_id = str(task.get("node_id") or "").strip()
    source_inbound_seq = task.get("source_inbound_seq")
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}

    wf = load_workflow(ws_root, workflow_id)
    if wf is None:
        return {"status": "failed", "outcome": "failed", "text": f"Workflow 未找到：{workflow_id}"}
    node = load_node(ws_root, node_id)
    if node is None:
        return {"status": "failed", "outcome": "failed", "text": f"节点未找到：{node_id}"}
    if not api_key:
        return {"status": "failed", "outcome": "failed", "text": "OpenAI api_key 未配置。"}

    rctx = RunContext(
        workspace_root=ws_root,
        supervisor_url=sup_url,
        session_id=session_id,
        worker_id=worker_id,
        http=http,
        llm_http=llm_http,
        api_key=api_key,
        base_url=base_url,
        default_model=default_model,
        user_text=str(input_data.get("instruction") or "").strip(),
        task_id=task_id,
        session_generation=session_generation,
    )

    history = []
    context_ref = str(input_data.get("context_ref") or "").strip()
    if not context_ref:
        history = await _fetch_history(rctx)

    own_caps = _collect_own_tool_capabilities(node, registry)
    downstream_caps = _collect_downstream_capabilities(ws_root, wf, registry, node.id)

    runtime_cfg = load_runtime_config(ws_root)
    rp = resolve_provider(ws_root, runtime_cfg, node, default_model)
    provider = OpenAIProvider(
        http=llm_http,
        api_key=rp.api_key or api_key,
        base_url=rp.base_url or base_url or None,
        model=rp.model,
    )

    await rctx.emit_event("node_started", {
        "task_id": task_id,
        "node_id": node.id,
        "node_name": node.name,
        "workflow_id": wf.id,
    })

    input_attachments = input_data.get("attachments") if isinstance(input_data.get("attachments"), list) else None

    outcome = await run_ai_node(
        rctx=rctx,
        provider=provider,
        registry=registry,
        workflow=wf,
        node=node,
        instruction=str(input_data.get("instruction") or "").strip(),
        history=history,
        run_id=task_id,
        context_ref=context_ref,
        resume_data=input_data.get("resume_data") if isinstance(input_data.get("resume_data"), dict) else None,
        own_tools_text=own_caps,
        downstream_capabilities=downstream_caps,
        streaming=bool(node.id == wf.entry_node and get_bool(runtime_cfg, "engine.streaming", False)),
        attachments=input_attachments,
    )

    await rctx.emit_event("node_completed", {
        "task_id": task_id,
        "node_id": node.id,
        "node_name": node.name,
        "outcome": outcome.outcome,
        "summary": outcome.summary,
        "workflow_id": wf.id,
        "source_inbound_seq": source_inbound_seq,
    })
    return outcome.to_dict()


async def _run_tool_task(
    *,
    http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    task_id: str,
) -> dict[str, Any]:
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
    tool_name = str(task.get("tool_name") or "").strip() or str(input_data.get("tool_name") or "").strip()
    arguments = dict(input_data.get("arguments") or {})

    kctx = ToolContext(
        supervisor_url=sup_url,
        session_id=session_id,
        run_id=task_id,
        worker_id=worker_id,
        workspace_root=ws_root,
        http=http,
        registry=registry,
        task_id=task_id,
        session_generation=session_generation,
        approval_poll_interval_sec=0.5,
    )

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] 开始执行 {tool_name}",
        "task_id": task_id,
        "tool_name": tool_name,
    })

    result = await registry.execute(name=tool_name, arguments=arguments, ctx=kctx)
    if isinstance(result, dict) and result.get("cancelled"):
        await kctx.emit_event("cancel_acknowledged", {"task_id": task_id, "tool_name": tool_name})
        return {
            "status": "cancelled",
            "tool_name": tool_name,
            "arguments": arguments,
            "summary": "任务已取消",
            "format": "json",
            "raw_inline": json.dumps(result, ensure_ascii=False),
            "truncated": False,
            "ref": "",
        }

    # 提取工具结果中的附件（如图片路径）
    tool_attachments = result.get("attachments") if isinstance(result, dict) and isinstance(result.get("attachments"), list) else []

    summary = summarize_result(tool_name, result)
    fmt, raw = result_to_raw(tool_name, result)
    max_inline = 8000
    truncated = len(raw) > max_inline
    ref = ""
    if truncated:
        ref = await write_artifact(ws_root, task_id, str(input_data.get("call_id") or task_id), tool_name, fmt, raw)
    raw_inline = raw if not truncated else raw[:max_inline] + "\n...<truncated>"

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] {tool_name}: {summary}",
        "task_id": task_id,
        "tool_name": tool_name,
    })

    return {
        "status": "completed",
        "tool_name": tool_name,
        "arguments": arguments,
        "summary": summary,
        "format": fmt,
        "raw_inline": raw_inline,
        "truncated": truncated,
        "ref": ref,
        "attachments": tool_attachments,
    }
