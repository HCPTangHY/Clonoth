"""Shared utilities and constants for built-in tools."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

from clonoth_runtime import classify_path, load_policy_config, parse_extra_roots

from .context import ToolContext


# ---------------------------------------------------------------------------
#  Process management
# ---------------------------------------------------------------------------


def kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the entire process group to avoid orphaned children holding pipes.

    Why: proc.kill() only kills the direct child (shell). Grandchild processes
    (e.g. PM2 daemon) inherit stdout/stderr pipe fds. communicate() waits for
    pipe EOF, so if grandchildren still hold the pipe, the gather(waiter) hangs
    forever. Using start_new_session=True + os.killpg kills the whole tree.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Fallback: kill just the direct process if killpg fails
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_SENSITIVE_ENV_KEYS_UPPER = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


# ---------------------------------------------------------------------------
#  Environment
# ---------------------------------------------------------------------------

def safe_subprocess_env() -> dict[str, str]:
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


# ---------------------------------------------------------------------------
#  Path resolution
# ---------------------------------------------------------------------------

def resolve_under_root(root: Path, rel_path: str) -> Path:
    """Resolve *rel_path* under *root*, raising if it escapes."""
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace root")
    return p


def _load_allowed_extra_roots(workspace_root: Path) -> list[Path]:
    """Load extra_roots from policy.yaml for defense in depth path checks."""
    data = load_policy_config(workspace_root)
    return parse_extra_roots(
        workspace_root,
        data.get("extra_roots") if isinstance(data, dict) else None,
    )


def resolve_and_classify(
    workspace_root: Path,
    path_str: str,
    *,
    workspace: Path | None = None,
) -> tuple[Path, str]:
    """Resolve path and classify trust level.

    Returns (resolved_path, trust_level).
    trust_level: 'workspace' | 'trusted' | 'external'
    Raises ValueError for invalid or escaping relative paths.
    """
    extra_roots = _load_allowed_extra_roots(workspace_root)
    resolved, display, trust_level = classify_path(
        workspace_root, extra_roots, path_str, workspace=workspace,
    )
    if resolved is None:
        raise ValueError(display)
    return resolved, trust_level


def resolve_under_allowed_roots(
    workspace_root: Path,
    path_str: str,
    *,
    workspace: Path | None = None,
) -> Path:
    """Resolve path under workspace, workspace_root, or policy-configured extra_roots.

    Allows untrusted external absolute paths (policy layer enforces approval).
    """
    resolved, _trust_level = resolve_and_classify(
        workspace_root, path_str, workspace=workspace,
    )
    return resolved


# ---------------------------------------------------------------------------
#  Policy / approval guard
# ---------------------------------------------------------------------------

async def request_guard(
    ctx: ToolContext,
    op: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Request supervisor policy decision and wait for approval when needed.

    Returns (op_response, error_result).
    error_result is None when the operation may proceed.
    """
    # [2026-09-06] Why: registry.authorize() pre-flights a tool's internal
    # guard (e.g. execute_command's command-level approval) before the adaptive
    # scheduler starts its async-upgrade timer; without a hand-off the tool
    # would request the same approval a second time. How: every approved guard
    # records an (op, canonical params) fingerprint on the ctx, and a matching
    # later request returns immediately. ctx objects are per tool call (the
    # scheduler snapshots before pre-flight), so fingerprints cannot leak
    # across calls. Purpose: one approval per exact guarded operation, with the
    # wait happening outside any execution timer.
    if _guard_fingerprint(op, parameters) in (getattr(ctx, "_approved_guard_fps", None) or ()):
        return {}, None
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

    try:
        approved = getattr(ctx, "_approved_guard_fps", None)
        if not isinstance(approved, set):
            approved = set()
            setattr(ctx, "_approved_guard_fps", approved)
        approved.add(_guard_fingerprint(op, parameters))
    except Exception:
        pass

    return op_res, None


def _guard_fingerprint(op: str, parameters: dict[str, Any]) -> str:
    """Stable identity of one guarded operation for pre-approval hand-off."""
    try:
        canonical = json.dumps(parameters, sort_keys=True, default=str)
    except Exception:
        canonical = repr(sorted((parameters or {}).items()))
    return f"{op}:{canonical}"


async def guard_external_read(
    ctx: ToolContext,
    is_external: bool,
    rel_path: str,
    tool_name: str,
    reason: str = "",
    *,
    trust_level: str = "",
) -> dict[str, Any] | None:
    """Request approval for non-workspace paths. Returns error dict or None (proceed).

    If trust_level is provided, uses it directly. Otherwise falls back to is_external bool.
    """
    # workspace-level paths are fully trusted, skip approval
    if trust_level == "workspace":
        return None
    if trust_level and trust_level != "external":
        # trusted-level: still allow but could be gated by policy in the future
        if not is_external:
            return None
    if not is_external:
        return None
    _op, err = await request_guard(
        ctx, "read_file",
        {"path": rel_path, "tool_name": tool_name,
         "reason": reason or f"{tool_name} on external path: {rel_path}"},
    )
    if err is not None:
        return {
            "success": False,
            "cancelled": err.get("cancelled", False),
            "error": err.get("error", "denied"),
        }
    return None
