from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import get_float, get_int, get_str, load_runtime_config
from providers.openai import OpenAIProvider


def strip_tool_trace_blocks(text: str) -> str:
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
    return "\n".join(out).strip()


async def wait_supervisor(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    health_timeout_sec: float = 2.0,
    poll_interval_sec: float = 0.5,
) -> None:
    print(f"[shell] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await client.get(f"{base_url}/v1/health", timeout=health_timeout_sec)
            if r.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(poll_interval_sec)


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


async def fetch_openai_config_secret(client: httpx.AsyncClient, supervisor_url: str) -> dict:
    r = await client.get(f"{supervisor_url}/v1/config/openai/secret")
    r.raise_for_status()
    cfg = r.json()
    return cfg if isinstance(cfg, dict) else {}


def _load_text_file(path: Path, max_chars: int = 200_000) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = text.strip()
        if not text:
            return ""
        if len(text) > max_chars:
            return text[:max_chars] + "\n...<truncated>"
        return text
    except Exception:
        return ""


def load_shell_orchestrator_prompt(*, workspace_root: Path) -> str:
    p = workspace_root / "config" / "prompts" / "shell_orchestrator_prompt.txt"
    text = _load_text_file(p)
    if text:
        return text

    # Fallback (should rarely happen because we ship the prompt file).
    return (
        "你是 Clonoth 的 Shell Orchestrator（对话编排 AI）。\n"
        "当需要文件/命令/重启等真实操作时，调用 create_task；否则直接回答。"
    )


def load_task_responder_prompt(*, workspace_root: Path) -> str:
    p = workspace_root / "config" / "prompts" / "task_responder_prompt.txt"
    text = _load_text_file(p)
    if text:
        return text

    # Fallback (should rarely happen because we ship the prompt file).
    return "你是 Clonoth 的 Task Responder。请根据 JSON 输入生成面向用户的最终回复。"


# Shell Orchestrator Worker.
#
# 重要原则：
# - 每一条 inbound message（非空）都必须经过 Orchestrator LLM 路由判断：reply 或 create task。
# - 不允许本地正则/硬编码绕过 LLM 决策。


async def orchestrate(
    *,
    supervisor_http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    supervisor_url: str,
    workspace_root: Path,
    session_id: str,
    use_context: bool,
) -> tuple[str, str]:
    """Return (action, payload).

    action:
      - "reply": payload is assistant reply text
      - "task":  payload is kernel task instruction
    """

    runtime_cfg = load_runtime_config(workspace_root)

    # Fetch recent chat history (canonical) and include it in the orchestrator call.
    # External channel adapters can disable context per-message.
    if use_context:
        history_limit = get_int(runtime_cfg, "shell.orchestrator.history_limit", 40, min_value=5, max_value=200)
    else:
        # Effectively stateless routing: only keep the latest user turn.
        history_limit = 1

    mr = await supervisor_http.get(
        f"{supervisor_url}/v1/sessions/{session_id}/messages",
        params={"limit": history_limit},
    )
    mr.raise_for_status()
    history = mr.json()
    if not isinstance(history, list):
        history = []

    # Remove internal tool traces from assistant messages to avoid confusing the orchestrator.
    cleaned_history: list[dict[str, str]] = []
    for m in history:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str):
            continue
        if role == "assistant":
            content = strip_tool_trace_blocks(content)
            if not content:
                continue
        cleaned_history.append({"role": str(role), "content": content})

    prompt = load_shell_orchestrator_prompt(workspace_root=workspace_root)

    # LLM config
    cfg = await fetch_openai_config_secret(supervisor_http, supervisor_url)
    api_key = _resolve_env_ref(cfg.get("api_key"))
    base_url = _resolve_env_ref(cfg.get("base_url")) or None
    default_model = _resolve_env_ref(cfg.get("model")) or "gpt-4o-mini"

    model_override = get_str(runtime_cfg, "shell.orchestrator.model", "").strip()
    model = model_override or default_model

    if not api_key:
        # No key => cannot call Kernel either. Reply an actionable error.
        return (
            "reply",
            "OpenAI api_key 未配置。\n"
            "请在 .env 中设置 OPENAI_API_KEY，或在 data/config.yaml 中配置 openai.api_key（可引用 ${OPENAI_API_KEY}）。",
        )

    provider = OpenAIProvider(http=llm_http, api_key=api_key, base_url=base_url, model=model)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "create_task",
                "description": "Create a Kernel execution task when real workspace operations are needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "A clear Chinese instruction for Kernel to execute.",
                        }
                    },
                    "required": ["instruction"],
                },
            },
        }
    ]

    resp = await provider.chat(messages=[{"role": "system", "content": prompt}, *cleaned_history], tools=tools)

    if not resp.ok:
        # Important: DO NOT fallback to creating tasks when LLM is failing.
        # Kernel also depends on the same provider and would likely fail too.
        err = resp.error or "unknown error"
        return "reply", f"Orchestrator LLM 调用失败：{err}\n请稍后重试。"

    if resp.tool_calls:
        for tc in resp.tool_calls:
            if tc.name != "create_task":
                continue
            instruction = tc.arguments.get("instruction")
            if isinstance(instruction, str) and instruction.strip():
                return "task", instruction.strip()

            # Tool-call emitted but args are invalid/missing -> fallback to using user text.
            return "task", ""

    # Default: direct reply
    text = (resp.text or "").strip()
    return "reply", text


