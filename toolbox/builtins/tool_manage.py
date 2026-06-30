"""Tool management: create_or_update_tool, reload_tools."""
from __future__ import annotations

import pprint
import re
from typing import Any

from ..context import ToolContext
from . import TOOL_NAME_RE, RESERVED_TOOL_NAMES


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: tool management returns paths and counts but the
    # unified schema requires a readable data.result. How: put all fields under
    # data alongside a concise result message. Purpose: keep tool creation and
    # reload output consistent with other tools.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: validation and reload failures should not use legacy
    # top-level-only error payloads. How: add data.result and mirror optional
    # metadata. Purpose: keep failed tool-management calls readable.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


# ---------------------------------------------------------------------------
#  Code generation
# ---------------------------------------------------------------------------

def _render_tool_py(*, spec: dict[str, Any], script_body: str, timeout_sec: float | None) -> str:
    """Generate a tool .py file from spec and user script body."""
    lines: list[str] = []
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append(
        '"""\n'
        "External tool (Clonoth).\n"
        "\n"
        "The engine parses SPEC via AST at registration time.\n"
        "At invocation this file runs as a subprocess:\n"
        "  - Input: tool arguments as JSON on stdin\n"
        "  - Output: result as JSON on stdout\n"
        "  - Sensitive env vars are stripped\n"
        '"""'
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
    lines.append('    def fail(error): print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)')
    lines.append('    args = _input')

    for line in script_body.splitlines():
        lines.append("    " + line)

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

async def create_or_update_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update an external tool under tools/."""
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    input_schema = args.get("input_schema")
    timeout_sec = args.get("timeout_sec")
    script = args.get("script")

    if not name:
        return _err("empty tool name")
    if not TOOL_NAME_RE.fullmatch(name):
        return _err("invalid tool name: only [A-Za-z_][A-Za-z0-9_]{0,63} is allowed")
    if name in RESERVED_TOOL_NAMES:
        return _err(f"reserved tool name: {name}")
    if not isinstance(script, str) or not script.strip():
        return _err("'script' is required")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}

    spec = {"name": name, "description": description, "input_schema": input_schema}
    code = _render_tool_py(
        spec=spec,
        script_body=script.strip(),
        timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
    )

    from .write_file import write_file
    path = f"tools/{name}.py"
    res = await write_file({"path": path, "content": code}, ctx)
    if not res.get("ok"):
        return res

    try:
        count = ctx.registry.reload()
    except Exception as e:
        return _err(f"tool written but reload failed: {e}", path=path)

    return _ok(f"Tool written: {path}", path=path, reloaded=count)


async def reload_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Reload tools/ directory and MCP tools.

    [AutoC 2026-05-31] Why: MCP tools were only loaded at engine startup,
    requiring a full restart after adding/modifying MCP clients.
    How: reload_tools now also calls load_mcp_tools() after reloading
    local tools. Purpose: hot-reload MCP tool changes without restart.
    """
    try:
        count = ctx.registry.reload()
        mcp_count = await ctx.registry.load_mcp_tools()
        return _ok(f"Tools reloaded: {count} local, {mcp_count} MCP", tools=count, mcp_tools=mcp_count)
    except Exception as e:
        return _err(e)
