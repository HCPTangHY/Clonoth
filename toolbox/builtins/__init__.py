"""Built-in tool implementations.

Each sub-module exposes one or more async tool functions with the
signature ``(args: dict, ctx: ToolContext) -> dict``.

This package also provides the canonical set of reserved tool names
that external/dynamic tools may not override.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
#  Name patterns
# ---------------------------------------------------------------------------

TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# ---------------------------------------------------------------------------
#  Reserved names (cannot be overridden by dynamic tools)
# ---------------------------------------------------------------------------

RESERVED_TOOL_NAMES = {
    "list_dir",
    "read_file",
    "apply_diff",
    "write_file",
    "execute_command",
    "search_in_files",
    "create_or_update_skill",
    "list_skills",
    "delete_skill",
    "create_or_update_mcp_client",
    "list_mcp_clients",
    "delete_mcp_client",
    "create_or_update_tool",
    "reload_tools",
    "request_restart",
    "create_schedule",
    "list_schedules",
    "delete_schedule",
    "save_memory",
    "list_memories",
    "delete_memory",
    "get_context_window",
}

# Convenience alias used by supervisor/admin_api.py
_RESERVED_TOOL_NAMES = RESERVED_TOOL_NAMES

# ---------------------------------------------------------------------------
#  Re-exports — so registry.py can do  ``from toolbox.builtins import ...``
# ---------------------------------------------------------------------------

from .list_dir import list_dir  # noqa: E402,F401
from .read_file import read_file  # noqa: E402,F401
from .write_file import write_file  # noqa: E402,F401
from .execute_command import execute_command  # noqa: E402,F401
from .search_in_files import search_in_files  # noqa: E402,F401
# Why: skill CRUD tools moved into engine.builtin.knowledge_inject. How: keep the
# names reserved above but stop importing the removed skill-tool module.
# Purpose: registry startup no longer depends on deleted knowledge files.
from .mcp_clients import create_or_update_mcp_client, list_mcp_clients, delete_mcp_client  # noqa: E402,F401
from .tool_manage import create_or_update_tool, reload_tools  # noqa: E402,F401
from .system import request_restart  # noqa: E402,F401
from .schedules import create_schedule, list_schedules, delete_schedule  # noqa: E402,F401
from .tasks import cancel_active_tasks, list_active_tasks, get_context_window  # noqa: E402,F401
# Why: memory CRUD tools moved into engine.builtin.knowledge_inject. How: keep the
# names reserved above but stop importing the removed memory-tool module.
# Purpose: plugin metadata becomes the single registration source.
from .apply_diff import apply_diff  # noqa: E402,F401
