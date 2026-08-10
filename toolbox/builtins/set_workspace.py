"""set_workspace — change the session-level working directory and workspace name."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import resolve_and_classify


async def set_workspace(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    path_str = str(args.get("path", "")).strip() or None

    if not name:
        return {"ok": False, "error": "name is required", "data": {"result": "ERROR: name is required"}}

    # Lazy import to avoid circular deps at module level
    from engine.workspace_registry import resolve_workspace, validate_workspace_name

    if not validate_workspace_name(name):
        return {"ok": False, "error": f"invalid workspace name: {name}", "data": {"result": f"ERROR: invalid workspace name: {name}"}}

    # Validate path if provided
    resolved_path: Path | None = None
    if path_str:
        try:
            resolved_path, _trust = resolve_and_classify(ctx.workspace_root, path_str)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "data": {"result": f"ERROR: {exc}"}}
        if not resolved_path.is_dir():
            return {"ok": False, "error": f"not a directory: {path_str}", "data": {"result": f"ERROR: not a directory: {path_str}"}}

    # Resolve or register in workspace registry
    try:
        ws_name, ws_path = resolve_workspace(
            ctx.workspace_root, name, path=str(resolved_path) if resolved_path else None,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "data": {"result": f"ERROR: {exc}"}}

    # Apply to context
    ctx.workspace_name = ws_name
    if ws_path and ws_path.is_dir():
        ctx.workspace = ws_path
        display = ws_path.as_posix()
        try:
            display = ws_path.relative_to(ctx.workspace_root).as_posix()
        except ValueError:
            pass
        return {"ok": True, "data": {
            "result": f"Workspace set: {ws_name} → {display}",
            "workspace_name": ws_name,
            "workspace_path": display,
        }}
    else:
        ctx.workspace = None  # abstract workspace, no path binding
        return {"ok": True, "data": {
            "result": f"Workspace set: {ws_name} (no path binding)",
            "workspace_name": ws_name,
            "workspace_path": None,
        }}
