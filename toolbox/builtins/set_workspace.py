"""set_workspace — change the session-level working directory."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import resolve_and_classify


async def set_workspace(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return {"ok": False, "error": "path is required", "data": {"result": "ERROR: path is required"}}

    try:
        resolved, is_external = resolve_and_classify(ctx.workspace_root, path_str)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "data": {"result": f"ERROR: {exc}"}}

    if not resolved.is_dir():
        return {"ok": False, "error": f"not a directory: {path_str}", "data": {"result": f"ERROR: not a directory: {path_str}"}}

    # ponytail: external paths are allowed (extra_roots covers them),
    # but we log it for visibility.
    ctx.workspace = resolved
    rel = resolved.as_posix()
    try:
        rel = resolved.relative_to(ctx.workspace_root).as_posix()
    except ValueError:
        pass
    return {"ok": True, "data": {"result": f"Workspace changed to: {rel}", "workspace": rel}}
