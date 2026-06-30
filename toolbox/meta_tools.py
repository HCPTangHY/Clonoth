"""Backward-compatibility shim.

All tool implementations have been moved to ``toolbox.builtins.*`` and
shared utilities to ``toolbox._common``.  This module re-exports the
public names so that existing ``from toolbox import meta_tools`` or
``from toolbox.meta_tools import ...`` continue to work.
"""
from __future__ import annotations

# Re-export shared utilities
from ._common import (
    safe_subprocess_env as _safe_subprocess_env,
    resolve_under_root as _resolve_under_root,
    resolve_under_allowed_roots as _resolve_under_allowed_roots,
    request_guard as _request_guard,
)

# Re-export constants
from .builtins import (
    RESERVED_TOOL_NAMES as _RESERVED_TOOL_NAMES,
    TOOL_NAME_RE as _TOOL_NAME_RE,
    SKILL_NAME_RE as _SKILL_NAME_RE,
)

# Re-export all tool functions
from .builtins import (
    list_dir,
    read_file,
    write_file,
    execute_command,
    search_in_files,
    create_or_update_skill,
    list_skills,
    delete_skill,
    create_or_update_mcp_client,
    list_mcp_clients,
    delete_mcp_client,
    create_or_update_tool,
    reload_tools,
    request_restart,
    create_schedule,
    list_schedules,
    delete_schedule,
    cancel_active_tasks,
    list_active_tasks,
)
