"""Workspace file tree API — depth-limited directory listing.

Provides a nested tree structure for a given workspace path.
Used by the web frontend sidebar and system prompt injection.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Directories to always skip
_IGNORE_DIRS = frozenset({
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".nuxt", "dist", ".vite",
})

# Files to always skip
_IGNORE_FILES = frozenset({
    ".DS_Store", "Thumbs.db", ".gitkeep",
})


def _build_tree_node(
    base: Path,
    rel: str,
    current_depth: int,
    max_depth: int,
) -> dict[str, Any]:
    """Recursively build a tree node dict.

    Returns:
        {
            "name": str,
            "path": str,          # relative to workspace root
            "type": "directory" | "file",
            "children": [...],    # only for directories within depth
            "truncated": bool,    # True if children were cut off by depth
            "size": int,          # file size in bytes (files only)
        }
    """
    full = base / rel if rel else base
    name = full.name or str(full)
    node: dict[str, Any] = {
        "name": name,
        "path": rel or ".",
        "type": "directory" if full.is_dir() else "file",
    }

    if full.is_file():
        try:
            node["size"] = full.stat().st_size
        except OSError:
            node["size"] = 0
        return node

    if not full.is_dir():
        return node

    if current_depth >= max_depth:
        node["truncated"] = True
        # Still count immediate children for the badge
        try:
            items = list(full.iterdir())
            dirs = sum(1 for c in items if c.is_dir() and c.name not in _IGNORE_DIRS)
            files = sum(1 for c in items if c.is_file() and c.name not in _IGNORE_FILES)
            node["childDirs"] = dirs
            node["childFiles"] = files
        except OSError:
            node["childDirs"] = 0
            node["childFiles"] = 0
        node["children"] = []
        return node

    children: list[dict[str, Any]] = []
    try:
        entries = sorted(full.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        entries = []

    for entry in entries:
        if entry.is_dir() and entry.name in _IGNORE_DIRS:
            continue
        if entry.is_file() and entry.name in _IGNORE_FILES:
            continue

        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        children.append(_build_tree_node(base, child_rel, current_depth + 1, max_depth))

    node["children"] = children
    node["truncated"] = False
    return node


def build_workspace_tree(
    workspace_path: Path,
    *,
    sub_path: str = "",
    max_depth: int = 2,
) -> dict[str, Any]:
    """Build a workspace file tree starting from workspace_path/sub_path.

    Args:
        workspace_path: Absolute path to the workspace root.
        sub_path: Optional subdirectory within the workspace to start from.
        max_depth: How many levels deep to recurse (1 = immediate children only).

    Returns:
        A nested tree dict. See _build_tree_node for structure.
    """
    root = workspace_path.resolve()
    if sub_path:
        target = (root / sub_path).resolve()
        # Prevent path traversal
        try:
            target.relative_to(root)
        except ValueError:
            return {
                "name": sub_path,
                "path": sub_path,
                "type": "error",
                "error": "path traversal not allowed",
            }
        if not target.exists():
            return {
                "name": sub_path,
                "path": sub_path,
                "type": "error",
                "error": "path not found",
            }
        rel = sub_path
    else:
        target = root
        rel = ""

    return _build_tree_node(root, rel, 0, max_depth)
