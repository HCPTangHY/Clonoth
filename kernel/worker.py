from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from clonoth_runtime import get_float, get_int, load_runtime_config

from providers.openai import OpenAIProvider

from .context import KernelContext
from .prompts import load_kernel_system_prompt
from .registry import ToolRegistry


async def wait_supervisor(
    http: httpx.AsyncClient,
    base_url: str,
    *,
    health_timeout_sec: float = 2.0,
    poll_interval_sec: float = 0.5,
) -> None:
    print(f"[kernel] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await http.get(f"{base_url}/v1/health", timeout=health_timeout_sec)
            if r.status_code == 200:
                print(f"[kernel] connected to supervisor: {base_url}", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(poll_interval_sec)


def to_openai_tools(tool_specs: list[dict]) -> list[dict]:
    tools: list[dict] = []
    for spec in tool_specs:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get(
                        "input_schema",
                        {"type": "object", "properties": {}, "required": []},
                    ),
                },
            }
        )
    return tools


def _short_text(s: str, max_chars: int) -> str:
    s = s or ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n...<truncated>"


def _short_json(obj: Any, max_chars: int) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return _short_text(s, max_chars)


def _sanitize_filename(s: str) -> str:
    s = (s or "").strip() or "x"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:80]


def _load_repeat_tool_call_threshold(workspace_root: Path, default: int = 3) -> int:
    """Load repeat-call guard threshold from data/policy.yaml.

    Config path:
        kernel_loop_guard.repeat_tool_call_threshold
    """

    p = workspace_root / "data" / "policy.yaml"
    try:
        if not p.exists():
            return default
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        sec = data.get("kernel_loop_guard")
        if not isinstance(sec, dict):
            return default
        thr = sec.get("repeat_tool_call_threshold")
        if isinstance(thr, int) and thr > 0:
            return thr
        return default
    except Exception:
        return default


def _tool_result_to_raw(tool_name: str, tool_args: dict[str, Any], result: Any) -> tuple[str, str]:
    """Convert tool result to raw text for LLM observation.

    Returns (format, raw_text).
    """

    # Special-case read_file: returning JSON will escape newlines and be unreadable.
    if tool_name == "read_file" and isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return "text", content

    # execute_command-like output is also better as plain text
    if isinstance(result, dict) and "returncode" in result and isinstance(result.get("output"), str):
        out = str(result.get("output") or "")
        if out.strip():
            rc = result.get("returncode")
            return "text", f"returncode={rc}\n" + out

    # Default JSON
    try:
        raw = json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        raw = str(result)
    return "json", raw


def _summarize_tool_result(tool_name: str, tool_args: dict[str, Any], result: Any) -> str:
    """Small summary for chat/progress (NOT for LLM reasoning)."""

    if not isinstance(result, dict):
        return "已获得工具结果"

    if result.get("ok") is False:
        return f"失败：{result.get('error') or 'unknown error'}"

    if tool_name == "list_dir":
        path = str(result.get("path") or tool_args.get("path") or "")
        items = result.get("items")
        if isinstance(items, list):
            names = []
            for it in items[:10]:
                if isinstance(it, dict) and isinstance(it.get("name"), str):
                    names.append(it["name"])
            suffix = ("，" + ", ".join(names)) if names else ""
            return f"已列出目录 {path}（{len(items)} 项）{suffix}"
        return f"已列出目录 {path}"

    if tool_name == "search_in_files":
        q = str(result.get("query") or tool_args.get("query") or "")
        matches = result.get("matches")
        if isinstance(matches, list):
            paths = []
            for m in matches[:10]:
                if isinstance(m, dict) and isinstance(m.get("path"), str):
                    paths.append(m["path"])
            suffix = ("，" + ", ".join(paths)) if paths else ""
            return f"已搜索关键词 {q}（{len(matches)} 个匹配）{suffix}"
        return f"已搜索关键词 {q}"

    if tool_name == "read_file":
        path = str(result.get("path") or tool_args.get("path") or "")
        content = str(result.get("content") or "")
        lines = content.count("\n") + (1 if content else 0)
        return f"已读取文件 {path}（约 {lines} 行）"

    if tool_name == "execute_command":
        rc = result.get("returncode")
        return f"命令执行完成（returncode={rc}）"

    if tool_name == "write_file":
        path = str(result.get("path") or tool_args.get("path") or "")
        b = result.get("bytes")
        return f"已写入文件 {path}（{b} bytes）"

    if tool_name == "create_or_update_tool":
        path = str(result.get("path") or "")
        reloaded = result.get("reloaded")
        return f"已更新工具 {path}（registry tools={reloaded}）"

    if tool_name == "reload_tools":
        n = result.get("tools")
        return f"已重载 tools（count={n}）"

    if tool_name == "request_restart":
        target = str(result.get("target") or tool_args.get("target") or "")
        return f"已请求重启：{target}"

    return "已获得工具结果"


