"""Switch the session workspace only after the supervisor confirms persistence."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..context import ToolContext
from .._common import resolve_and_classify


async def set_workspace(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from engine.workspace_registry import load_workspaces, resolve_workspace, validate_workspace_name

    name = args.get("name")
    path_str = args.get("path")
    if not isinstance(name, str) or not validate_workspace_name(name):
        return {"ok": False, "error": "invalid workspace name", "data": {"result": "ERROR: invalid workspace name"}}
    name = name.strip()
    if path_str is not None and not isinstance(path_str, str):
        return {"ok": False, "error": "path must be a string", "data": {"result": "ERROR: path must be a string"}}
    path_str = (path_str or "").strip()
    try:
        # Resolve without changing the shared name registry or the running context.
        if path_str:
            path, _trust = resolve_and_classify(ctx.workspace_root, path_str)
            if not path.is_dir():
                raise ValueError(f"not a directory: {path_str}")
        else:
            raw = load_workspaces(ctx.workspace_root).get(name, {}).get("path")
            path = Path(raw).resolve() if raw else None
        response = await ctx.http.put(
            f"{ctx.supervisor_url.rstrip('/')}/v1/sessions/{ctx.route_session_id()}/workspace",
            json={"name": name, "path": path.as_posix() if path else None}, timeout=10.0,
        )
        response.raise_for_status()
        saved = response.json()
        if not isinstance(saved, dict) or saved.get("name") != name:
            raise ValueError("supervisor returned an unconfirmed workspace")
        saved_path = saved.get("path")
        if saved_path is not None and not isinstance(saved_path, str):
            raise ValueError("supervisor returned an invalid workspace path")
        path = Path(saved_path).resolve() if saved_path else None
    except (ValueError, OSError, httpx.HTTPError) as exc:
        text = f"Workspace switch was not confirmed: {exc}. Read the session workspace before retrying."
        return {"ok": False, "error": text, "data": {"result": text}}

    ctx.workspace_name = name
    ctx.workspace = path if path and path.is_dir() else None
    display = ctx.workspace.as_posix() if ctx.workspace else None
    warning = ""
    try:
        # ponytail: sessions.json is authoritative; workspaces.yaml is a reusable
        # name registry, not a cross-file transaction. A registry failure must not
        # pretend the confirmed session switch was rolled back.
        resolve_workspace(ctx.workspace_root, name, path=path.as_posix() if path else None)
    except (ValueError, OSError) as exc:
        warning = f"Session workspace was saved, but the shared name registry was not updated: {exc}"
    text = f"Workspace set: {name}" + (f" → {display}" if display else " (no path binding)")
    data = {"result": text + (f"\n{warning}" if warning else ""),
            "workspace_name": name, "workspace_path": display}
    if warning:
        data["warning"] = warning
    return {"ok": True, "data": data}
