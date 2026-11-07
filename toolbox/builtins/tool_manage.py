"""External script management through manage_tool."""
from __future__ import annotations

import math
import pprint
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

async def _upsert_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
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
    for flag in ("pure", "async_mode"):
        if flag in args:
            spec[flag] = bool(args[flag])
    try:
        timeout_value = float(timeout_sec) if timeout_sec is not None else None
    except (TypeError, ValueError, OverflowError):
        return _err("timeout_sec must be a positive finite number")
    if timeout_value is not None and (not math.isfinite(timeout_value) or timeout_value <= 0):
        return _err("timeout_sec must be a positive finite number")
    code = _render_tool_py(
        spec=spec,
        script_body=script.strip(),
        timeout_sec=timeout_value,
    )

    from .write_file import write_file
    path = f"tools/{name}.py"
    # Tools belong to the installation registry, not the active project workspace.
    target = ctx.registry.tools_dir / f"{name}.py"
    res = await write_file({"path": str(target.resolve()), "content": code}, ctx)
    if not res.get("ok"):
        return res

    try:
        count = ctx.registry.reload()
    except Exception as e:
        return _err(f"tool written but reload failed: {e}", path=path)

    return _ok(f"Tool written: {path}", path=path, reloaded=count)


async def _reload_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Reload tools/ directory.

    [AutoC 2026-08-27] MCP discovery no longer runs here: the mcp builtin plugin
    owns it, registers mcp_* tools through register_builtin_tool (which writes
    the reload snapshots), so they survive reload() without re-discovery.
    """
    try:
        count = ctx.registry.reload()
        return _ok(f"Tools reloaded: {count} local", tools=count)
    except Exception as e:
        return _err(e)


async def manage_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create/update a script or reload the external-tool registry."""
    action = str(args.get("action") or "").strip().lower()
    if not action:
        action = "upsert" if (args.get("name") or args.get("script")) else "reload"
    if action == "reload":
        return await _reload_tools(args, ctx)
    if action == "upsert":
        return await _upsert_tool(args, ctx)
    return _err(f"invalid action: {action!r}, must be 'upsert' or 'reload'")
