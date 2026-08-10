from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import get_float, load_runtime_config

from . import builtins as _builtins
from ._common import kill_process_group as _kill_process_group
from ._common import safe_subprocess_env as _safe_subprocess_env
from . import mcp_runtime


ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


def _json_text(value: Any) -> str:
    # [AutoC 2026-05-31] Why: registry-level fallback wrapping needs a stable
    # readable transcript for arbitrary legacy tool payloads. How: serialize JSON
    # values compactly and fall back to str() if serialization fails. Purpose: let
    # every tool result expose data.result even when the tool itself is old.
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _error_tool_response(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: registry-generated failures, such as missing tools or
    # script timeouts, bypass individual tool code. How: put the error in both error
    # and data.result, with optional metadata under data and top level. Purpose:
    # keep all registry-originated failures in the unified ok/data/error shape.
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    data.update(fields)
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _ensure_tool_response_shape(result: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: user-created external tools and older built-ins may
    # still return legacy top-level fields. How: preserve already unified payloads,
    # add data.result to ok=false payloads, and otherwise move legacy fields under
    # data while mirroring attachments during migration. Purpose: enforce the new
    # ok/data/error contract at the registry boundary without losing metadata.
    if not isinstance(result, dict):
        return {"ok": True, "data": {"result": str(result), "value": result}}

    data = result.get("data") if isinstance(result.get("data"), dict) else None
    if isinstance(data, dict) and isinstance(data.get("result"), str) and "ok" in result:
        # [AutoC 2026-05-31] Why: some already-migrated tools may still provide
        # attachments only at the top level for migration compatibility. How: add
        # the nested data.attachments mirror when it is missing. Purpose: keep the
        # attachment contract consistent at the registry boundary.
        attachments = result.get("attachments")
        if isinstance(attachments, list) and not isinstance(data.get("attachments"), list):
            result = dict(result)
            data = dict(data)
            data["attachments"] = attachments
            result["data"] = data
        return result

    ok_value = False if result.get("ok") is False else True
    error_value = result.get("error")
    if ok_value is False:
        merged_data = dict(data or {})
        if not isinstance(merged_data.get("result"), str):
            merged_data["result"] = f"ERROR: {error_value or 'unknown'}"
        for key, value in result.items():
            if key not in {"ok", "data", "error"} and key not in merged_data:
                merged_data[key] = value
        return {"ok": False, "data": merged_data, "error": str(error_value or merged_data.get("result") or "unknown")}

    merged_data = dict(data or {})
    for key, value in result.items():
        if key in {"ok", "data", "error"}:
            continue
        merged_data.setdefault(key, value)
    if not isinstance(merged_data.get("result"), str):
        for key in ("text", "description", "message", "output", "result", "note"):
            value = result.get(key)
            if isinstance(value, str):
                merged_data["result"] = value
                break
    if not isinstance(merged_data.get("result"), str):
        merged_data["result"] = _json_text({k: v for k, v in result.items() if k not in {"ok", "data", "error"}})

    response = {"ok": True, "data": merged_data}
    attachments = merged_data.get("attachments") or result.get("attachments")
    if isinstance(attachments, list):
        response["attachments"] = attachments
        merged_data.setdefault("attachments", attachments)
    return response


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
                env=_safe_subprocess_env(),
                # Fix: run in new session so os.killpg can kill the entire
                # process tree, preventing orphaned grandchildren from holding
                # pipe fds and causing communicate() to hang forever.
                start_new_session=True,
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
                        # Fix: kill entire process group, not just the interpreter
                        _kill_process_group(proc)
                        # Fix: 5s safety timeout — if killpg didn't clean all
                        # grandchildren, don't hang forever waiting for pipe EOF
                        try:
                            await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        return _error_tool_response("script task cancelled", cancelled=True)
                except Exception:
                    pass
                if asyncio.get_running_loop().time() - started >= timeout_val:
                    _kill_process_group(proc)
                    try:
                        await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    return _error_tool_response(f"script timeout after {timeout_val}s")
        except asyncio.TimeoutError:
            try:
                _kill_process_group(proc)  # type: ignore[union-attr]
            except Exception:
                pass
            return _error_tool_response(f"script timeout after {timeout_val}s")
        except Exception as e:
            return _error_tool_response(f"script execution failed: {e}")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            # [AutoC 2026-05-31] Why: migrated external tools may intentionally
            # exit non-zero after printing a structured ok=false JSON payload.
            # How: parse stdout first and preserve the tool-owned error fields,
            # adding data.result only when the payload does not already have it.
            # Purpose: keep failures readable without discarding specific messages.
            try:
                parsed = json.loads(stdout_text)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    parsed_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
                    if not isinstance(parsed_data.get("result"), str):
                        parsed = dict(parsed)
                        parsed_data = dict(parsed_data)
                        parsed_data["result"] = f"ERROR: {parsed.get('error', f'script exited with code {proc.returncode}')}"
                        parsed["data"] = parsed_data
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return _error_tool_response(
                f"script exited with code {proc.returncode}",
                stderr=stderr_text[:2000] if stderr_text else "",
                stdout=stdout_text[:2000] if stdout_text else "",
            )

        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output]

        try:
            result = json.loads(stdout_text)
            if isinstance(result, dict):
                return _ensure_tool_response_shape(result)
            # [AutoC 2026-05-31] Why: script tools can print JSON scalars, but the
            # engine now expects ok/data/error. How: keep the scalar under value and
            # expose its string form as data.result. Purpose: make every successful
            # external script output readable through the same contract.
            return {"ok": True, "data": {"result": str(result), "value": result}}
        except json.JSONDecodeError:
            # [AutoC 2026-05-31] Why: legacy scripts may still print plain text.
            # How: wrap stdout as both data.result and data.output. Purpose: keep
            # plain-text scripts usable while conforming to the unified schema.
            return {"ok": True, "data": {"result": stdout_text, "output": stdout_text}}

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

    def register_builtin_tool(self, name: str, description: str, input_schema: dict[str, Any], func: ToolFunc) -> None:
        """Register one builtin tool declared by a built-in plugin."""
        # Why: some built-in tools now live with their owning plugin instead of
        # the central registry table. How: install the spec and callable into the
        # active maps and, when available, into the builtin snapshots used by
        # reload(). Purpose: plugin-owned builtin tools remain visible after tools
        # hot-reload and cannot be overridden by external script tools.
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("builtin tool name is required")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}, "required": []}
        if not callable(func):
            raise TypeError(f"builtin tool function is not callable: {clean_name}")

        spec = {
            "name": clean_name,
            "description": str(description or ""),
            "input_schema": input_schema,
        }
        self._tool_specs[clean_name] = spec
        self._tool_funcs[clean_name] = func

        # Why: plugin tools are registered after ToolRegistry.__init__ has already
        # captured its builtin snapshot. How: refresh the snapshots when they
        # exist. Purpose: later reload() calls keep plugin-declared tools.
        if hasattr(self, "_builtin_specs"):
            self._builtin_specs[clean_name] = spec
        if hasattr(self, "_builtin_funcs"):
            self._builtin_funcs[clean_name] = func

    def _load_builtin_meta_tools(self) -> None:
        builtins: list[tuple[str, str, dict[str, Any], ToolFunc]] = [
            (
                "list_dir",
                "List one or more directories under workspace root. Supports batch listing and recursive mode. Ignores .git by default.",
                {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "description": "Array of directory paths to list (relative to workspace root). MUST be an array even for single directory.",
                            "items": {"type": "string"},
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to list subdirectories recursively. Default false.",
                        },
                        "path": {"type": "string", "description": "(Legacy) Single directory path."},
                    },
                    "required": [],
                },
                _builtins.list_dir,
            ),
            (
                "read_file",
                "Read one or more files under workspace root. Supports text files (with optional line range), images (returned as multimodal), and binary files. Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "Array of file objects. Each has: path (required), startLine (optional, 1-based), endLine (optional, 1-based inclusive). MUST be an array even for single file.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "File path relative to workspace root."},
                                    "startLine": {"type": "integer", "description": "Start line (1-based, inclusive). Only for text files."},
                                    "endLine": {"type": "integer", "description": "End line (1-based, inclusive). Only for text files."},
                                },
                                "required": ["path"],
                            },
                        },
                        "path": {"type": "string", "description": "(Legacy single-file mode) File path relative to workspace root."},
                        "start_line": {"type": "integer", "description": "(Legacy) Start line number."},
                        "end_line": {"type": "integer", "description": "(Legacy) End line number."},
                    },
                    "required": [],
                },
                _builtins.read_file,
            ),
            (
                "write_file",
                "Write a text file under workspace root (policy+approval guarded).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
                _builtins.write_file,
            ),
            (
                "apply_diff",
                "Apply sequential search/replace diffs to a file. "
                "Each diff specifies an exact 'search' string and a 'replace' string. "
                "The search must match 100% exactly (including whitespace and indentation). "
                "Multiple diffs are applied in order; each operates on the result of the previous. "
                "If 'start_line' is omitted and search matches multiple locations, the diff is rejected. "
                "Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to workspace root.",
                        },
                        "diffs": {
                            "type": "array",
                            "description": "Array of diff objects to apply sequentially.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "search": {
                                        "type": "string",
                                        "description": "Exact text to find. Must match 100% including whitespace and indentation.",
                                    },
                                    "replace": {
                                        "type": "string",
                                        "description": "Replacement text.",
                                    },
                                    "start_line": {
                                        "type": "integer",
                                        "description": "(Recommended) 1-based line number to start searching from. Required when search matches multiple locations. Note: line numbers refer to content AFTER previous diffs in the array have been applied.",
                                    },
                                },
                                "required": ["search", "replace"],
                            },
                        },
                    },
                    "required": ["path", "diffs"],
                },
                _builtins.apply_diff,
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
                _builtins.execute_command,
            ),
            (
                "set_workspace",
                "Switch to a named workspace. Changes cwd for execute_command and path resolution for file tools. Also isolates memory storage. Workspace names are abstract labels; a filesystem path binding is optional.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Workspace name (e.g. 'tangqiu', 'clonoth', '闲聊'). Auto-registered if new."},
                        "path": {"type": "string", "description": "Optional: absolute or workspace-relative directory path to bind. Updates the cwd."},
                    },
                    "required": ["name"],
                },
                _builtins.set_workspace,
            ),
            (
                "grep",
                "Search file contents using pattern matching (like grep). Supports regex, glob filter, replace mode, and output_mode (content/files_only/count).",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "description": "Directory or file path to search in (relative or absolute). Supports single file. Default '.'"},
                        "mode": {"type": "string", "description": "search (default) or replace", "enum": ["search", "replace"]},
                        "pattern": {"type": "string", "description": "file glob pattern, e.g. '*.py', '**/*.js'. Default '**/*'"},
                        "isRegex": {"type": "boolean", "description": "treat query as regex. Default false"},
                        "maxResults": {"type": "integer", "description": "max matches for search mode. Default 100"},
                        "replace": {"type": "string", "description": "replacement string for replace mode. Supports $1 $2 capture groups"},
                        "maxFiles": {"type": "integer", "description": "max files to modify in replace mode. Default 50"},
                        "output_mode": {"type": "string", "description": "Output format: content (default, full match text), files_only (file paths only), count (match count per file)", "enum": ["content", "files_only", "count"]},
                    },
                    "required": ["query"],
                },
                _builtins.search_in_files,
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
                _builtins.create_or_update_mcp_client,
            ),
            (
                "list_mcp_clients",
                "List configured MCP clients.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_mcp_clients,
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
                _builtins.delete_mcp_client,
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
                _builtins.create_or_update_tool,
            ),
            (
                "reload_tools",
                "Reload tools/ directory and MCP tools.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.reload_tools,
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
                _builtins.request_restart,
            ),
            (
                "create_schedule",
                "Create or update a scheduled task in data/schedules.yaml. "
                "Supports type='message' (default, injects text as inbound) and type='script' "
                "(runs a shell command, parses stdout JSON for inbound injection). "
                "Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "unique schedule id"},
                        "cron": {"type": "string", "description": "5-field cron: minute hour day month weekday (UTC)"},
                        "text": {"type": "string", "description": "message text injected as inbound (for message type); prefix text (for script type)"},
                        "type": {"type": "string", "enum": ["message", "script"], "description": "schedule type: message (default) or script"},
                        "command": {"type": "string", "description": "shell command to run (required for script type). stdout must be a single-line JSON object with a 'text' field: {\"text\": \"message to inject\"}. Optional 'attachments' field for file paths. Empty stdout = silent skip (if silent=true) or prefix-only injection."},
                        "timeout": {"type": "integer", "description": "script timeout in seconds (default 30, script type only)"},
                        "silent": {"type": "boolean", "description": "if true, skip inbound when script stdout is empty (default true, script type only)"},
                        "conversation_key": {"type": "string", "description": "conversation key (default: scheduler:{id})"},
                        "entry_node_id": {"type": "string", "description": "optional entry node override for the injected inbound"},
                        "workflow_id": {"type": "string", "description": "optional workflow override"},
                        "enabled": {"type": "boolean"},
                        "once": {"type": "boolean", "description": "if true, auto-delete after first trigger"},
                    },
                    "required": ["id", "cron"],
                },
                _builtins.create_schedule,
            ),
            (
                "list_schedules",
                "List all scheduled tasks from data/schedules.yaml.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_schedules,
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
                _builtins.delete_schedule,
            ),
            (
                "cancel_active_tasks",
                "Cancel all active downstream tasks in the current session. "
                "Use when the user's new message makes the previous task unnecessary.",
                {"type": "object", "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "可选。只取消指定节点的活跃任务（如 scout、smith），不填则取消 session 内全部下游任务。",
                    },
                }, "required": []},
                _builtins.cancel_active_tasks,
            ),
            (
                "get_context_window",
                "Get current context window token usage for this session. "
                "Returns actual LLM-reported tokens (if available), character-based estimates, "
                "compact threshold, and utilization ratio. "
                "Use this to check how much context budget remains before automatic compaction triggers.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.get_context_window,
            ),
        ]

        for name, desc, schema, func in builtins:
            # Why: built-in tools now have a public registration path shared by
            # registry-owned and plugin-owned tools. How: seed the central table
            # through register_builtin_tool(). Purpose: keep one code path for
            # validating and storing builtin specs.
            self.register_builtin_tool(name, desc, schema, func)

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

            # [AutoC 2026-05-31] Why: external tools may declare SPEC.result_format
            # so result_to_raw() can route by an explicit formatter id before
            # falling back to structural predicates. How: copy the literal field
            # into the registered spec when it is a non-empty string. Purpose: keep
            # tool metadata available without changing any tool return payload.
            registered_spec = {
                "name": name,
                "description": description,
                "input_schema": input_schema,
                "async_mode": bool(spec.get("async_mode", False)),
            }
            result_format = spec.get("result_format")
            if isinstance(result_format, str) and result_format.strip():
                registered_spec["result_format"] = result_format.strip()
            self._tool_specs[name] = registered_spec
            self._tool_funcs[name] = _make_script_tool(
                script_path=py.resolve(), timeout_sec=timeout_sec,
            )

        return len(self._tool_specs)

    def list_specs(self) -> list[dict[str, Any]]:
        return list(self._tool_specs.values())

    def get_spec(self, name: str) -> dict[str, Any] | None:
        """按名称获取单个工具的 spec，不存在返回 None。"""
        return self._tool_specs.get(name)

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name not in self._tool_funcs:
            return _error_tool_response(f"tool not found: {name}")

        func = self._tool_funcs[name]
        return _ensure_tool_response_shape(await func(arguments, ctx))

    async def load_remote_tools(self, supervisor_url: str) -> int:
        """Load remote-worker tools from Supervisor and register wrappers."""
        # [AutoC 2026-08-01] Register Supervisor-brokered remote tools as first-class
        # ToolRegistry entries. Why: AI/tool tasks should call remote workers through
        # the same registry path as builtin, script, and MCP tools. How: fetch the
        # broker tool list, skip names that would override builtin or local script
        # tools, then install a small wrapper per remote tool. Purpose: remote tools
        # are available after startup and hot reload without changing inference code.
        from . import remote_runtime

        count = 0
        try:
            tools = await remote_runtime.list_remote_tools(supervisor_url)
        except Exception:
            logging.warning("[registry] remote tool list unavailable, skipping")
            return 0

        builtin_names = set(self._builtin_specs.keys())
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            registered_name = str(tool.get("name") or "").strip()
            if not registered_name or registered_name in builtin_names or registered_name in self._tool_specs:
                continue

            description = str(tool.get("description") or "").strip()
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}, "required": []}
            try:
                timeout_sec = float(tool.get("timeout_sec") or 60.0)
            except Exception:
                timeout_sec = 60.0

            self._tool_specs[registered_name] = {
                "name": registered_name,
                "description": description or registered_name,
                "input_schema": schema,
                "remote": tool.get("remote") if isinstance(tool.get("remote"), dict) else {},
            }
            self._tool_funcs[registered_name] = _make_remote_tool(registered_name, timeout_sec, supervisor_url)
            count += 1
        return count

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
                # [AutoC 2026-05-31] Why: MCP list_tools may be migrated to the
                # unified data wrapper while older code returns top-level tools.
                # How: read data.tools first, then fall back to result.tools.
                # Purpose: keep dynamic MCP registration working across schemas.
                result_data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
                tools = result_data.get("tools") if isinstance(result_data.get("tools"), list) else (result.get("tools") if isinstance(result, dict) else [])
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


