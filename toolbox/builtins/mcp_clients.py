"""MCP client management tools."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext
from .. import mcp_runtime


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: MCP client management tools also need the unified
    # data.result field. How: centralize success payload creation and keep all
    # previous structured fields under data. Purpose: make management-tool output
    # readable and schema-consistent.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: failures from MCP client management should include a
    # readable data.result. How: wrap the error string once. Purpose: avoid legacy
    # ok=false payloads without data.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


async def create_or_update_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as e:
        return _err(e)
    return _ok(f"MCP client saved: {spec.get('id', '')}", client=spec, path="data/mcp_clients.yaml")


async def list_mcp_clients(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
        return _ok(f"{len(clients)} MCP clients", clients=clients)
    except Exception as e:
        return _err(e)


async def delete_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    client_id = str(args.get("id", "")).strip()
    if not client_id:
        return _err("empty client id")
    try:
        ok = mcp_runtime.delete_client(ctx.workspace_root, client_id)
        if not ok:
            return _err(f"client not found: {client_id}")
        return _ok(f"MCP client deleted: {client_id}", deleted=True, id=client_id)
    except Exception as e:
        return _err(e)
