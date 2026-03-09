from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import get_float, load_runtime_config

from . import meta_tools
from . import mcp_runtime


ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


def _extract_tool_spec(py: Path) -> tuple[dict[str, Any] | None, float | None]:
    """Extract tool spec from a python file via AST.

    External tool format:
        SPEC = { ... }
        TIMEOUT_SEC = 60  # optional

        if __name__ == "__main__":
            # reads JSON from stdin, writes JSON to stdout

    Security:
    - We do NOT import or execute the module at registration time.
    - We only parse literals via ast.literal_eval.
    - At invocation, the script runs as an isolated subprocess.
    """

    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        return None, None

    try:
        tree = ast.parse(text, filename=str(py))
    except SyntaxError:
        return None, None

    vals: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id
            if name not in {"SPEC", "TIMEOUT_SEC"}:
                continue
            try:
                vals[name] = ast.literal_eval(node.value)
            except Exception:
                continue

    spec = vals.get("SPEC")
    if not isinstance(spec, dict):
        return None, None

    timeout_sec: float | None = None
    if isinstance(vals.get("TIMEOUT_SEC"), (int, float)):
        timeout_sec = float(vals.get("TIMEOUT_SEC"))

    return spec, timeout_sec


def _make_script_tool(*, script_path: Path, timeout_sec: float | None) -> ToolFunc:
    """Create a tool function that runs a Python script as a subprocess.

    Protocol:
    - Input: tool arguments as JSON on stdin
    - Output: result as JSON on stdout
    - Environment: sensitive variables stripped
    - Timeout: configurable
    """

    async def _run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        root = getattr(ctx, "workspace_root", None)
        default_timeout_sec = 60.0
        if isinstance(root, Path):
            try:
                runtime_cfg = load_runtime_config(root)
                default_timeout_sec = get_float(
                    runtime_cfg,
                    "tools.script.default_timeout_sec",
                    60.0,
                    min_value=1.0,
                    max_value=3600.0,
                )
            except Exception:
                default_timeout_sec = 60.0

        timeout_val = float(timeout_sec) if timeout_sec is not None else float(default_timeout_sec)
        max_output = 16000

        input_json = json.dumps(args or {}, ensure_ascii=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root) if isinstance(root, Path) else None,
                env=meta_tools._safe_subprocess_env(),
            )
            waiter = asyncio.create_task(proc.communicate(input=input_json.encode("utf-8")))
            started = asyncio.get_running_loop().time()
            while True:
                done, _pending = await asyncio.wait({waiter}, timeout=0.2)
                if waiter in done:
                    stdout_bytes, stderr_bytes = waiter.result()
                    break
                try:
                    if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                        proc.kill()
                        await asyncio.gather(waiter, return_exceptions=True)
                        return {"ok": False, "error": "script task cancelled", "cancelled": True}
                except Exception:
                    pass
                if asyncio.get_running_loop().time() - started >= timeout_val:
                    proc.kill()
                    await asyncio.gather(waiter, return_exceptions=True)
                    return {"ok": False, "error": f"script timeout after {timeout_val}s"}
        except asyncio.TimeoutError:
            try:
                proc.kill()  # type: ignore[union-attr]
            except Exception:
                pass
            return {"ok": False, "error": f"script timeout after {timeout_val}s"}
        except Exception as e:
            return {"ok": False, "error": f"script execution failed: {e}"}

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"script exited with code {proc.returncode}",
                "stderr": stderr_text[:2000] if stderr_text else "",
                "stdout": stdout_text[:2000] if stdout_text else "",
            }

        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output]

        try:
            result = json.loads(stdout_text)
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        except json.JSONDecodeError:
            return {"ok": True, "output": stdout_text}

    return _run