def _make_remote_tool(registered_name: str, timeout_sec: float, supervisor_url: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # [AutoC 2026-08-01] Defer remote runtime import until execution. Why: the
        # registry module is imported by many engine paths, but remote HTTP client
        # code is only needed for remote tools. How: import inside the wrapper and
        # pass the fixed registered name, timeout, and Supervisor URL. Purpose: keep
        # remote tool calls isolated while preserving the standard ToolFunc shape.
        from . import remote_runtime

        return await remote_runtime.call_remote_tool(ctx, registered_name, args or {}, timeout_sec, supervisor_url)
    return _call


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # [2026-06-10] Why: MCP tool calls previously had no cancel support — the
        # engine loop only checks cancel between tools, not during a tool's await.
        # How: run the MCP call in a background task and poll check_cancelled every
        # 2 seconds. If cancel is detected, cancel the background task and return a
        # cancelled result. Purpose: users can stop stuck MCP calls without restarting.
        import asyncio
        try:
            mcp_task = asyncio.ensure_future(
                mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
            )
            while not mcp_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(mcp_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                if mcp_task.done():
                    break
                # Check cancel
                if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                    mcp_task.cancel()
                    try:
                        await mcp_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled by user", "cancelled": True}
            return mcp_task.result()
        except asyncio.CancelledError:
            return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled", "cancelled": True}
        except Exception as e:
            return _error_tool_response(str(e), mcp_client=client_id, mcp_tool=tool_name)
    return _call
