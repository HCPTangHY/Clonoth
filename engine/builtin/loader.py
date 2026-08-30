from __future__ import annotations

"""Auto-discovery loader for built-in hook handlers."""

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..hooks.loader import (
    iter_hook_points,
    register_meta_handler,
    register_declared_nodes,
    clear_load_error,
    record_load_error,
)

if TYPE_CHECKING:
    from toolbox.registry import ToolRegistry

logger = logging.getLogger(__name__)

_SKIP_MODULES = {"__init__", "loader", "result"}


def auto_discover_and_register(
    registry: Any,
    *,
    package: str = "engine.builtin",
    directory: Path | None = None,
    tool_registry: "ToolRegistry | None" = None,
    context: Any = None,
) -> dict[str, Any]:
    """Scan built-in handler modules and register PLUGIN_META declarations.

    Why: built-in engine and supervisor hook handlers should not be wired through
    hard-coded registration functions. How: import each module under engine.builtin,
    read PLUGIN_META, instantiate its handler class, and register the declared
    methods on the provided registry. Purpose: keep handler placement, metadata,
    and registration in one file per handler.

    Two-phase loading: first collect all modules and their PLUGIN_META, then
    instantiate in dependency order. A plugin whose `requires` list references
    a handler_name that failed or is missing will be skipped with a clear error.

    [AutoC 2026-08-27] Directory plugins: an entry under base_dir may be a single
    .py file or a package directory with __init__.py. Why: a plugin that owns a
    real library (e.g. the MCP protocol runtime) should not be forced into one
    file. How: mirror the external loader's package rule — one directory with
    __init__.py is one plugin; internal modules stay private to the package.
    Purpose: keep one-plugin-one-entry for both shapes.
    """
    base_dir = Path(directory) if directory is not None else Path(__file__).parent
    handlers: dict[str, Any] = {}
    if not base_dir.is_dir():
        return handlers

    # Phase 1: import all modules, collect metadata
    # Why: we need the full set of plugin names before resolving dependencies.
    # How: scan once, store (module, meta, entry) tuples keyed by handler_name.
    # Purpose: enable Phase 2 to check `requires` against the complete set.
    pending: dict[str, tuple[Any, dict, Path]] = {}  # handler_name -> (module, meta, entry)
    for entry in sorted(base_dir.iterdir()):
        if entry.is_dir():
            if _should_skip_package(entry):
                continue
            entry_name = entry.name
        else:
            if _should_skip(entry):
                continue
            entry_name = entry.stem
        module_name = f"{package}.{entry_name}"
        try:
            module = importlib.import_module(module_name)
            meta = getattr(module, "PLUGIN_META", None)
            if not isinstance(meta, dict):
                continue
            # Peek at handler_class to derive handler_name for dependency keys
            class_name = str(meta.get("handler_class") or "").strip()
            if not class_name:
                # [AutoC 2026-08-30] Meta-only plugins: PLUGIN_META without a
                # handler_class is accepted when it carries declarative
                # surfaces (nodes). Why: some kernel capabilities (e.g. the
                # turn summarizer node) are dispatched by core code and own no
                # hooks, but still ship their node through the plugin system.
                if isinstance(meta.get("nodes"), list):
                    pending[entry_name] = (module, meta, entry)
                continue
            cls = getattr(module, class_name, None)
            preview_name = str(getattr(cls, "name", "") or "") if cls else ""
            handler_name = preview_name or entry_name
            pending[handler_name] = (module, meta, entry)
        except Exception as exc:
            logger.error("Failed to import built-in hook %s: %s", module_name, exc, exc_info=True)

    # Phase 2: topological instantiation respecting `requires`
    # Why: plugins like memory_extract and dream depend on knowledge_inject being
    # loaded first. How: resolve in dependency order; skip plugins whose
    # requirements are not satisfied. Purpose: fail clearly instead of silently
    # breaking at runtime when a dependency is missing.
    loaded: set[str] = set()
    load_order = _resolve_load_order(pending)
    for handler_name in load_order:
        module, meta, py_file = pending[handler_name]
        module_name = f"{package}.{py_file.stem}"
        # Check requires
        requires = meta.get("requires")
        if isinstance(requires, list):
            missing = [r for r in requires if str(r) not in loaded]
            if missing:
                logger.error(
                    "Skipping plugin %s: unsatisfied requires %s (loaded: %s)",
                    handler_name, missing, sorted(loaded),
                )
                continue
        try:
            if not str(meta.get("handler_class") or "").strip():
                # Meta-only plugin: register declarative surfaces inside
                # collecting() so their disposers land in the shared ledger.
                with registry.collecting(handler_name):
                    register_declared_nodes(handler_name, meta, context, module)
                handlers[handler_name] = None
                loaded.add(handler_name)
                _register_meta(registry, py_file, meta, handler_name)
                continue
            # Why: every registration a plugin makes must be reversible, including
            # registrations done in __init__ (e.g. prompt sections). How: the
            # shared register_meta_handler runs instantiation, hook registration,
            # teardown archival, and tool registration inside collecting(name) so
            # the shared disposal ledger archives each disposer under this plugin.
            # Purpose: unload_plugin(handler_name) undoes the whole load across
            # every registration surface.
            instance = register_meta_handler(
                registry, module, meta, ledger_name=handler_name,
                context=context, tool_registry=tool_registry,
            )
            handler_name = str(getattr(instance, "name", "") or py_file.stem)
            handlers[handler_name] = instance
            loaded.add(handler_name)
            _register_meta(registry, py_file, meta, handler_name)
        except Exception as exc:
            logger.error("Failed to load built-in hook %s: %s", module_name, exc, exc_info=True)
    return handlers