def _short_text(s: str, max_chars: int) -> str:
    s = s or ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n...<truncated>"


async def _fetch_inbound_text(
    *,
    supervisor_http: httpx.AsyncClient,
    supervisor_url: str,
    session_id: str,
    source_inbound_seq: int | None,
) -> str:
    if source_inbound_seq is None:
        return ""
    try:
        seq = int(source_inbound_seq)
    except Exception:
        return ""
    if seq <= 0:
        return ""

    after = max(0, seq - 1)
    r = await supervisor_http.get(
        f"{supervisor_url}/v1/sessions/{session_id}/events",
        params={"after_seq": after},
    )
    r.raise_for_status()
    evts = r.json()
    if not isinstance(evts, list):
        return ""

    for e in evts:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "inbound_message":
            continue
        try:
            e_seq = int(e.get("seq", 0))
        except Exception:
            continue
        if e_seq != seq:
            continue
        payload = e.get("payload") or {}
        if isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str):
                return text

    return ""


async def _collect_tool_traces_for_task(
    *,
    supervisor_http: httpx.AsyncClient,
    supervisor_url: str,
    session_id: str,
    task_id: str,
    limit: int = 200,
    max_blocks: int = 10,
    max_total_chars: int = 20_000,
) -> list[str]:
    """Collect CLONOTH_TOOL_TRACE blocks for the given task_id."""

    r = await supervisor_http.get(
        f"{supervisor_url}/v1/sessions/{session_id}/messages",
        params={"limit": int(limit)},
    )
    r.raise_for_status()
    msgs = r.json()
    if not isinstance(msgs, list):
        return []

    traces: list[str] = []
    total = 0

    for m in reversed(msgs):
        # scan backwards: newest first
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if "[CLONOTH_TOOL_TRACE v1]" not in content:
            continue
        if f"TASK: {task_id}" not in content:
            continue

        block = content.strip()
        block = _short_text(block, 4000)

        if total + len(block) > max_total_chars:
            break
        traces.append(block)
        total += len(block)

        if len(traces) >= max_blocks:
            break

    # keep original chronological order
    traces.reverse()
    return traces


