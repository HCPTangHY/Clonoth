from __future__ import annotations

"""Loader for user-provided hook plugins.

[2026-05-03] Why: the hook system has built-in handlers, but users need a
stable extension point outside engine source files. How: scan one directory for
enabled Python files and call their register(hook_registry) function. Purpose:
allow local custom handlers to be installed at startup without changing the
engine package.
"""

import importlib.util
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

from .registry import HookRegistry
from ..context import accepts_context

logger = logging.getLogger(__name__)


def _default_plugin_meta(py_file: Path) -> dict:
    """Build default metadata for a plugin file."""
    # Why: existing plugins predate PLUGIN_META and must keep loading unchanged.
    # How: derive a stable name from the file stem and fill every documented
    # metadata field. Purpose: make list_plugins() complete for legacy plugins.
    return {
        "name": py_file.stem,
        "version": "unknown",
        "description": "",
        "author": "",
        "hooks": [],
    }


def _normalize_plugin_meta(py_file: Path, raw_meta: object) -> dict:
    """Merge optional PLUGIN_META fields with loader-owned defaults."""
    # Why: plugin authors may omit PLUGIN_META or only provide some fields.
    # How: copy defaults first, then overlay a declared dict and repair required
    # display fields when they are empty. Purpose: keep name and version present
    # while preserving any extra metadata keys a plugin chooses to publish.
    meta = _default_plugin_meta(py_file)
    if isinstance(raw_meta, dict):
        meta.update(raw_meta)
    elif raw_meta is not None:
        logger.warning("Plugin %s has non-dict PLUGIN_META, using defaults", py_file.name)
    if not meta.get("name"):
        meta["name"] = py_file.stem
    if not meta.get("version"):
        meta["version"] = "unknown"
    if meta.get("description") is None:
        meta["description"] = ""
    if meta.get("author") is None:
        meta["author"] = ""
    if meta.get("hooks") is None:
        meta["hooks"] = []
    return meta


def _is_enabled_python_plugin(path: Path) -> bool:
    """Return whether one filesystem entry should be imported as a plugin."""
    # Why: plugin directories may contain __init__.py, private helpers, examples,
    # and disabled files. How: accept only normal .py files that are not private
    # and do not end with .disabled. Purpose: avoid executing files that users did
    # not explicitly enable.
    if not path.is_file():
        return False
    if path.name.startswith("_"):
        return False
    if path.name.endswith(".disabled"):
        return False
    return path.suffix == ".py"


def _is_plugin_package(path: Path) -> bool:
    """Return whether one directory entry is a directory-style plugin.

    Why: a plugin that needs internal modules should not be forced into one
    file. How: a directory with __init__.py is one plugin; its PLUGIN_META and
    entry live in __init__.py while internal modules stay private. Purpose:
    keep the one-plugin-one-entry rule while allowing internal splitting.
    """
    if not path.is_dir():
        return False
    if path.name.startswith(("_", ".")):
        return False
    # [plugin-admin 2026-08-23] Directory plugins honour the same .disabled
    # suffix as .py files. Why: enable/disable renames the entry; without this
    # check a disabled package would keep loading at startup. How: reject names
    # ending in .disabled exactly like _is_enabled_python_plugin does. Purpose:
    # one uniform disable convention for both plugin shapes.
    if path.name.endswith(".disabled"):
        return False
    return (path / "__init__.py").is_file()


def _load_module_from_path(py_file: Path) -> ModuleType:
    """Import a plugin module from an arbitrary file path."""
    # Why: external plugins live in the workspace plugins/ directory, not in an
    # installed package. How: build an importlib spec directly from the file path.
    # Purpose: support simple drop-in plugin files while keeping import failures
    # isolated to the loader's try-except block.
    module_name = f"clonoth_external_plugin_{py_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {py_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_package_from_dir(plugin_dir: Path) -> ModuleType:
    """Import a directory-style plugin as a package rooted at its __init__.py."""
    # Why: directory plugins may use relative imports between internal modules.
    # How: register the package in sys.modules with submodule_search_locations
    # before exec so `from .catalog import x` resolves. Purpose: make internal
    # splitting work exactly like a normal installed package.
    import sys

    module_name = f"clonoth_external_plugin_{plugin_dir.name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def plugin_module_key(entry_name: str) -> str:
    """Stable sys.modules key for one plugin entry."""
    return f"clonoth_external_plugin_{Path(entry_name).stem}"


def drop_plugin_module(entry_name: str) -> None:
    """Remove one plugin entry's module (and submodules) from sys.modules.

    Why: the loaders register packages under a fixed module name, so an
    unloaded plugin's module object would otherwise survive in sys.modules and
    a later reload would re-exec nothing, silently returning the stale object.
    How: pop the package key and every key namespaced under it, then clear the
    parent spec cache entries importlib keeps for file locations. Purpose:
    reload is a genuine re-execution of current on-disk source.
    """
    import sys

    base = plugin_module_key(entry_name)
    for key in [k for k in list(sys.modules) if k == base or k.startswith(base + ".")]:
        sys.modules.pop(key, None)