def _resolve_load_order(pending: dict[str, tuple]) -> list[str]:
    """Return handler names in dependency-first order.

    Why: plugins declare `requires` listing other handler_names that must load
    first. How: simple topological sort — plugins with no or satisfied deps go
    first; cycles or missing deps are caught in Phase 2 and skipped. Purpose:
    ensure knowledge_inject loads before memory_extract/dream without hardcoding.
    """
    order: list[str] = []
    visited: set[str] = set()
    names = set(pending.keys())

    def _visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        _, meta, _ = pending[name]
        requires = meta.get("requires")
        if isinstance(requires, list):
            for dep in requires:
                dep = str(dep)
                if dep in names and dep not in visited:
                    _visit(dep)
        order.append(name)

    for name in pending:
        _visit(name)
    return order


def _should_skip_package(path: Path) -> bool:
    """Return whether a directory entry should be ignored by built-in discovery."""
    # Why: built-in plugins may now split into directory packages (e.g. mcp/
    # carrying its protocol runtime next to __init__.py). How: mirror the
    # external loader's package rule — skip private/hidden names and entries
    # without __init__.py. Purpose: one built-in plugin may own internal
    # modules without flattening them into a single file.
    if not path.is_dir():
        return True
    if path.name.startswith(("_", ".")):
        return True
    if path.name in _SKIP_MODULES:
        return True
    return not (path / "__init__.py").is_file()


def _should_skip(py_file: Path) -> bool:
    """Return whether a Python file should be ignored by built-in discovery."""
    # Why: helper modules and private files are not hook handlers. How: skip
    # reserved stems and underscore-prefixed files. Purpose: let result.py and the
    # loader itself coexist with discoverable handler modules.
    if not py_file.is_file() or py_file.suffix != ".py":
        return True
    if py_file.stem in _SKIP_MODULES:
        return True
    return py_file.name.startswith("_")


def _register_meta(registry: Any, py_file: Path, meta: dict[str, Any], handler_name: str) -> None:
    """Publish normalized built-in plugin metadata when the registry supports it."""
    # Why: HookRegistry can expose loaded plugin metadata to diagnostics. How:
    # register a copied display record after successful handler registration.
    # Purpose: make built-in auto-discovery observable without coupling the loader
    # to one concrete registry implementation.
    register_plugin_meta = getattr(registry, "register_plugin_meta", None)
    if not callable(register_plugin_meta):
        return
    display_meta = dict(meta)
    if isinstance(display_meta.get("tools"), list):
        # Why: tool declarations contain live callables that diagnostic metadata
        # should not expose. How: keep only small serializable tool records.
        # Purpose: HookRegistry.list_plugins() remains safe to inspect and encode.
        display_meta["tools"] = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
            }
            for tool in display_meta["tools"]
            if isinstance(tool, dict)
        ]
    display_meta.setdefault("name", handler_name)
    display_meta.setdefault("version", "builtin")
    display_meta.setdefault("description", "")
    display_meta.setdefault("author", "core")
    display_meta.setdefault("hooks", [str(point[0]) for point in display_meta.get("hook_points", [])])
    display_meta.setdefault("module", py_file.stem)
    register_plugin_meta(display_meta)