def _format_tool_trace(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = ["[CLONOTH_TOOL_TRACE v1]"]

    for e in entries:
        lines.append(f"TOOL_CALL: {e['name']} {e['args_json']}")
        lines.append(f"TOOL_RESULT_FORMAT: {e['raw_format']}")
        lines.append(f"TOOL_RESULT_TRUNCATED: {str(bool(e.get('truncated'))).lower()}")

        ref = (e.get("ref") or "").strip()
        lines.append(f"TOOL_RESULT_REF: {ref}")

        raw_inline = (e.get("raw_inline") or "").rstrip()
        if raw_inline:
            lines.append("TOOL_RESULT_RAW: |")
            for ln in raw_inline.splitlines():
                lines.append("  " + ln)
        else:
            lines.append("TOOL_RESULT_RAW: <empty>")

        lines.append(f"TOOL_RESULT_SUMMARY: {e['summary']}")
        lines.append("-")

    lines.append("[/CLONOTH_TOOL_TRACE]")
    return "\n".join(lines)


def _strip_tool_trace_blocks(text: str) -> str:
    """Remove internal CLONOTH_TOOL_TRACE blocks from model output.

    说明：
    - CLONOTH_TOOL_TRACE 是系统内部观测数据。
    - 即使 prompt 已约束，模型仍可能误把它输出给用户；这里做防御性剥离。
    """

    if not text:
        return ""

    out: list[str] = []
    in_block = False
    for ln in text.splitlines():
        if "[CLONOTH_TOOL_TRACE v1]" in ln:
            in_block = True
            continue
        if in_block:
            if "[/CLONOTH_TOOL_TRACE]" in ln:
                in_block = False
            continue
        out.append(ln)

    cleaned = "\n".join(out).strip()
    return cleaned


async def _write_artifact(
    *,
    workspace_root: Path,
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    raw_format: str,
    raw_text: str,
) -> str:
    artifacts_dir = workspace_root / "data" / "artifacts" / task_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    ext = "json" if raw_format == "json" else "txt"
    fname = f"{_sanitize_filename(tool_name)}_{_sanitize_filename(tool_call_id)}.{ext}"
    path = artifacts_dir / fname
    path.write_text(raw_text, encoding="utf-8")

    return path.relative_to(workspace_root).as_posix()


async def run_task(
    *,
    http: httpx.AsyncClient,
    base_url: str,
    worker_id: str,
    provider: OpenAIProvider,
    registry: ToolRegistry,
    task: dict,
) -> str:
    session_id = task["session_id"]
    task_id = task["task_id"]
    instruction = task["instruction"]

    workspace_root = Path(__file__).resolve().parents[1]
    runtime_cfg = load_runtime_config(workspace_root)

    history_limit = get_int(runtime_cfg, "kernel.history_limit", 60, min_value=10, max_value=400)
    max_steps = get_int(runtime_cfg, "kernel.max_steps", 24, min_value=4, max_value=200)
    max_progress_arg_chars = get_int(
        runtime_cfg,
        "kernel.tool_trace.max_progress_arg_chars",
        240,
        min_value=80,
        max_value=4000,
    )
    max_inline_tool_result_chars = get_int(
        runtime_cfg,
        "kernel.tool_trace.max_inline_chars",
        6000,
        min_value=1000,
        max_value=50000,
    )
    approval_poll_interval_sec = get_float(
        runtime_cfg,
        "kernel.approval_poll_interval_sec",
        0.5,
        min_value=0.1,
        max_value=10.0,
    )

    ctx = KernelContext(
        supervisor_url=base_url,
        session_id=session_id,
        task_id=task_id,
        worker_id=worker_id,
        workspace_root=workspace_root,
        http=http,
        registry=registry,
        approval_poll_interval_sec=approval_poll_interval_sec,
    )

    # fetch conversation context (canonical)
    history = []
    use_context = task.get("use_context", True)
    if use_context and history_limit > 0:
        mr = await http.get(
            f"{base_url}/v1/sessions/{session_id}/messages",
            params={"limit": history_limit},
        )
        if mr.status_code == 200:
            history = mr.json()
            if not isinstance(history, list):
                history = []

    system_prompt = load_kernel_system_prompt(workspace_root=workspace_root)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append(
        {
            "role": "system",
            "content": f"当前 task_id={task_id}。请完成用户最新需求：{instruction}",
        }
    )

    await ctx.emit_event("task_progress", {"message": f"开始处理：{instruction}"})

    repeat_threshold = _load_repeat_tool_call_threshold(ctx.workspace_root, default=3)
    repeat_counts: dict[str, int] = {}
    for _step in range(max_steps):
        tools = to_openai_tools(registry.list_specs())
        resp = await provider.chat(messages=messages, tools=tools)

        # Rust-like provider: explicit ok/error instead of raising.
        if not resp.ok:
            err = resp.error or "unknown error"
            sc = f" (status={resp.status_code})" if resp.status_code else ""
            msg = f"LLM 调用失败{sc}：{err}"
            await ctx.emit_event("task_progress", {"message": msg})
            return msg

        if resp.tool_calls:
            # If model emitted helper text alongside tool calls, keep it.
            if resp.text and resp.text.strip():
                messages.append({"role": "assistant", "content": resp.text.strip()})

            trace_entries: list[dict[str, Any]] = []

            for tc in resp.tool_calls:
                # Repeat-call guard: identical tool calls beyond threshold usually mean the model is stuck.
                try:
                    canon_args = json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)
                except Exception:
                    canon_args = str(tc.arguments)
                fp = f"{tc.name}:{canon_args}"
                repeat_counts[fp] = repeat_counts.get(fp, 0) + 1
                if repeat_counts[fp] > repeat_threshold:
                    msg = (
                        "检测到重复工具调用超过阈值，已中止任务以避免死循环。\n"
                        f"tool={tc.name} repeat_count={repeat_counts[fp]} threshold={repeat_threshold}"
                    )
                    await ctx.emit_event("task_progress", {"message": msg})
                    return msg

                args_json = _short_json(tc.arguments, max_progress_arg_chars)
                await ctx.emit_event("task_progress", {"message": f"调用工具：{tc.name} {args_json}"})

                await ctx.emit_event(
                    "tool_call",
                    {"tool_call_id": tc.id, "name": tc.name, "arguments": tc.arguments},
                )

                result = await registry.execute(name=tc.name, arguments=tc.arguments, ctx=ctx)

                summary = _summarize_tool_result(tc.name, tc.arguments, result)
                raw_format, raw_text = _tool_result_to_raw(tc.name, tc.arguments, result)

                # Always persist the full raw result as artifact (for debugging and for LLM to re-read if needed)
                ref = await _write_artifact(
                    workspace_root=ctx.workspace_root,
                    task_id=task_id,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    raw_format=raw_format,
                    raw_text=raw_text,
                )

                truncated = len(raw_text) > max_inline_tool_result_chars
                raw_inline = raw_text if not truncated else _short_text(raw_text, max_inline_tool_result_chars)

                # Event stream: summary only (+ ref)
                await ctx.emit_event(
                    "tool_result",
                    {
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "summary": summary,
                        "ref": ref,
                        "truncated": truncated,
                    },
                )

                await ctx.emit_event("task_progress", {"message": f"工具完成：{tc.name} -> {summary}"})

                trace_entries.append(
                    {
                        "name": tc.name,
                        "args_json": args_json,
                        "summary": summary,
                        "raw_format": raw_format,
                        "raw_inline": raw_inline,
                        "truncated": truncated,
                        "ref": ref,
                    }
                )

            # Observation (canonical): feed RAW tool results to the model.
            messages.append({"role": "assistant", "content": _format_tool_trace(trace_entries)})
            continue

        text = (resp.text or "").strip()
        if text:
            cleaned = _strip_tool_trace_blocks(text)
            if cleaned != text:
                await ctx.emit_event("task_progress", {"message": "已过滤内部 CLONOTH_TOOL_TRACE 输出"})

            if cleaned:
                await ctx.emit_event("task_progress", {"message": "已生成最终回复"})
                return cleaned

            # 模型只输出了内部 trace（或空白），强制其再给一次面向用户的最终回复。
            messages.append(
                {
                    "role": "system",
                    "content": "请只输出面向用户的最终答复（自然语言），不要包含任何 [CLONOTH_TOOL_TRACE] 块，也不要输出 JSON。",
                }
            )
            continue

    return "我尝试了多步但仍未得到最终答案（max_steps reached）。"


