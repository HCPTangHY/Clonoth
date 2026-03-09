from __future__ import annotations

import asyncio
import json
import os
import pprint
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import (
    get_float,
    get_int,
    load_policy_config,
    load_runtime_config,
    parse_extra_roots,
)

from .context import ToolContext
from . import mcp_runtime


_SENSITIVE_ENV_KEYS_UPPER = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# Reserved names: built-in meta tools (cannot be overridden by dynamic tools)
_RESERVED_TOOL_NAMES = {
    "list_dir",
    "read_file",
    "write_file",
    "execute_command",
    "search_in_files",
    "create_or_update_skill",
    "list_skills",
    "delete_skill",
    "create_or_update_mcp_client",
    "list_mcp_clients",
    "delete_mcp_client",
    "create_or_update_tool",
    "reload_tools",
    "request_restart",
    "create_schedule",
    "list_schedules",
    "delete_schedule",
}


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 5 :]
    return (yaml.safe_load(head) or {}), body


def _safe_subprocess_env() -> dict[str, str]:
    """Return a subprocess env with common secret variables stripped.

    This is not a complete security boundary, but it meaningfully reduces
    accidental leakage from command execution.
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


def _load_allowed_extra_roots(workspace_root: Path) -> list[Path]:
    """Load extra_roots from policy.yaml for defense in depth path checks."""
    data = load_policy_config(workspace_root)
    return parse_extra_roots(workspace_root, data.get("extra_roots") if isinstance(data, dict) else None)


def _resolve_under_allowed_roots(workspace_root: Path, path_str: str) -> Path:
    """Resolve path under workspace_root or policy-configured extra_roots."""

    extra_roots = _load_allowed_extra_roots(workspace_root)

    raw = Path(path_str)
    p = raw.resolve() if raw.is_absolute() else (workspace_root / path_str).resolve()

    try:
        p.relative_to(workspace_root)
        return p
    except ValueError:
        pass

    for r in extra_roots:
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue

    raise ValueError("path escapes workspace root")


async def _request_guard(
    ctx: ToolContext,
    op: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Request supervisor policy decision and wait for approval when needed.

    Returns (op_response, error_result).
    error_result is None when the operation may proceed.
    """

    op_res = await ctx.request_op(op, parameters)
    safety_level = str(op_res.get("safety_level") or "")

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    if safety_level == "deny":
        return op_res, {"ok": False, "error": op_res.get("reason", "denied")}

    if safety_level == "approval_required":
        approval_id = op_res.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") == "cancelled":
            return op_res, {"ok": False, "error": "task cancelled", "approval_id": approval_id, "cancelled": True}
        if approval.get("status") != "allowed":
            return op_res, {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    return op_res, None


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


def _write_text_artifact(*, ctx: ToolContext, filename: str, text: str) -> str:
    artifacts_dir = ctx.workspace_root / "data" / "artifacts" / ctx.run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    p = artifacts_dir / filename
    p.write_text(text or "", encoding="utf-8", errors="ignore")
    return p.relative_to(ctx.workspace_root).as_posix()


def _git_snapshot(ctx: ToolContext) -> dict[str, Any]:
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
    rc_name, name = _run_capture(args=["git", "config", "user.name"], cwd=root)
    rc_email, email = _run_capture(args=["git", "config", "user.email"], cwd=root)

    if rc_name == 0 and name.strip() and rc_email == 0 and email.strip():
        return

    _run_capture(args=["git", "config", "user.name", "clonoth"], cwd=root)
    _run_capture(args=["git", "config", "user.email", "clonoth@local"], cwd=root)


def _git_commit_all(*, ctx: ToolContext, message: str) -> dict[str, Any]:
    root = ctx.workspace_root

    snap_before = _git_snapshot(ctx)
    if not snap_before.get("git_ok"):
        return {"ok": False, "error": snap_before.get("git_error")}

    before_head = str(snap_before.get("git_head") or "")

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


async def list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
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


async def read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    _op, err = await _request_guard(
        ctx,
        "read_file",
        {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
        },
    )
    if err is not None:
        return err

    p = _resolve_under_allowed_roots(ctx.workspace_root, path)
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


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    content = str(args.get("content", ""))

    _op, err = await _request_guard(
        ctx,
        "write_file",
        {
            "path": path,
            "content_preview": content[:200],
            "content_len": len(content),
        },
    )
    if err is not None:
        return err

    p = _resolve_under_allowed_roots(ctx.workspace_root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


async def execute_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
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

    _op, err = await _request_guard(ctx, "execute_command", {"command": command})
    if err is not None:
        return err

    if await ctx.check_cancelled():
        return {"ok": False, "error": "task cancelled", "cancelled": True}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_subprocess_env(),
        )
        waiter = asyncio.create_task(proc.communicate())
        started = time.monotonic()
        while True:
            done, _pending = await asyncio.wait({waiter}, timeout=0.2)
            if waiter in done:
                stdout_bytes, stderr_bytes = waiter.result()
                break
            if await ctx.check_cancelled():
                proc.kill()
                await asyncio.gather(waiter, return_exceptions=True)
                return {"ok": False, "error": "task cancelled", "cancelled": True}
            if time.monotonic() - started >= timeout_sec:
                proc.kill()
                await asyncio.gather(waiter, return_exceptions=True)
                return {"ok": False, "error": f"timeout after {timeout_sec}s"}

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        out = (stdout_text or "") + ("\n" + stderr_text if stderr_text else "")
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + "\n...<truncated>"
        return {"ok": True, "returncode": int(proc.returncode or 0), "output": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def search_in_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
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


async def create_or_update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    content = args.get("content")
    enabled = bool(args.get("enabled", True))

    if not name:
        return {"ok": False, "error": "empty skill name"}
    if not _SKILL_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "invalid skill name: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed"}

    path = f"skills/{name}/SKILL.md"
    if not isinstance(content, str) or not content.strip():
        meta = {
            "name": name,
            "description": description,
            "enabled": enabled,
        }
        body = description or f"Skill {name}"
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"
    else:
        meta, body = _parse_skill_frontmatter(content)
        if not isinstance(meta, dict):
            meta = {}
        meta["name"] = name
        if description:
            meta["description"] = description
        elif not isinstance(meta.get("description"), str):
            meta["description"] = ""
        meta["enabled"] = enabled
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"

    res = await write_file({"path": path, "content": content}, ctx)
    if not res.get("ok"):
        return res
    return {"ok": True, "path": path, "name": name, "enabled": enabled}


async def list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    skills_dir = ctx.workspace_root / "skills"
    if not skills_dir.exists():
        return {"ok": True, "skills": []}

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            rel = skill_md.relative_to(ctx.workspace_root).as_posix()
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = _parse_skill_frontmatter(text)
            if not isinstance(meta, dict):
                meta = {}
            items.append(
                {
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "path": rel,
                }
            )
        except Exception as e:
            items.append({"name": skill_md.parent.name, "path": skill_md.relative_to(ctx.workspace_root).as_posix(), "error": str(e)})

    return {"ok": True, "skills": items}


async def delete_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "empty skill name"}
    if not _SKILL_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "invalid skill name"}

    skill_dir = _resolve_under_allowed_roots(ctx.workspace_root, f"skills/{name}")
    if not skill_dir.exists():
        return {"ok": False, "error": f"skill not found: {name}"}
    if not skill_dir.is_dir():
        return {"ok": False, "error": f"not a skill directory: {name}"}

    _op, err = await _request_guard(ctx, "write_file", {"path": f"skills/{name}/SKILL.md", "delete": True})
    if err is not None:
        return err

    shutil.rmtree(skill_dir)
    return {"ok": True, "deleted": True, "name": name}


