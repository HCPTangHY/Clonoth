from __future__ import annotations

import json
import os
import pprint
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from .context import KernelContext


_SENSITIVE_ENV_KEYS_UPPER = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Reserved names: built-in meta tools (cannot be overridden by dynamic tools)
_RESERVED_TOOL_NAMES = {
    "list_dir",
    "read_file",
    "write_file",
    "execute_command",
    "search_in_files",
    "create_or_update_tool",
    "reload_tools",
    "request_restart",
}


def _safe_subprocess_env() -> dict[str, str]:
    """Return a subprocess env with common secret variables stripped.

    Rationale:
    - We want to keep provider keys in environment variables.
    - But we do NOT want arbitrary `execute_command` calls to be able to read/exfiltrate them.

    This is not a complete security boundary, but it meaningfully reduces accidental leakage.
    """

    env = os.environ.copy()
    for k in list(env.keys()):
        ku = k.upper()
        if ku in _SENSITIVE_ENV_KEYS_UPPER:
            env.pop(k, None)
            continue
        if ku.endswith("_API_KEY"):
            env.pop(k, None)
            continue
    return env


def _resolve_under_root(root: Path, rel_path: str) -> Path:
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace root")
    return p


def _run_capture(*, args: list[str], cwd: Path, timeout_sec: float = 10.0) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=_safe_subprocess_env(),
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out.strip()
    except Exception as e:
        return 999, str(e)


def _write_text_artifact(*, ctx: KernelContext, filename: str, text: str) -> str:
    artifacts_dir = ctx.workspace_root / "data" / "artifacts" / ctx.task_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    p = artifacts_dir / filename
    p.write_text(text or "", encoding="utf-8", errors="ignore")
    return p.relative_to(ctx.workspace_root).as_posix()


def _git_snapshot(ctx: KernelContext) -> dict[str, Any]:
    root = ctx.workspace_root

    runtime_cfg = load_runtime_config(root)
    diff_max_chars = get_int(runtime_cfg, "meta.git.diff_max_chars", 400_000, min_value=10_000, max_value=2_000_000)

    rc, inside = _run_capture(args=["git", "rev-parse", "--is-inside-work-tree"], cwd=root)
    if rc != 0 or inside.strip().lower() != "true":
        return {"git_ok": False, "git_error": "not a git repo"}

    head_rc, head = _run_capture(args=["git", "rev-parse", "HEAD"], cwd=root)
    status_rc, status = _run_capture(args=["git", "status", "--porcelain"], cwd=root)
    stat_rc, diff_stat = _run_capture(args=["git", "diff", "--stat"], cwd=root, timeout_sec=20)

    diff_ref = ""
    if stat_rc == 0 and diff_stat.strip():
        diff_rc, diff_text = _run_capture(args=["git", "diff"], cwd=root, timeout_sec=30)
        # Avoid crazy large diff artifacts.
        if diff_rc == 0:
            if len(diff_text) > diff_max_chars:
                diff_text = diff_text[:diff_max_chars] + "\n...<truncated>"
            diff_ref = _write_text_artifact(
                ctx=ctx,
                filename=f"git_diff_{uuid.uuid4().hex}.diff",
                text=diff_text,
            )

    return {
        "git_ok": True,
        "git_head": head.strip() if head_rc == 0 else "",
        "git_dirty": bool(status.strip()) if status_rc == 0 else None,
        "git_status_porcelain": status,
        "git_diff_stat": diff_stat,
        "git_diff_ref": diff_ref,
    }


def _git_ensure_identity(*, root: Path) -> None:
    # Ensure we can commit even on fresh machines.
    rc_name, name = _run_capture(args=["git", "config", "user.name"], cwd=root)
    rc_email, email = _run_capture(args=["git", "config", "user.email"], cwd=root)

    if rc_name == 0 and name.strip() and rc_email == 0 and email.strip():
        return

    _run_capture(args=["git", "config", "user.name", "clonoth"], cwd=root)
    _run_capture(args=["git", "config", "user.email", "clonoth@local"], cwd=root)


def _git_commit_all(*, ctx: KernelContext, message: str) -> dict[str, Any]:
    root = ctx.workspace_root

    snap_before = _git_snapshot(ctx)
    if not snap_before.get("git_ok"):
        return {"ok": False, "error": snap_before.get("git_error")}

    before_head = str(snap_before.get("git_head") or "")

    # Nothing to commit
    if not snap_before.get("git_dirty"):
        return {"ok": True, "committed": False, "before_head": before_head, "after_head": before_head}

    _git_ensure_identity(root=root)

    rc_add, out_add = _run_capture(args=["git", "add", "-A"], cwd=root, timeout_sec=30)
    if rc_add != 0:
        return {"ok": False, "error": "git add failed", "output": out_add, "before_head": before_head}

    rc_commit, out_commit = _run_capture(
        args=["git", "commit", "-m", message],
        cwd=root,
        timeout_sec=60,
    )

    snap_after = _git_snapshot(ctx)
    after_head = str(snap_after.get("git_head") or "") if snap_after.get("git_ok") else before_head

    if rc_commit != 0:
        # e.g. nothing to commit
        return {
            "ok": False,
            "error": "git commit failed",
            "output": out_commit,
            "before_head": before_head,
            "after_head": after_head,
        }

    return {
        "ok": True,
        "committed": True,
        "output": out_commit,
        "before_head": before_head,
        "after_head": after_head,
    }