def _resolve_env_ref(raw: str | None) -> str:
    """Resolve a config value.

    Supports simple env interpolation:
    - "${OPENAI_API_KEY}" -> read from environment
    """

    if raw is None:
        return ""
    s = str(raw).strip()
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        var = s[2:-1]
        return (os.getenv(var) or "").strip()
    return s


async def fetch_openai_config(http: httpx.AsyncClient, supervisor_url: str) -> dict:
    r = await http.get(f"{supervisor_url}/v1/config/openai/secret")
    r.raise_for_status()
    cfg = r.json()
    if not isinstance(cfg, dict):
        return {}
    return cfg


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Clonoth Kernel Worker")
    parser.add_argument(
        "--supervisor",
        default=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765"),
        help="Supervisor base URL",
    )
    parser.add_argument("--worker-id", default=os.getenv("CLONOTH_WORKER_ID") or str(uuid.uuid4()))
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
    )

    args = parser.parse_args()

    base_url = args.supervisor.rstrip("/")
    worker_id = args.worker_id

    workspace_root = Path(__file__).resolve().parents[1]
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")

    runtime_cfg = load_runtime_config(workspace_root)
    client_timeout_sec = get_float(runtime_cfg, "kernel.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout_sec = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    health_timeout_sec = get_float(runtime_cfg, "kernel.supervisor.health_timeout_sec", 2.0, min_value=0.5, max_value=30.0)
    wait_poll_interval_sec = get_float(
        runtime_cfg,
        "kernel.supervisor.wait_poll_interval_sec",
        0.5,
        min_value=0.1,
        max_value=5.0,
    )

    def _resolve_poll_interval_sec() -> float:
        if args.poll_interval is not None:
            return float(args.poll_interval)
        env_val = (os.getenv("CLONOTH_POLL_INTERVAL") or "").strip()
        if env_val:
            try:
                return float(env_val)
            except Exception:
                pass

        cfg_now = load_runtime_config(workspace_root)
        return get_float(cfg_now, "kernel.task_poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)

    # trust_env=False: 避免环境代理变量影响本地 127.0.0.1 通信
    async with (
        httpx.AsyncClient(timeout=client_timeout_sec, trust_env=False) as http,
        httpx.AsyncClient(timeout=llm_timeout_sec, trust_env=False) as llm_http,
    ):
        await wait_supervisor(
            http,
            base_url,
            health_timeout_sec=health_timeout_sec,
            poll_interval_sec=wait_poll_interval_sec,
        )

        while True:
            current_task_id = None
            try:
                r = await http.get(f"{base_url}/v1/tasks/next", params={"worker_id": worker_id})
                if r.status_code == 204:
                    await asyncio.sleep(_resolve_poll_interval_sec())
                    continue
                r.raise_for_status()
                task = r.json()
                current_task_id = task.get("task_id")

                print(
                    f"[kernel] picked task {task.get('task_id')} session={task.get('session_id')}",
                    flush=True,
                )

                cfg = await fetch_openai_config(http, base_url)
                api_key = _resolve_env_ref(cfg.get("api_key"))
                model = _resolve_env_ref(cfg.get("model")) or "gpt-4o-mini"
                openai_base_url = _resolve_env_ref(cfg.get("base_url")) or "https://api.openai.com/v1"

                if not api_key:
                    text = (
                        "OpenAI api_key 未配置（环境变量中也未找到）。\n"
                        "请确保以下任一条件满足：\n"
                        "1. 在当前终端/环境中设置了正确的环境变量（如 OPENAI_API_KEY，且在 data/config.yaml 中配置了引用如 ${OPENAI_API_KEY}）。\n"
                        "2. 在 data/config.yaml 中直接填入了 openai.api_key。\n"
                        "3. 调用 Supervisor API：POST http://127.0.0.1:8765/v1/config/openai { api_key, base_url?, model? }"
                    )
                else:
                    provider = OpenAIProvider(http=llm_http, api_key=api_key, base_url=openai_base_url, model=model)
                    text = await run_task(
                        http=http,
                        base_url=base_url,
                        worker_id=worker_id,
                        provider=provider,
                        registry=registry,
                        task=task,
                    )

                # Do NOT emit outbound_message directly from Kernel.
                # Kernel only provides a draft result; Shell (chat AI) will post-process
                # and produce the final user-facing outbound_message.
                cr = await http.post(
                    f"{base_url}/v1/tasks/{task['task_id']}/complete",
                    json={"status": "done", "result": {"draft": text}},
                )
                cr.raise_for_status()

                print(f"[kernel] completed task {task.get('task_id')}", flush=True)

            except Exception as e:
                print(f"[kernel] error: {e}")
                if current_task_id:
                    try:
                        cr = await http.post(
                            f"{base_url}/v1/tasks/{current_task_id}/complete",
                            json={
                                "status": "failed",
                                "result": {
                                    "draft": f"系统出现严重内部错误，任务意外中止。\n```\n{e}\n```",
                                    "error": str(e),
                                },
                            },
                        )
                        cr.raise_for_status()
                    except Exception as inner_e:
                        print(f"[kernel] failed to report error: {inner_e}")

                await asyncio.sleep(_resolve_poll_interval_sec())


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
