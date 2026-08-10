"""Workspace registry — data/workspaces.yaml.

Maps abstract workspace names to optional filesystem paths.
Session-level; AI switches via set_workspace(name=...).
Memory isolation uses the workspace name as a subdirectory key.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict

logger = logging.getLogger(__name__)

_REGISTRY_FILE = "data/workspaces.yaml"

# ponytail: name must be safe for directory names — alphanumeric, CJK, dash, underscore
_NAME_RE = re.compile(r'^[\w\-\u4e00-\u9fff\u3040-\u30ff]{1,64}$')


def _registry_path(workspace_root: Path) -> Path:
    return workspace_root / _REGISTRY_FILE


def load_workspaces(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Load workspace registry. Returns {name: {path: str|null, ...}}."""
    data = load_yaml_dict(_registry_path(workspace_root))
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        name = str(k).strip()
        if not name:
            continue
        if isinstance(v, dict):
            result[name] = dict(v)
        elif isinstance(v, str):
            result[name] = {"path": v}
        else:
            result[name] = {"path": None}
    return result


def save_workspaces(workspace_root: Path, registry: dict[str, dict[str, Any]]) -> None:
    """Persist workspace registry to YAML."""
    import yaml
    p = _registry_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {k: v for k, v in registry.items()},
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )


def resolve_workspace(
    workspace_root: Path,
    name: str,
    path: str | None = None,
) -> tuple[str, Path | None]:
    """Resolve or register a workspace by name.

    Returns (name, resolved_path_or_None).
    Auto-registers unknown names when path is provided.
    """
    name = name.strip()
    if not name:
        raise ValueError("workspace name is required")
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid workspace name: {name!r} (alphanumeric/CJK/dash/underscore, 1-64 chars)")

    registry = load_workspaces(workspace_root)

    if name in registry:
        entry = registry[name]
        existing_path = entry.get("path")
        if path and existing_path and str(path).strip() != str(existing_path).strip():
            # Update path binding
            entry["path"] = str(path).strip()
            save_workspaces(workspace_root, registry)
        if path and not existing_path:
            entry["path"] = str(path).strip()
            save_workspaces(workspace_root, registry)
        raw = entry.get("path")
        resolved = Path(raw).resolve() if raw else None
        return name, resolved

    # Auto-register
    entry: dict[str, Any] = {}
    if path:
        entry["path"] = str(path).strip()
    registry[name] = entry
    save_workspaces(workspace_root, registry)
    raw_path = entry.get("path")
    resolved = Path(raw_path).resolve() if raw_path else None
    return name, resolved


def validate_workspace_name(name: str) -> bool:
    """Check if name is valid for use as workspace directory key."""
    return bool(name and _NAME_RE.match(name.strip()))