async def create_or_update_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "client": spec, "path": "data/mcp_clients.yaml"}


async def list_mcp_clients(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
        return {"ok": True, "clients": clients}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def delete_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    client_id = str(args.get("id", "")).strip()
    if not client_id:
        return {"ok": False, "error": "empty client id"}
    try:
        ok = mcp_runtime.delete_client(ctx.workspace_root, client_id)
        if not ok:
            return {"ok": False, "error": f"client not found: {client_id}"}
        return {"ok": True, "deleted": True, "id": client_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def _render_tool_py(*, spec: dict[str, Any], script_body: str, timeout_sec: float | None) -> str:
    """Generate a tool .py file from spec and user script body."""
    lines: list[str] = []
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append(
        "\"\"\"\n"
        "External tool (Clonoth).\n"
        "\n"
        "The engine parses SPEC via AST at registration time.\n"
        "At invocation this file runs as a subprocess:\n"
        "  - Input: tool arguments as JSON on stdin\n"
        "  - Output: result as JSON on stdout\n"
        "  - Sensitive env vars are stripped\n"
        "\"\"\""
    )
    lines.append("")
    lines.append("SPEC = " + pprint.pformat(spec, width=100))

    if timeout_sec is not None:
        lines.append("")
        lines.append(f"TIMEOUT_SEC = {float(timeout_sec)}")

    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append('    import json, sys')
    lines.append('    _input = json.loads(sys.stdin.read())')
    lines.append('    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)')
    lines.append('    def fail(error): print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)); sys.exit(1)')
    lines.append('    args = _input')

    for line in script_body.splitlines():
        lines.append("    " + line)

    lines.append("")
    return "\n".join(lines)


async def create_or_update_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update an external tool under tools/."""
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    input_schema = args.get("input_schema")
    timeout_sec = args.get("timeout_sec")
    script = args.get("script")

    if not name:
        return {"ok": False, "error": "empty tool name"}
    if not _TOOL_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "invalid tool name: only [A-Za-z_][A-Za-z0-9_]{0,63} is allowed"}
    if name in _RESERVED_TOOL_NAMES:
        return {"ok": False, "error": f"reserved tool name: {name}"}
    if not isinstance(script, str) or not script.strip():
        return {"ok": False, "error": "'script' is required"}
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}

    spec = {"name": name, "description": description, "input_schema": input_schema}
    code = _render_tool_py(
        spec=spec,
        script_body=script.strip(),
        timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
    )

    path = f"tools/{name}.py"
    res = await write_file({"path": path, "content": code}, ctx)
    if not res.get("ok"):
        return res

    try:
        count = ctx.registry.reload()
    except Exception as e:
        return {"ok": False, "error": f"tool written but reload failed: {e}", "path": path}

    return {"ok": True, "path": path, "reloaded": count}


async def reload_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        count = ctx.registry.reload()
        return {"ok": True, "tools": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def request_restart(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = str(args.get("target", "engine"))
    reason = str(args.get("reason", ""))

    git_info = _git_snapshot(ctx)

    op, err = await _request_guard(
        ctx,
        "restart",
        {
            "target": target,
            "reason": reason,
            "git": git_info,
            "will_git_commit_before_restart": True,
        },
    )
    if err is not None:
        return err

    approval_id: str | None = op.get("approval_id") if op.get("safety_level") == "approval_required" else None

    commit_res = _git_commit_all(
        ctx=ctx,
        message=f"clonoth: checkpoint before restart (handoff={ctx.run_id}, target={target})",
    )

    if target in {"engine", "all"}:
        final_text = f"已请求重启：{target}。"
        if reason.strip():
            final_text += f"\n原因：{reason.strip()}"
        final_text += "\n（系统已创建 checkpoint；若新版本启动失败将自动回滚）"

        try:
            await ctx.emit_event("outbound_message", {"text": final_text})
        except Exception:
            pass

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

# ---------------------------------------------------------------------------
#  定时调度工具
# ---------------------------------------------------------------------------

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


async def create_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """创建或更新定时调度任务。"""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return {"ok": False, "error": "empty schedule id"}
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return {"ok": False, "error": "invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed"}

    cron_expr = str(args.get("cron") or "").strip()
    if not cron_expr:
        return {"ok": False, "error": "empty cron expression"}
    parts = cron_expr.split()
    if len(parts) != 5:
        return {"ok": False, "error": "cron must be 5 fields: minute hour day month weekday"}

    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}

    conv_key = str(args.get("conversation_key") or f"scheduler:{sid}").strip()
    workflow_id = str(args.get("workflow_id") or "").strip()
    enabled = bool(args.get("enabled", True))
    once = bool(args.get("once", False))

    _op, err = await _request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
    if err is not None:
        return err

    schedules = load_schedules(ctx.workspace_root)
    entry = {
        "id": sid,
        "cron": cron_expr,
        "text": text,
        "conversation_key": conv_key,
        "workflow_id": workflow_id,
        "enabled": enabled,
        "once": once,
    }

    replaced = False
    for i, s in enumerate(schedules):
        if str(s.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)

    save_schedules(ctx.workspace_root, schedules)
    return {"ok": True, "schedule": entry, "replaced": replaced}


async def list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """列出所有定时调度任务。"""
    from supervisor.scheduler import load_schedules

    schedules = load_schedules(ctx.workspace_root)
    return {"ok": True, "schedules": schedules}


async def delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """删除定时调度任务。"""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return {"ok": False, "error": "empty schedule id"}

    _op, err = await _request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
    if err is not None:
        return err

    schedules = load_schedules(ctx.workspace_root)
    before = len(schedules)
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(schedules) == before:
        return {"ok": False, "error": f"schedule not found: {sid}"}

    save_schedules(ctx.workspace_root, schedules)
    return {"ok": True, "deleted": True, "id": sid}


# ---------------------------------------------------------------------------
#  任务管理工具
# ---------------------------------------------------------------------------

async def cancel_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """取消当前会话中所有正在执行的下游 task。

    入口节点在收到新用户消息后，如果判断旧任务不再需要，调用此工具取消。
    """
    try:
        r = await ctx.http.post(
            f"{ctx.supervisor_url}/v1/sessions/{ctx.session_id}/cancel_active_tasks",
            params={"exclude_task_id": ctx.task_id},
        )
        if r.status_code >= 400:
            return {"ok": False, "error": r.text}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def list_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """列出当前会话中所有活跃 task 的摘要。"""
    # active_tasks_summary 已经通过 input_data 注入，这里提供一个实时查询接口
    return {"ok": True, "note": "活跃任务信息已在系统上下文中注入，无需额外查询。"}