def _load_one_plugin(hook_registry: HookRegistry, entry: Path, context: Any = None) -> dict:
    """Import one plugin entry and register it. Returns its meta dict.

    Why: startup directory scan and runtime single-plugin load must share one
    code path. How: this is the former loop body of load_external_plugins,
    unchanged; it raises on failure instead of swallowing. Purpose: callers
    decide between best-effort (scan) and strict (admin operation) handling.
    """
    is_package = _is_plugin_package(entry)
    module = _load_package_from_dir(entry) if is_package else _load_module_from_path(entry)
    meta = _normalize_plugin_meta(entry, getattr(module, "PLUGIN_META", {}))

    # Mode 1: PLUGIN_META with handler_class + hook_points
    raw_meta = getattr(module, "PLUGIN_META", None)
    if isinstance(raw_meta, dict) and raw_meta.get("handler_class") and isinstance(raw_meta.get("hook_points"), list):
        class_name = str(raw_meta["handler_class"]).strip()
        cls = getattr(module, class_name)
        # Why: registrations made during load must be reversible,
        # including registrations done in __init__ (e.g. prompt
        # sections). How: instantiate and register inside
        # collecting(name) so the ledger archives each disposer under
        # this plugin; teardown() joins the same ledger. Purpose:
        # unload_plugin(name) undoes the whole load.
        with hook_registry.collecting(meta["name"]):
            instance = cls(context) if context is not None and accepts_context(cls) else cls()
            priority = raw_meta.get("priority", getattr(instance, "priority", None))
            for item in raw_meta["hook_points"]:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    hook_point, method_name = str(item[0]).strip(), str(item[1]).strip()
                    method = getattr(instance, method_name)
                    hook_registry.register(hook_point, method, priority=priority)
            teardown = getattr(instance, "teardown", None)
            if callable(teardown):
                hook_registry.add_plugin_disposer(meta["name"], teardown)
        hook_registry.register_plugin_meta(meta)
        logger.info(
            "Loaded external plugin (PLUGIN_META): %s %s (%s)",
            meta["name"], meta["version"], entry.name,
        )
        return meta

    # Mode 2: Legacy register() function
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError(
            f"Plugin {meta['name']} {meta['version']} ({entry.name}) has no PLUGIN_META handler or register()"
        )
    with hook_registry.collecting(meta["name"]):
        register(_legacy_register_argument(register, hook_registry, context))
    hook_registry.register_plugin_meta(meta)
    logger.info(
        "Loaded external plugin (legacy): %s %s (%s)",
        meta["name"], meta["version"], entry.name,
    )
    return meta


def load_single_plugin(
    hook_registry: HookRegistry,
    plugins_dir: Path,
    entry_name: str,
    context: Any = None,
    event_sink: Any = None,
) -> dict:
    """Load one plugin entry at runtime. Returns its meta dict; raises on failure.

    Why: plugin administration (unload/reload/enable/disable) needs a strict
    single-entry load that the startup scan cannot provide. How: resolve the
    entry name inside plugins_dir with a containment check (no separators, no
    ..), drop any stale sys.modules copy so reload re-executes on-disk source,
    then register through the shared _load_one_plugin path. event_sink, when
    given, is called as event_sink(event_type, payload) after success — the
    loader itself stays process-neutral about where events go. Purpose:
    runtime plugin management without a process restart.
    """
    clean = str(entry_name or "").strip()
    if not clean or "/" in clean or "\\" in clean or clean in {".", ".."} or ".." in Path(clean).parts:
        raise ValueError(f"invalid plugin entry name: {entry_name!r}")
    plugins_dir = Path(plugins_dir).resolve()
    entry = (plugins_dir / clean).resolve()
    if plugins_dir != entry and plugins_dir not in entry.parents:
        raise ValueError(f"plugin entry escapes plugins directory: {entry_name!r}")
    if not _is_plugin_package(entry) and not _is_enabled_python_plugin(entry):
        raise ValueError(f"no enabled plugin entry named {clean!r} under {plugins_dir}")

    drop_plugin_module(clean)
    meta = _load_one_plugin(hook_registry, entry, context)
    if callable(event_sink):
        try:
            event_sink("plugin_loaded", {"plugin": meta.get("name", clean), "entry": clean})
        except Exception as exc:  # event emission must never fail the load
            logger.warning("plugin_loaded event sink failed: %s", exc)
    return meta


def load_external_plugins(hook_registry: HookRegistry, plugins_dir: Path, context: Any = None) -> int:
    """Load enabled external hook plugins from plugins_dir.

    Supports two registration modes (checked in order):
    1. PLUGIN_META auto-discovery — same mechanism as engine/builtin.
    2. Legacy register() function — backward compatible with older plugins.

    Each entry may be a single .py file or a directory with __init__.py
    (one plugin, one entry; internal splitting stays private to the plugin).

    When ``context`` (an EngineContext) is provided, handler classes whose
    __init__ takes an argument receive it, and legacy register() functions
    whose first parameter is named ctx/context/engine_ctx receive it instead
    of the bare hook registry.

    Returns:
        Number of plugin entries loaded successfully.
    """
    count = 0
    plugins_dir = Path(plugins_dir)
    if not plugins_dir.is_dir():
        return count

    for entry in sorted(plugins_dir.iterdir()):
        is_package = _is_plugin_package(entry)
        if not is_package and not _is_enabled_python_plugin(entry):
            continue
        try:
            _load_one_plugin(hook_registry, entry, context)
            count += 1
        except Exception as exc:
            logger.error("Failed to load plugin %s: %s", entry.name, exc, exc_info=True)
    return count


def _legacy_register_argument(register: Any, hook_registry: HookRegistry, context: Any) -> Any:
    """Choose what a legacy register() function receives.

    Why: old plugins declare register(hook_registry); new ones may declare
    register(ctx) to reach every registration surface. How: when a context is
    available and the first parameter is named ctx/context/engine_ctx, pass the
    EngineContext; otherwise pass the hook registry as before. Purpose: adopt
    the new entry point without touching existing plugin files.
    """
    if context is None:
        return hook_registry
    import inspect

    try:
        params = list(inspect.signature(register).parameters.values())
    except (TypeError, ValueError):
        return hook_registry
    if params and params[0].name in {"ctx", "context", "engine_ctx"}:
        return context
    return hook_registry
