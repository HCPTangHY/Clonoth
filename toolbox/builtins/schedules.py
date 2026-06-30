"""Schedule management: create_schedule, list_schedules, delete_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule tools return structured management data but
    # still need a canonical readable transcript. How: place all structured fields
    # under data with a result summary. Purpose: align schedules with ok/data/error.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule validation and approval failures should not
    # return legacy ok=false shapes. How: mirror optional flags under data and top
    # level while adding data.result. Purpose: keep scheduler failures readable.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


async def create_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return _err("invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed")

    cron_expr = str(args.get("cron") or "").strip()
    if not cron_expr:
        return _err("empty cron expression")
    parts = cron_expr.split()
    if len(parts) != 5:
        return _err("cron must be 5 fields: minute hour day month weekday")

    stype = str(args.get("type") or "message").strip()
    if stype not in ("message", "script"):
        return _err(f"invalid type: {stype}, must be 'message' or 'script'")

    text = str(args.get("text") or "").strip()
    if stype == "message" and not text:
        return _err("empty text (required for message type)")

    command = str(args.get("command") or "").strip()
    if stype == "script" and not command:
        return _err("empty command (required for script type)")

    conv_key = str(args.get("conversation_key") or "").strip()
    if not conv_key:
        # [2026-05-25] Auto-inherit from ToolContext (populated by supervisor
        # task_context → engine RunContext → ToolContext, zero I/O overhead).
        conv_key = str(getattr(ctx, "conversation_key", "") or "").strip()
    if not conv_key:
        conv_key = f"scheduler:{sid}"
    entry_node_id = str(args.get("entry_node_id") or "").strip()
    workflow_id = str(args.get("workflow_id") or "").strip()
    enabled = bool(args.get("enabled", True))
    once = bool(args.get("once", False))

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    entry: dict[str, Any] = {
        "id": sid,
        "cron": cron_expr,
        "conversation_key": conv_key,
        "enabled": enabled,
        "once": once,
    }
    if stype == "script":
        entry["type"] = "script"
        entry["command"] = command
        timeout = int(args.get("timeout") or 30)
        entry["timeout"] = max(5, min(timeout, 300))
        entry["silent"] = bool(args.get("silent", True))
        if text:
            entry["text"] = text
    else:
        entry["text"] = text
    if entry_node_id:
        entry["entry_node_id"] = entry_node_id
    if workflow_id:
        entry["workflow_id"] = workflow_id

    replaced = False
    for i, s in enumerate(schedules):
        if str(s.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule {'updated' if replaced else 'created'}: {sid}", schedule=entry, replaced=replaced)


async def list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List all scheduled tasks."""
    from supervisor.scheduler import load_schedules

    schedules = load_schedules(ctx.workspace_root)
    return _ok(f"{len(schedules)} schedules", schedules=schedules)


async def delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    before = len(schedules)
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(schedules) == before:
        return _err(f"schedule not found: {sid}")

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule deleted: {sid}", deleted=True, id=sid)