async def generate_task_final_reply(
    *,
    supervisor_http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    supervisor_url: str,
    workspace_root: Path,
    task: dict[str, Any],
) -> str:
    """Post-process a completed Kernel task result via chat AI."""

    runtime_cfg = load_runtime_config(workspace_root)

    task_id = str(task.get("task_id") or "").strip()
    session_id = str(task.get("session_id") or "").strip()
    instruction = str(task.get("instruction") or "").strip()
    status = str(task.get("status") or "").strip()

    # Result draft from Kernel
    result = task.get("result")
    draft = ""
    if isinstance(result, dict):
        v = result.get("draft")
        if not isinstance(v, str):
            v = result.get("text")
        draft = str(v or "").strip()

    # Retrieve original user message when possible
    source_inbound_seq = task.get("source_inbound_seq")
    user_text = await _fetch_inbound_text(
        supervisor_http=supervisor_http,
        supervisor_url=supervisor_url,
        session_id=session_id,
        source_inbound_seq=int(source_inbound_seq) if isinstance(source_inbound_seq, int) else None,
    )

    # Collect tool traces for this task for more faithful summarization
    tool_traces = await _collect_tool_traces_for_task(
        supervisor_http=supervisor_http,
        supervisor_url=supervisor_url,
        session_id=session_id,
        task_id=task_id,
        limit=get_int(runtime_cfg, "shell.responder.tool_trace_history_limit", 200, min_value=50, max_value=500),
    )

    prompt = load_task_responder_prompt(workspace_root=workspace_root)

    cfg = await fetch_openai_config_secret(supervisor_http, supervisor_url)
    api_key = _resolve_env_ref(cfg.get("api_key"))
    base_url = _resolve_env_ref(cfg.get("base_url")) or None
    default_model = _resolve_env_ref(cfg.get("model")) or "gpt-4o-mini"

    model_override = get_str(runtime_cfg, "shell.responder.model", "").strip()
    model = model_override or default_model

    if not api_key:
        # No LLM available -> fallback to Kernel draft.
        return draft or f"任务已完成（status={status}），但 LLM 未配置，无法生成最终回复。"

    provider = OpenAIProvider(http=llm_http, api_key=api_key, base_url=base_url, model=model)

    inp = {
        "user_text": user_text,
        "task_instruction": instruction,
        "task_status": status,
        "kernel_draft": draft,
        "tool_traces": tool_traces,
    }

    resp = await provider.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(inp, ensure_ascii=False, indent=2)},
        ],
        tools=None,
    )

    if not resp.ok:
        err = resp.error or "unknown error"
        # fallback to draft
        return draft or f"任务已完成（status={status}），但生成最终回复失败：{err}"

    text = (resp.text or "").strip()
    text = strip_tool_trace_blocks(text)
    return text or draft or "（无输出）"


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Clonoth Shell Orchestrator Worker")
    parser.add_argument(
        "--supervisor",
        default=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765"),
        help="Supervisor base URL",
    )
    parser.add_argument("--worker-id", default=os.getenv("CLONOTH_SHELL_WORKER_ID") or str(uuid.uuid4()))
    args = parser.parse_args()

    base_url = args.supervisor.rstrip("/")
    worker_id = str(args.worker_id or "").strip() or str(uuid.uuid4())

    workspace_root = Path(__file__).resolve().parents[1]
    runtime_cfg = load_runtime_config(workspace_root)

    supervisor_timeout_sec = get_float(runtime_cfg, "shell.http.client_timeout_sec", 10.0, min_value=1.0, max_value=120.0)
    llm_timeout_sec = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)

    health_timeout_sec = get_float(runtime_cfg, "shell.supervisor.health_timeout_sec", 2.0, min_value=0.5, max_value=30.0)
    wait_poll_interval_sec = get_float(
        runtime_cfg,
        "shell.supervisor.wait_poll_interval_sec",
        0.5,
        min_value=0.1,
        max_value=5.0,
    )
    events_poll_interval_sec = get_float(runtime_cfg, "shell.events_poll_interval_sec", 0.5, min_value=0.1, max_value=10.0)

    # Worker main loop
    lease_sec = 30.0

    # trust_env=False: 避免环境代理变量影响本地 127.0.0.1 通信
    async with (
        httpx.AsyncClient(timeout=supervisor_timeout_sec, trust_env=False) as supervisor_http,
        httpx.AsyncClient(timeout=llm_timeout_sec, trust_env=False) as llm_http,
    ):
        await wait_supervisor(
            supervisor_http,
            base_url,
            health_timeout_sec=health_timeout_sec,
            poll_interval_sec=wait_poll_interval_sec,
        )
        print(f"[shell-worker] connected to supervisor: {base_url} worker_id={worker_id}", flush=True)

        async def inbound_loop() -> None:
            while True:
                try:
                    nr = await supervisor_http.get(
                        f"{base_url}/v1/inbound/next",
                        params={"worker_id": worker_id, "lease_sec": lease_sec},
                    )

                    if nr.status_code == 204:
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    nr.raise_for_status()
                    item = nr.json()
                    if not isinstance(item, dict):
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    inbound_seq = int(item.get("inbound_seq", 0) or 0)
                    session_id = str(item.get("session_id") or "")
                    text = str(item.get("text") or "").strip()
                    use_context = bool(item.get("use_context", True))

                    if inbound_seq <= 0 or not session_id:
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    if not text:
                        # nothing to do but ack
                        ar = await supervisor_http.post(
                            f"{base_url}/v1/inbound/{inbound_seq}/ack",
                            json={"worker_id": worker_id},
                        )
                        ar.raise_for_status()
                        continue

                    action, payload = await orchestrate(
                        supervisor_http=supervisor_http,
                        llm_http=llm_http,
                        supervisor_url=base_url,
                        workspace_root=workspace_root,
                        session_id=session_id,
                        use_context=use_context,
                    )

                    if action == "reply":
                        cleaned = strip_tool_trace_blocks(payload)
                        if not cleaned:
                            cleaned = "（无输出）"
                        rr = await supervisor_http.post(
                            f"{base_url}/v1/sessions/{session_id}/outbound",
                            json={"text": cleaned, "source_inbound_seq": inbound_seq},
                        )
                        if rr.status_code != 409:
                            rr.raise_for_status()
                        else:
                            print(f"[shell-worker] outbound conflict (seq={inbound_seq}): {rr.text}", flush=True)
                    else:
                        instruction = payload.strip() if payload.strip() else text
                        tr = await supervisor_http.post(
                            f"{base_url}/v1/tasks",
                            json={
                                "session_id": session_id,
                                "instruction": instruction,
                                "priority": 0,
                                "context": {},
                                "source_inbound_seq": inbound_seq,
                                "use_context": use_context,
                            },
                        )

                        if tr.status_code != 409:
                            tr.raise_for_status()
                        else:
                            print(f"[shell-worker] task conflict (seq={inbound_seq}): {tr.text}", flush=True)

                    ackr = await supervisor_http.post(
                        f"{base_url}/v1/inbound/{inbound_seq}/ack",
                        json={"worker_id": worker_id},
                    )
                    ackr.raise_for_status()

                except Exception as e:
                    print(f"[shell-worker] inbound error: {e}", flush=True)
                    await asyncio.sleep(events_poll_interval_sec)

        async def task_result_loop() -> None:
            while True:
                try:
                    nr = await supervisor_http.get(
                        f"{base_url}/v1/task_results/next",
                        params={"worker_id": worker_id, "lease_sec": lease_sec},
                    )
                    if nr.status_code == 204:
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    nr.raise_for_status()
                    task = nr.json()
                    if not isinstance(task, dict):
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    task_id = str(task.get("task_id") or "").strip()
                    session_id = str(task.get("session_id") or "").strip()
                    if not task_id or not session_id:
                        await asyncio.sleep(events_poll_interval_sec)
                        continue

                    final_text = await generate_task_final_reply(
                        supervisor_http=supervisor_http,
                        llm_http=llm_http,
                        supervisor_url=base_url,
                        workspace_root=workspace_root,
                        task=task,
                    )
                    final_text = strip_tool_trace_blocks(final_text)
                    if not final_text:
                        final_text = "（无输出）"

                    rr = await supervisor_http.post(
                        f"{base_url}/v1/sessions/{session_id}/outbound",
                        json={"text": final_text, "source_task_id": task_id},
                    )
                    rr.raise_for_status()

                except Exception as e:
                    print(f"[shell-worker] task-result error: {e}", flush=True)
                    await asyncio.sleep(events_poll_interval_sec)

        await asyncio.gather(inbound_loop(), task_result_loop())


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