# ---------------------------------------------------------------------------
#  Single-entry runtime reload for built-in plugins
# ---------------------------------------------------------------------------

def builtin_entry_path(entry_name: str, *, directory: Path | None = None) -> Path | None:
    """Resolve one built-in plugin entry (file or package) under base_dir."""
    base_dir = Path(directory) if directory is not None else Path(__file__).parent
    pkg = base_dir / entry_name
    if pkg.is_dir() and (pkg / "__init__.py").is_file() and not _should_skip_package(pkg):
        return pkg
    py_file = base_dir / f"{entry_name}.py"
    if py_file.is_file() and not _should_skip(py_file):
        return py_file
    return None


def drop_builtin_module(entry_name: str, *, package: str = "engine.builtin") -> None:
    """Remove one built-in plugin module and its submodules from sys.modules.

    Why: reload must re-execute the entry's module code. How: delete the exact
    module plus every submodule under its prefix (directory plugins own private
    modules like a vendored runtime). Purpose: import_module after this picks
    up the on-disk code instead of the cached first import.
    """
    full = f"{package}.{entry_name}"
    prefix = full + "."
    for name in [n for n in list(sys.modules) if n == full or n.startswith(prefix)]:
        del sys.modules[name]


def load_single_builtin(
    registry: Any,
    entry_name: str,
    *,
    package: str = "engine.builtin",
    directory: Path | None = None,
    context: Any = None,
    tool_registry: "ToolRegistry | None" = None,
) -> dict:
    """(Re)load one built-in plugin entry at runtime and register its PLUGIN_META.

    Why: the plugin manager's runtime reload previously covered only external
    plugins/ entries, so a code fix inside engine/builtin/ (e.g. a route
    annotation bug) still required a process restart to verify. How: locate the
    entry, drop its cached modules, import fresh, and run the same
    register_meta_handler + _register_meta path the startup scan uses, with the
    shared disposal ledger attributing every registration. Purpose: built-in
    plugins become reloadable through the same admin surface as external ones.
    """
    entry = builtin_entry_path(entry_name, directory=directory)
    if entry is None:
        raise ValueError(f"no built-in plugin entry named {entry_name!r}")

    drop_builtin_module(entry_name, package=package)
    module_name = f"{package}.{entry_name}"
    try:
        module = importlib.import_module(module_name)
        meta = getattr(module, "PLUGIN_META", None)
        if not isinstance(meta, dict):
            raise ValueError(f"{module_name} declares no PLUGIN_META")
        class_name = str(meta.get("handler_class") or "").strip()
        if not class_name:
            # Meta-only plugin (declarative surfaces only, e.g. turn_summary).
            if not isinstance(meta.get("nodes"), list):
                raise ValueError(f"{module_name} PLUGIN_META has no handler_class and no declarations")
            handler_name = str(meta.get("name") or "").strip() or entry_name
            with registry.collecting(handler_name):
                register_declared_nodes(handler_name, meta, context, module)
            _register_meta(registry, entry, meta, handler_name)
            clear_load_error(entry_name)
            return {"name": handler_name, "entry": entry_name}
        cls = getattr(module, class_name, None)
        if cls is None:
            raise ValueError(f"{module_name} has no class {class_name!r}")
        handler_name = str(getattr(cls, "name", "") or entry_name)
        instance = register_meta_handler(
            registry, module, meta, ledger_name=handler_name,
            context=context, tool_registry=tool_registry,
        )
        handler_name = str(getattr(instance, "name", "") or entry_name)
        _register_meta(registry, entry, meta, handler_name)
        clear_load_error(entry_name)
        return {"name": handler_name, "entry": entry_name}
    except Exception as exc:
        record_load_error(entry_name, exc)
        raise