class ToolRegistry:
    def __init__(self, *, workspace_root: Path, tools_dir: Path) -> None:
        self.workspace_root = workspace_root
        self.tools_dir = tools_dir

        self._tool_specs: dict[str, dict[str, Any]] = {}
        self._tool_funcs: dict[str, ToolFunc] = {}

        self._load_builtin_meta_tools()
        self._builtin_specs = dict(self._tool_specs)
        self._builtin_funcs = dict(self._tool_funcs)

        self.reload()

    def _load_builtin_meta_tools(self) -> None:
        builtins: list[tuple[str, str, dict[str, Any], ToolFunc]] = [
            (
                "list_dir",
                "List a directory under workspace root.",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "relative path"}},
                    "required": [],
                },
                meta_tools.list_dir,
            ),
            (
                "read_file",
                "Read a text file under workspace root with optional line range (policy+approval guarded).",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path"],
                },
                meta_tools.read_file,
            ),
            (
                "write_file",
                "Write a text file under workspace root (policy+approval guarded).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
                meta_tools.write_file,
            ),
            (
                "execute_command",
                "Execute a shell command in workspace root (policy+approval guarded; secrets are stripped from env).",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["command"],
                },
                meta_tools.execute_command,
            ),
            (
                "search_in_files",
                "Search substring in files under workspace root.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["query"],
                },
                meta_tools.search_in_files,
            ),
            (
                "create_or_update_skill",
                "Create or update a skill under skills/<name>/SKILL.md.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "content": {"type": "string", "description": "full SKILL.md content (optional; frontmatter will be normalized)"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
                meta_tools.create_or_update_skill,
            ),
            (
                "list_skills",
                "List local skills under skills/*/SKILL.md.",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.list_skills,
            ),
            (
                "delete_skill",
                "Delete a skill directory under skills/<name>/.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
                meta_tools.delete_skill,
            ),
            (
                "create_or_update_mcp_client",
                "Create or update an MCP client config in data/mcp_clients.yaml. Supports stdio, sse, and streamable_http transports.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "streamable-http", "http"]},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "env": {"type": "object"},
                        "url": {"type": "string"},
                        "headers": {"type": "object"},
                    },
                    "required": ["id", "transport"],
                },
                meta_tools.create_or_update_mcp_client,
            ),
            (
                "list_mcp_clients",
                "List configured MCP clients.",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.list_mcp_clients,
            ),
            (
                "delete_mcp_client",
                "Delete an MCP client config by id.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                    "required": ["id"],
                },
                meta_tools.delete_mcp_client,
            ),
            (
                "create_or_update_tool",
                "Create/update an external tool under tools/. "
                "Provide 'script' (Python code body). "
                "The tool runs as a subprocess: JSON in via stdin, JSON out via stdout.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "input_schema": {"type": "object"},
                        "script": {"type": "string", "description": "Python script body. Has access to: args (dict from stdin), output(result), fail(error)."},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["name", "script"],
                },
                meta_tools.create_or_update_tool,
            ),
            (
                "reload_tools",
                "Reload tools/ directory.",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.reload_tools,
            ),
            (
                "request_restart",
                "Request supervisor to restart engine/all (policy+approval guarded; includes git diff summary).",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": ["engine", "all"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["target"],
                },
                meta_tools.request_restart,
            ),
            (
                "create_schedule",
                "Create or update a scheduled task in data/schedules.yaml. "
                "The task fires as an inbound message at the specified cron time (UTC). "
                "Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "unique schedule id"},
                        "cron": {"type": "string", "description": "5-field cron: minute hour day month weekday (UTC)"},
                        "text": {"type": "string", "description": "message text injected as inbound"},
                        "conversation_key": {"type": "string", "description": "conversation key (default: scheduler:{id})"},
                        "workflow_id": {"type": "string", "description": "optional workflow override"},
                        "enabled": {"type": "boolean"},
                        "once": {"type": "boolean", "description": "if true, auto-delete after first trigger"},
                    },
                    "required": ["id", "cron", "text"],
                },
                meta_tools.create_schedule,
            ),
            (
                "list_schedules",
                "List all scheduled tasks from data/schedules.yaml.",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.list_schedules,
            ),
            (
                "delete_schedule",
                "Delete a scheduled task by id. Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "schedule id to delete"},
                    },
                    "required": ["id"],
                },
                meta_tools.delete_schedule,
            ),
            (
                "cancel_active_tasks",
                "Cancel all active downstream tasks in the current session. "
                "Use when the user's new message makes the previous task unnecessary.",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.cancel_active_tasks,
            ),
        ]

        for name, desc, schema, func in builtins:
            self._tool_specs[name] = {
                "name": name,
                "description": desc,
                "input_schema": schema,
            }
            self._tool_funcs[name] = func

    def reload(self) -> int:
        """Reload external tools under tools/.

        Returns total number of tools available.
        """

        self._tool_specs = dict(self._builtin_specs)
        self._tool_funcs = dict(self._builtin_funcs)

        self.tools_dir.mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "__init__.py").touch(exist_ok=True)

        if str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))

        builtin_names = set(self._tool_specs.keys())

        for py in self.tools_dir.glob("*.py"):
            if py.name == "__init__.py":
                continue

            spec, timeout_sec = _extract_tool_spec(py)
            if spec is None:
                continue

            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            if name in builtin_names:
                continue

            description = str(spec.get("description", ""))
            input_schema = spec.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}, "required": []}

            self._tool_specs[name] = {
                "name": name,
                "description": description,
                "input_schema": input_schema,
            }
            self._tool_funcs[name] = _make_script_tool(
                script_path=py.resolve(), timeout_sec=timeout_sec,
            )

        return len(self._tool_specs)

    def list_specs(self) -> list[dict[str, Any]]:
        return list(self._tool_specs.values())

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name not in self._tool_funcs:
            return {"ok": False, "error": f"tool not found: {name}"}

        func = self._tool_funcs[name]
        return await func(arguments, ctx)

    async def load_mcp_tools(self) -> int:
        """Scan enabled MCP clients and register their tools as first-class tools."""
        count = 0
        try:
            clients = mcp_runtime.list_clients(self.workspace_root)
        except Exception:
            return 0

        for client in clients:
            if not isinstance(client, dict):
                continue
            cid = str(client.get("id") or "").strip()
            if not cid or not client.get("enabled", True):
                continue

            try:
                result = await mcp_runtime.list_tools(self.workspace_root, cid)
                tools = result.get("tools") if isinstance(result, dict) else []
                if not isinstance(tools, list):
                    continue
            except Exception:
                logging.warning(f"[registry] MCP client '{cid}' tool list unavailable, skipping")
                continue

            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw_name = str(tool.get("name") or "").strip()
                if not raw_name:
                    continue

                reg_name = f"mcp_{cid}_{raw_name}"
                if reg_name in self._tool_specs:
                    continue

                desc = str(tool.get("description") or "").strip()
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}, "required": []}

                self._tool_specs[reg_name] = {
                    "name": reg_name,
                    "description": f"[MCP:{cid}] {desc}" if desc else f"[MCP:{cid}] {raw_name}",
                    "input_schema": schema,
                }
                self._tool_funcs[reg_name] = _make_mcp_tool(self.workspace_root, cid, raw_name)
                count += 1

        return count


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            return await mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
        except Exception as e:
            return {"ok": False, "error": str(e), "mcp_client": client_id, "mcp_tool": tool_name}
    return _call
