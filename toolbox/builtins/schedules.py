"""Scheduled-task management through manage_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}", **fields}, **fields}


async def _upsert_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a schedule or patch the supplied fields of an existing one."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return _err("invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed")

    _, err = await request_guard(ctx, "write_file", {
        "path": str(ctx.workspace_root / "data" / "schedules.yaml"), "schedule_id": sid,
    })
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    # Read and merge after approval: pending approvals must not restore stale fields.
    schedules = load_schedules(ctx.workspace_root)
    existing = next((s for s in schedules if str(s.get("id") or "").strip() == sid), {})
    values = {**existing, **{key: value for key, value in args.items() if value is not None}}
    cron_expr = str(values.get("cron") or "").strip()
    if len(cron_expr.split()) != 5:
        return _err("cron must be 5 fields: minute hour day month weekday")
    stype = str(values.get("type") or "message").strip()
    if stype not in ("message", "script"):
        return _err(f"invalid type: {stype}, must be 'message' or 'script'")
    text = str(values.get("text") or "").strip()
    command = str(values.get("command") or "").strip()
    if stype == "message" and not text:
        return _err("empty text (required for message type)")
    if stype == "script" and not command:
        return _err("empty command (required for script type)")

    conv_key = str(values.get("conversation_key") or "").strip()
    conv_key = conv_key or str(ctx.conversation_key or "").strip() or f"scheduler:{sid}"
    entry: dict[str, Any] = {
        "id": sid,
        "cron": cron_expr,
        "conversation_key": conv_key,
        "enabled": bool(values.get("enabled", True)),
        "once": bool(values.get("once", False)),
    }
    if stype == "script":
        try:
            timeout = int(values.get("timeout") or 30)
        except (TypeError, ValueError, OverflowError):
            return _err("timeout must be an integer")
        entry.update(type="script", command=command, timeout=max(5, min(timeout, 300)),
                     silent=bool(values.get("silent", True)))
        if text:
            entry["text"] = text
    else:
        entry["text"] = text
    for key in ("entry_node_id", "workflow_id"):
        value = str(values.get(key) or "").strip()
        if value:
            entry[key] = value

    replaced = False
    for i, schedule in enumerate(schedules):
        if str(schedule.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)
    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule {'updated' if replaced else 'created'}: {sid}", schedule=entry, replaced=replaced)


async def _list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from supervisor.scheduler import load_schedules

    schedules = load_schedules(ctx.workspace_root)
    if not schedules:
        return _ok("0 schedules (data/schedules.yaml is empty)", schedules=schedules)
    lines = [f"{len(schedules)} schedules (full entry details: read_file data/schedules.yaml):"]
    for schedule in schedules:
        sid = str(schedule.get("id") or "?")
        cron = str(schedule.get("cron") or "?")
        stype = "script" if schedule.get("type") == "script" else "message"
        enabled = "enabled" if schedule.get("enabled", True) else "disabled"
        once = ", once" if schedule.get("once") else ""
        line = f"- {sid} | cron '{cron}' | {stype} | {enabled}{once}"
        ck = str(schedule.get("conversation_key") or "")
        if ck:
            line += f" | ck={ck}"
        lines.append(line)
    return _ok("\n".join(lines), schedules=schedules)


async def _delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return _err("invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed")
    _, err = await request_guard(ctx, "write_file", {
        "path": str(ctx.workspace_root / "data" / "schedules.yaml"), "delete_schedule": sid,
    })
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))
    schedules = load_schedules(ctx.workspace_root)
    remaining = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(remaining) == len(schedules):
        return _err(f"schedule not found: {sid}")
    save_schedules(ctx.workspace_root, remaining)
    return _ok(f"Schedule deleted: {sid}", deleted=True, id=sid)


async def manage_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List schedules, upsert selected fields, or delete one schedule."""
    action = str(args.get("action") or "").strip().lower()
    if not action:
        updates = ("cron", "text", "command", "type", "enabled", "once", "timeout",
                   "silent", "conversation_key", "entry_node_id", "workflow_id")
        action = "upsert" if args.get("id") and any(args.get(k) is not None for k in updates) else "list"
    if action == "list":
        return await _list_schedules(args, ctx)
    if action == "upsert":
        return await _upsert_schedule(args, ctx)
    if action == "delete":
        return await _delete_schedule(args, ctx)
    return _err(f"invalid action: {action!r}, must be 'list', 'upsert', or 'delete'")
