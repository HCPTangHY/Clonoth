from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import get_float, load_runtime_config

from . import meta_tools


ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


def _extract_decl_tool(py: Path) -> tuple[dict[str, Any] | None, list[str] | None, float | None]:
    """Extract a declarative command tool from a python file via AST.

    Tool v2 format (safe):
        SPEC = { ... }
        COMMANDS = ["..."]  # or COMMAND = "..."
        TIMEOUT_SEC = 60

    Security:
    - We DO NOT import/execute the module.
    - We only parse literals via `ast.literal_eval`.
    """

    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        return None, None, None

    try:
        tree = ast.parse(text, filename=str(py))
    except SyntaxError:
        return None, None, None

    vals: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id
            if name not in {"SPEC", "COMMAND", "COMMANDS", "TIMEOUT_SEC"}:
                continue
            try:
                vals[name] = ast.literal_eval(node.value)
            except Exception:
                continue

    spec = vals.get("SPEC")
    if not isinstance(spec, dict):
        return None, None, None

    commands: list[str] | None = None
    if isinstance(vals.get("COMMANDS"), list):
        raw = vals.get("COMMANDS")
        cmd_list: list[str] = []
        for c in raw:
            if isinstance(c, str) and c.strip():
                cmd_list.append(c.strip())
        if cmd_list:
            commands = cmd_list
    elif isinstance(vals.get("COMMAND"), str) and str(vals.get("COMMAND")).strip():
        commands = [str(vals.get("COMMAND")).strip()]

    timeout_sec: float | None = None
    if isinstance(vals.get("TIMEOUT_SEC"), (int, float)):
        timeout_sec = float(vals.get("TIMEOUT_SEC"))

    return spec, commands, timeout_sec


def _make_command_tool(*, commands: list[str], timeout_sec: float | None) -> ToolFunc:
    async def _run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Default timeout for declarative tools is configurable.
        root = getattr(ctx, "workspace_root", None)
        default_timeout_sec = 60.0
        if isinstance(root, Path):
            try:
                runtime_cfg = load_runtime_config(root)
                default_timeout_sec = get_float(
                    runtime_cfg,
                    "tools.command.default_timeout_sec",
                    60.0,
                    min_value=1.0,
                    max_value=3600.0,
                )
            except Exception:
                default_timeout_sec = 60.0

        timeout_val = float(timeout_sec) if timeout_sec is not None else float(default_timeout_sec)

        rendered: list[str] = []
        for tmpl in commands:
            try:
                rendered.append(tmpl.format(**(args or {})))
            except KeyError as e:
                return {"ok": False, "error": f"missing argument for command template: {e}"}
            except Exception as e:
                return {"ok": False, "error": f"command template format failed: {e}"}

        steps: list[dict[str, Any]] = []
        for cmd in rendered:
            res = await meta_tools.execute_command(
                {"command": cmd, "timeout_sec": timeout_val},
                ctx,
            )
            steps.append({"command": cmd, "result": res})
            if not isinstance(res, dict) or not res.get("ok"):
                return {"ok": False, "error": "command failed", "steps": steps}

        return {"ok": True, "steps": steps}

    return _run


class ToolRegistry:
    def __init__(self, *, workspace_root: Path, tools_dir: Path) -> None:
        self.workspace_root = workspace_root
        self.tools_dir = tools_dir

        self._tool_specs: dict[str, dict[str, Any]] = {}
        self._tool_funcs: dict[str, ToolFunc] = {}

        self._load_builtin_meta_tools()
        # snapshot builtins so reload() can reset dynamic tools
        self._builtin_specs = dict(self._tool_specs)
        self._builtin_funcs = dict(self._tool_funcs)

        self.reload()

    def _load_builtin_meta_tools(self) -> None:
        # 内置 meta tools
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
                "create_or_update_tool",
                "Create/update a declarative command tool under tools/ (parsed via AST; not imported/executed).",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "input_schema": {"type": "object"},
                        "command": {"type": "string"},
                        "commands": {"type": "array", "items": {"type": "string"}},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["name"],
                },
                meta_tools.create_or_update_tool,
            ),
            (
                "reload_tools",
                "Reload tools/ directory (declarative tools).",
                {"type": "object", "properties": {}, "required": []},
                meta_tools.reload_tools,
            ),
            (
                "request_restart",
                "Request supervisor to restart shell/kernel/all (policy+approval guarded; includes git diff summary).",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": ["shell", "kernel", "all"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["target"],
                },
                meta_tools.request_restart,
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
        """Reload declarative tools under tools/.

        Returns total number of tools available.
        """

        # reset to builtins only
        self._tool_specs = dict(self._builtin_specs)
        self._tool_funcs = dict(self._builtin_funcs)

        # Keep tools/ as a package (useful for future), but we do NOT import modules.
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "__init__.py").touch(exist_ok=True)

        # Ensure workspace root in sys.path for potential future imports (not used for decl tools).
        if str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))

        builtin_names = set(self._tool_specs.keys())

        for py in self.tools_dir.glob("*.py"):
            if py.name == "__init__.py":
                continue

            spec, commands, timeout_sec = _extract_decl_tool(py)
            if spec is None or commands is None:
                continue

            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            # Do not allow overriding builtin tools.
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
            self._tool_funcs[name] = _make_command_tool(commands=commands, timeout_sec=timeout_sec)

        return len(self._tool_specs)

    def list_specs(self) -> list[dict[str, Any]]:
        return list(self._tool_specs.values())

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name not in self._tool_funcs:
            return {"ok": False, "error": f"tool not found: {name}"}

        func = self._tool_funcs[name]
        return await func(arguments, ctx)
