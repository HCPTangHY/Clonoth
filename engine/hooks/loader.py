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

logger = logging.getLogger(__name__)


def iter_hook_points(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Validate and return hook point declarations from PLUGIN_META."""
    # Why: malformed metadata should fail one module clearly instead of registering
    # a partial handler. How: require a list of two-item declarations. Purpose:
    # keep auto-discovery predictable and easy to diagnose. Shared by the built-in
    # and external loaders.
    raw_points = meta.get("hook_points")
    if not isinstance(raw_points, list):
        raise ValueError("PLUGIN_META.hook_points must be a list")
    points: list[tuple[str, str]] = []
    for item in raw_points:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"invalid hook point declaration: {item!r}")
        hook_point, method_name = str(item[0]).strip(), str(item[1]).strip()
        if not hook_point or not method_name:
            raise ValueError(f"empty hook point declaration: {item!r}")
        points.append((hook_point, method_name))
    return points


def register_declared_tools(module_name: str, meta: dict[str, Any], tool_registry: Any) -> list[Any]:
    """Register PLUGIN_META.tools declarations into the provided ToolRegistry."""
    # Why: plugins own their tool implementations and schemas. How: when a
    # ToolRegistry is provided, validate each declaration and pass it through
    # register_builtin_tool(). Purpose: loader failures stay localized; returns
    # disposers so callers can archive them in the plugin ledger.
    raw_tools = meta.get("tools")
    if raw_tools is None or tool_registry is None:
        return []
    if not isinstance(raw_tools, list):
        raise ValueError(f"{module_name} PLUGIN_META.tools must be a list")
    register_builtin_tool = getattr(tool_registry, "register_builtin_tool", None)
    if not callable(register_builtin_tool):
        raise TypeError("tool_registry must provide register_builtin_tool")
    disposers: list[Any] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ValueError(f"{module_name} has invalid tool declaration: {tool!r}")
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "")
        input_schema = tool.get("input_schema")
        func = tool.get("func")
        if not name:
            raise ValueError(f"{module_name} tool declaration missing name")
        if not isinstance(input_schema, dict):
            raise ValueError(f"{module_name}.{name} input_schema must be a dict")
        if not callable(func):
            raise ValueError(f"{module_name}.{name} func must be callable")
        dispose = register_builtin_tool(name, description, input_schema, func)
        if callable(dispose):
            disposers.append(dispose)
    return disposers


def register_meta_handler(
    hook_registry: HookRegistry,
    module: ModuleType,
    meta: dict,
    *,
    ledger_name: str,
    context: Any = None,
    tool_registry: Any = None,
) -> Any:
    """Instantiate PLUGIN_META's handler class and register all declarations.

    [plugin-admin 2026-08-23] Shared by the built-in and external loaders so
    both honor one contract: explicit "wants_context": true instantiation (no
    signature sniffing), collecting() attribution, hook-point registration,
    teardown archival, and tool declarations. Returns the handler instance.
    """
    class_name = str(meta.get("handler_class") or "").strip()
    if not class_name:
        raise ValueError("PLUGIN_META.handler_class is required")
    cls = getattr(module, class_name)
    with hook_registry.collecting(ledger_name):
        instance = cls(context) if context is not None and meta.get("wants_context") else cls()
        priority = meta.get("priority", getattr(instance, "priority", None))
        for hook_point, method_name in iter_hook_points(meta):
            method = getattr(instance, method_name)
            hook_registry.register(str(hook_point), method, priority=priority)
        teardown = getattr(instance, "teardown", None)
        if callable(teardown):
            hook_registry.add_plugin_disposer(ledger_name, teardown)
        register_declared_tools(module.__name__, meta, tool_registry)
    return instance


# [plugin-admin 2026-08-23] Per-entry load error table. Why: plugin load
# failures used to be visible only in the process log, so the admin UI could
# only show "not loaded" without the reason. How: scan and single-load record
# failures here keyed by entry stem; a successful load clears the entry.
# Purpose: the plugin manager surfaces the actual ImportError/TypeError.
_LOAD_ERRORS: dict[str, str] = {}


def get_load_error(entry_name: str) -> str:
    """Return the last recorded load error for one entry, or empty string."""
    return _LOAD_ERRORS.get(Path(entry_name).stem, "")


def _resolve_client_assets(plugin_dir: Path, meta: dict) -> None:
    """Inline {"file": relative_path} references inside PLUGIN_META.client.

    Why: slot scripts, annotator scripts, and styles were embedded as Python string literals in
    __init__.py, which loses editor support entirely. How: walk client.slots,
    client.annotators, and client.styles; any value shaped as {"file": "client/x.js"} is read
    from the plugin directory (containment-checked) and replaced by its text.
    Purpose: consumers (the web manifest) always receive plain strings.
    """
    client = meta.get("client")
    if not isinstance(client, dict):
        return
    base = Path(plugin_dir).resolve()

    def _inline(value: Any) -> Any:
        if not (isinstance(value, dict) and isinstance(value.get("file"), str)):
            return value
        rel = value["file"].strip()
        target = (base / rel).resolve()
        if base != target and base not in target.parents:
            raise ValueError(f"client asset escapes plugin directory: {rel!r}")
        return target.read_text(encoding="utf-8")

    slots = client.get("slots")
    if isinstance(slots, list):
        for slot in slots:
            if isinstance(slot, dict) and "script" in slot:
                slot["script"] = _inline(slot["script"])
    annotators = client.get("annotators")
    if isinstance(annotators, list):
        for ann in annotators:
            if isinstance(ann, dict) and "script" in ann:
                ann["script"] = _inline(ann["script"])
    if "styles" in client:
        client["styles"] = _inline(client["styles"])


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
    # [plugin-admin 2026-08-23] Why: directory plugins register themselves in
    # sys.modules but file plugins did not, so runtime admin meta lookup and
    # module-drop-before-reload only worked for packages. How: mirror the
    # package loader and publish the module under the same stable key. Purpose:
    # .py-file and directory plugins behave identically after load.
    import sys

    sys.modules[module_name] = module
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


def _load_one_plugin(
    hook_registry: HookRegistry,
    entry: Path,
    context: Any = None,
    process: str | None = None,
) -> dict:
    """Import one plugin entry and register it. Returns its meta dict.

    Why: startup directory scan and runtime single-plugin load must share one
    code path. How: this is the former loop body of load_external_plugins,
    unchanged; it raises on failure instead of swallowing. Purpose: callers
    decide between best-effort (scan) and strict (admin operation) handling.
    """
    is_package = _is_plugin_package(entry)
    module = _load_package_from_dir(entry) if is_package else _load_module_from_path(entry)
    meta = _normalize_plugin_meta(entry, getattr(module, "PLUGIN_META", {}))

    # [plugin-admin 2026-08-23] Process targeting: PLUGIN_META may declare
    # "processes": ["supervisor"] / ["engine"] so a plugin skips the wrong
    # process explicitly instead of silently no-oping on a missing face.
    targets = meta.get("processes")
    if process and isinstance(targets, list) and targets and process not in targets:
        raise PluginProcessSkip(
            f"plugin {meta['name']} targets processes {targets}, not {process}"
        )

    # [plugin-admin 2026-08-23] Name coherence: the meta name, the entry name,
    # and the sys.modules key are three independent sources. Mismatches used to
    # cause the admin UI to misreport load state (no_tool_finish_guard).
    entry_stem = entry.stem if not is_package else entry.name
    if meta["name"] != entry_stem:
        logger.warning(
            "Plugin name mismatch: PLUGIN_META name %r differs from entry name %r; "
            "admin tooling keys on the meta name",
            meta["name"], entry_stem,
        )

    # [plugin-admin 2026-08-23] Client asset resolution: slots/styles may
    # reference files ({"file": "client/slot.js"}) instead of inlining source
    # in the Python manifest. How: read the file from the plugin directory and
    # substitute the content before meta registration, so consumers always see
    # plain strings. Purpose: plugin JS/CSS live in real files with editor
    # support instead of triple-quoted Python strings.
    _resolve_client_assets(entry if is_package else entry.parent, meta)

    # Mode 1: PLUGIN_META with handler_class + hook_points
    raw_meta = getattr(module, "PLUGIN_META", None)
    if isinstance(raw_meta, dict) and raw_meta.get("handler_class") and isinstance(raw_meta.get("hook_points"), list):
        register_meta_handler(
            hook_registry, module, meta, ledger_name=meta["name"], context=context,
        )
        hook_registry.register_plugin_meta(meta)
        logger.info(
            "Loaded external plugin (PLUGIN_META): %s %s (%s)",
            meta["name"], meta["version"], entry.name,
        )
        return meta

    # Mode 2: register() function. The argument is always the EngineContext
    # (falling back to the bare registry only when no context exists at all).
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError(
            f"Plugin {meta['name']} {meta['version']} ({entry.name}) has no PLUGIN_META handler or register()"
        )
    with hook_registry.collecting(meta["name"]):
        try:
            register(context if context is not None else hook_registry)
        except AttributeError as exc:
            # [plugin-admin 2026-08-23] Legacy plugins wrote
            # register(hook_registry) and call hook_registry.register(...).
            # That call now fails against EngineContext; translate the
            # attribute error into an actionable migration message.
            raise TypeError(
                f"Plugin {meta['name']} uses the legacy register(hook_registry) "
                f"signature. Migrate to register(ctx) and use ctx.hooks.register(...). "
                f"Original error: {exc}"
            ) from exc
    hook_registry.register_plugin_meta(meta)
    logger.info(
        "Loaded external plugin (register): %s %s (%s)",
        meta["name"], meta["version"], entry.name,
    )
    return meta


class PluginProcessSkip(Exception):
    """Raised when a plugin declares processes that exclude the current one."""


def load_single_plugin(
    hook_registry: HookRegistry,
    plugins_dir: Path,
    entry_name: str,
    context: Any = None,
    event_sink: Any = None,
    process: str | None = None,
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
    try:
        meta = _load_one_plugin(hook_registry, entry, context, process=process)
    except PluginProcessSkip:
        raise
    except Exception as exc:
        # [plugin-admin 2026-08-23] record the failure so the admin UI can show it
        _LOAD_ERRORS[Path(clean).stem] = f"{type(exc).__name__}: {exc}"
        raise
    _LOAD_ERRORS.pop(Path(clean).stem, None)
    if callable(event_sink):
        try:
            event_sink("plugin_loaded", {"plugin": meta.get("name", clean), "entry": clean})
        except Exception as exc:  # event emission must never fail the load
            logger.warning("plugin_loaded event sink failed: %s", exc)
    return meta


def load_external_plugins(
    hook_registry: HookRegistry,
    plugins_dir: Path,
    context: Any = None,
    process: str | None = None,
) -> int:
    """Load enabled external hook plugins from plugins_dir.

    Supports two registration modes (checked in order):
    1. PLUGIN_META auto-discovery — same mechanism as engine/builtin.
    2. register(ctx) function — receives the EngineContext.

    Each entry may be a single .py file or a directory with __init__.py
    (one plugin, one entry; internal splitting stays private to the plugin).

    ``process`` identifies the loading process ("engine" or "supervisor");
    plugins declaring PLUGIN_META["processes"] skip mismatched processes.
    Load failures are recorded in the module-level error table (visible via
    get_load_error) and logged, without aborting the remaining scan.

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
            _load_one_plugin(hook_registry, entry, context, process=process)
            _LOAD_ERRORS.pop(entry.stem, None)
            count += 1
        except PluginProcessSkip as exc:
            logger.info("Skipped plugin %s: %s", entry.name, exc)
        except Exception as exc:
            _LOAD_ERRORS[entry.stem] = f"{type(exc).__name__}: {exc}"
            logger.error("Failed to load plugin %s: %s", entry.name, exc, exc_info=True)
    return count