async def list_dir(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    path = str(args.get("path", "."))
    p = _resolve_under_root(ctx.workspace_root, path)
    if not p.exists():
        return {"ok": False, "error": "path not found", "path": path}

    items = []
    for child in sorted(p.iterdir()):
        items.append(
            {
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return {"ok": True, "path": path, "items": items}


async def read_file(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    op = await ctx.request_op(
        "read_file",
        {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
        },
    )
    if op.get("safety_level") == "deny":
        return {"ok": False, "error": op.get("reason", "denied")}

    if op.get("safety_level") == "approval_required":
        approval_id = op.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") != "allowed":
            return {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    p = _resolve_under_root(ctx.workspace_root, path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "file not found", "path": path}

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()

    start_val = start_line if isinstance(start_line, int) and start_line > 0 else 1
    s = start_val - 1

    if isinstance(end_line, int) and end_line >= start_val:
        e = min(end_line, len(lines))
    else:
        e = len(lines)

    sliced = lines[s:e]
    numbered = "\n".join([f"{i+s+1:>4} | {ln}" for i, ln in enumerate(sliced)])
    return {"ok": True, "path": path, "content": numbered}


async def write_file(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    content = str(args.get("content", ""))

    op = await ctx.request_op(
        "write_file",
        {
            "path": path,
            "content_preview": content[:200],
            "content_len": len(content),
        },
    )
    if op.get("safety_level") == "deny":
        return {"ok": False, "error": op.get("reason", "denied")}

    if op.get("safety_level") == "approval_required":
        approval_id = op.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") != "allowed":
            return {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    p = _resolve_under_root(ctx.workspace_root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


async def execute_command(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    default_timeout_sec = get_float(
        runtime_cfg,
        "meta.execute_command.default_timeout_sec",
        60.0,
        min_value=1.0,
        max_value=3600.0,
    )
    max_output_chars = get_int(
        runtime_cfg,
        "meta.execute_command.max_output_chars",
        8000,
        min_value=1000,
        max_value=200_000,
    )

    timeout_raw = args.get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)

    op = await ctx.request_op("execute_command", {"command": command})
    if op.get("safety_level") == "deny":
        return {"ok": False, "error": op.get("reason", "denied")}

    if op.get("safety_level") == "approval_required":
        approval_id = op.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") != "allowed":
            return {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    try:
        cp = subprocess.run(
            command,
            shell=True,
            cwd=str(ctx.workspace_root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=_safe_subprocess_env(),
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + "\n...<truncated>"
        return {"ok": True, "returncode": cp.returncode, "output": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout_sec}s"}


async def search_in_files(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    query = str(args.get("query", ""))
    rel_path = str(args.get("path", "."))

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    max_file_size_bytes = get_int(
        runtime_cfg,
        "meta.search.max_file_size_bytes",
        2_000_000,
        min_value=100_000,
        max_value=50_000_000,
    )
    max_matches = get_int(runtime_cfg, "meta.search.max_matches", 50, min_value=1, max_value=5000)

    if not query:
        return {"ok": False, "error": "empty query"}

    root = _resolve_under_root(ctx.workspace_root, rel_path)
    if not root.exists():
        return {"ok": False, "error": "path not found", "path": rel_path}

    matches: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.endswith((".pyc",)):
            continue
        # skip big files
        try:
            if p.stat().st_size > max_file_size_bytes:
                continue
        except Exception:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if query in text:
            rel = p.relative_to(ctx.workspace_root).as_posix()
            matches.append({"path": rel})
            if len(matches) >= max_matches:
                break

    return {"ok": True, "query": query, "matches": matches}


def _render_command_tool_py(*, spec: dict[str, Any], commands: list[str], timeout_sec: float | None) -> str:
    lines: list[str] = []
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append(
        "\"\"\"\n"
        "This is a *declarative command tool* (Clonoth Tool v2).\n"
        "\n"
        "- The Kernel will NOT import/execute this module.\n"
        "- It will parse this file as AST and extract literals (SPEC/COMMANDS/TIMEOUT_SEC).\n"
        "- Therefore: do NOT put runtime code here.\n"
        "\"\"\""
    )
    lines.append("")

    lines.append("# Tool specification")
    lines.append("SPEC = " + pprint.pformat(spec, width=100))
    lines.append("")

    lines.append("# Command template(s). You can reference args via Python format: {arg_name}")
    lines.append("COMMANDS = " + pprint.pformat(commands, width=100))

    if timeout_sec is not None:
        lines.append("")
        lines.append(f"TIMEOUT_SEC = {float(timeout_sec)}")

    lines.append("")
    return "\n".join(lines)


async def create_or_update_tool(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    """Create/update a *declarative command tool* under tools/.

    This replaces the previous 'arbitrary python tool' approach to avoid policy bypass.
    """

    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    input_schema = args.get("input_schema")

    timeout_sec = args.get("timeout_sec")

    command = args.get("command")
    commands = args.get("commands")

    if not name:
        return {"ok": False, "error": "empty tool name"}

    # Security: tool name must be a safe identifier to prevent path traversal.
    # The tool file will be written to tools/{name}.py.
    if not _TOOL_NAME_RE.fullmatch(name):
        return {
            "ok": False,
            "error": "invalid tool name: only [A-Za-z_][A-Za-z0-9_]{0,63} is allowed",
        }
    if name in _RESERVED_TOOL_NAMES:
        return {"ok": False, "error": f"reserved tool name: {name}"}

    cmd_list: list[str] = []
    if isinstance(commands, list) and commands:
        for c in commands:
            if isinstance(c, str) and c.strip():
                cmd_list.append(c.strip())
    elif isinstance(command, str) and command.strip():
        cmd_list = [command.strip()]

    if not cmd_list:
        return {"ok": False, "error": "empty command(s)"}

    if not isinstance(input_schema, dict):
        # default: accept any args
        input_schema = {"type": "object", "properties": {}, "required": []}

    spec: dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }

    code = _render_command_tool_py(spec=spec, commands=cmd_list, timeout_sec=float(timeout_sec) if timeout_sec is not None else None)

    path = f"tools/{name}.py"
    res = await write_file({"path": path, "content": code}, ctx)
    if not res.get("ok"):
        return res

    # reload tools
    try:
        count = ctx.registry.reload()
    except Exception as e:
        return {"ok": False, "error": f"tool written but reload failed: {e}", "path": path}

    return {"ok": True, "path": path, "reloaded": count}


async def reload_tools(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    try:
        count = ctx.registry.reload()
        return {"ok": True, "tools": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def request_restart(args: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    target = str(args.get("target", "kernel"))
    reason = str(args.get("reason", ""))

    # Prepare git diff summary for approval UI.
    git_info = _git_snapshot(ctx)

    # The approval request MUST contain diff summary/ref (cannot be faked by the LLM).
    op = await ctx.request_op(
        "restart",
        {
            "target": target,
            "reason": reason,
            "git": git_info,
            # tell user what will happen
            "will_git_commit_before_restart": True,
        },
    )
    if op.get("safety_level") == "deny":
        return {"ok": False, "error": op.get("reason", "denied")}

    approval_id: str | None = None
    if op.get("safety_level") == "approval_required":
        approval_id = op.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") != "allowed":
            return {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    # After approval: auto-commit current changes (best-effort) to make rollback easy.
    commit_res = _git_commit_all(
        ctx=ctx,
        message=f"clonoth: checkpoint before restart (task={ctx.task_id}, target={target})",
    )

    # If we're about to restart the kernel (or all), the current task would otherwise be left
    # in a running state (because the worker may be terminated before it can emit outbound_message
    # and task_completed). We therefore pre-emit a final outbound message and complete the task.
    if target in {"kernel", "all"}:
        final_text = f"已请求重启：{target}。"
        if reason.strip():
            final_text += f"\n原因：{reason.strip()}"
        final_text += "\n（系统已创建 checkpoint；若新版本启动失败将自动回滚）"

        try:
            await ctx.emit_event("outbound_message", {"text": final_text})
            await ctx.http.post(
                f"{ctx.supervisor_url}/v1/tasks/{ctx.task_id}/complete",
                json={
                    "status": "done",
                    "result": {
                        "text": final_text,
                        "restart": {
                            "target": target,
                            "reason": reason,
                            "git": git_info,
                            "git_commit": commit_res,
                        },
                    },
                },
            )
        except Exception:
            # best-effort only
            pass

    # call supervisor admin restart
    r = await ctx.http.post(
        f"{ctx.supervisor_url}/v1/admin/restart",
        json={"target": target, "reason": reason, "approval_id": approval_id},
    )
    if r.status_code >= 400:
        return {"ok": False, "error": r.text, "git": git_info, "git_commit": commit_res}

    return {
        "ok": True,
        "target": target,
        "scheduled": True,
        "git": git_info,
        "git_commit": commit_res,
    }
