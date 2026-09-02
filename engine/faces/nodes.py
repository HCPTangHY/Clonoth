"""Declarative node face: plugins declare nodes that override kernel defaults.

[AutoC 2026-08-30] Why: engine/system_nodes/ was a compromise — built-in
capabilities should be able to ship their own nodes and third-party plugins
should be able to override kernel defaults without writing into the user's
config/nodes/ directory (Paradox-style mod override semantics). How: a
declarative face mounted as ``ctx.contributions.nodes``; declarations live in
a module-level store so consumers without context access (load_node, runner
discovery, the supervisor node list) can query them. Purpose: a plugin
declares a capability, the framework consumes it — unloading the plugin
reverts the override through the shared disposal ledger.

Precedence: plugin declaration > engine/system_nodes/ > config/nodes/.
Conflict rule matches the routes face: the same node id declared by two
different plugins is rejected; re-registration by the same plugin (reload)
replaces.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Module-level store: node_id -> {"owner": str, "data": dict}.
# Why module-level: load_node() and the runner caches are plain functions
# without EngineContext access; the store must be reachable from them. Each
# process (engine / supervisor) has its own interpreter and therefore its own
# store, populated by the plugins loaded in that process.
_DECLS: dict[str, dict[str, Any]] = {}
_VERSION = 0


def _bump() -> None:
    global _VERSION
    _VERSION += 1


def declared_node(node_id: str) -> dict[str, Any] | None:
    """Return the plugin-declared node dict for one id, or None."""
    entry = _DECLS.get((node_id or "").strip())
    return entry["data"] if entry is not None else None


def declared_node_owner(node_id: str) -> str:
    """Return the owning plugin name for one declared id, or empty string."""
    entry = _DECLS.get((node_id or "").strip())
    return str(entry["owner"]) if entry is not None else ""


def declared_node_path(node_id: str) -> str:
    """Return the declaration source file path for one id, or empty string.

    [AutoC 2026-09-02] Why: the admin node editor reads node YAML through a
    raw-file endpoint that only knew the two on-disk node directories; after
    system nodes migrated to plugin declarations the editor got 404. How: the
    loader records the resolved {"file": ...} target on the store entry, and
    the admin API resolves plugin-declared ids to this path. Purpose: editing
    a plugin-declared node opens its declaration file in the plugin package.
    """
    entry = _DECLS.get((node_id or "").strip())
    return str(entry.get("path") or "") if entry is not None else ""


def iter_declared_nodes() -> list[dict[str, Any]]:
    """Return all declarations as {"id", "owner", "data"} rows, sorted by id."""
    return [
        {"id": nid, "owner": str(entry["owner"]), "data": entry["data"]}
        for nid, entry in sorted(_DECLS.items())
    ]


def registry_version() -> int:
    """Monotonic mutation counter; consumers use it as a cache key component."""
    return _VERSION


class PluginNodesFace:
    """Contribution face for plugin-declared nodes (both processes).

    Mounted as ``ctx.contributions.nodes`` in the engine and supervisor
    contexts. Registration derives the owner from the shared disposal
    ledger's collecting block, exactly like the routes face.
    """

    def __init__(self) -> None:
        self._ledger: Any = None

    def set_disposal_ledger(self, ledger: Any) -> None:
        """Attach the shared disposal ledger (see engine/registry_core.py)."""
        self._ledger = ledger

    def register(self, decl: dict[str, Any], source_path: str = "") -> Callable[[], None]:
        """Declare one node and return its disposer.

        decl uses the same schema as a node YAML file (id/type/name/prompt/
        tool_access/...). Validation beyond id presence happens at parse time
        in load_node; a declaration that fails to parse falls back to the
        file sources with a warning instead of silently breaking the id.
        source_path optionally records the declaration file for raw editors.
        """
        owner = self._ledger.current_owner() if self._ledger is not None else None
        if not isinstance(decl, dict):
            raise ValueError("node declaration must be a dict")
        nid = str(decl.get("id") or "").strip()
        if not nid:
            raise ValueError("node declaration missing id")
        existing = _DECLS.get(nid)
        if existing is not None and existing["owner"] != (owner or ""):
            raise ValueError(
                f"node {nid!r} already declared by plugin {existing['owner']!r}"
            )
        data = dict(decl)
        data["id"] = nid
        # [AutoC 2026-08-30] Idempotent re-registration: the engine reloads
        # built-ins on every ai_step, so an unchanged same-owner declaration
        # must not bump the version (that would invalidate runner caches for
        # no reason). The original disposer stays recorded, so unload still
        # works; this duplicate registration archives nothing.
        if existing is not None and existing["data"] == data:
            return lambda: None
        _DECLS[nid] = {"owner": owner or "", "data": data, "path": source_path or ""}
        _bump()

        def _dispose() -> None:
            current = _DECLS.get(nid)
            if current is not None and current["data"] is data:
                del _DECLS[nid]
                _bump()

        if self._ledger is not None:
            self._ledger.record(_dispose)
        return _dispose
