from __future__ import annotations

"""Auto-discovery loader for built-in hook handlers."""

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    """
    base_dir = Path(directory) if directory is not None else Path(__file__).parent
    handlers: dict[str, Any] = {}
    if not base_dir.is_dir():
        return handlers

    # Phase 1: import all modules, collect metadata
    # Why: we need the full set of plugin names before resolving dependencies.
    # How: scan once, store (module, meta, py_file) tuples keyed by handler_name.
    # Purpose: enable Phase 2 to check `requires` against the complete set.
    pending: dict[str, tuple[Any, dict, Path]] = {}  # handler_name -> (module, meta, py_file)
    for py_file in sorted(base_dir.glob("*.py")):
        if _should_skip(py_file):
            continue
        module_name = f"{package}.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
            meta = getattr(module, "PLUGIN_META", None)
            if not isinstance(meta, dict):
                continue
            # Peek at handler_class to derive handler_name for dependency keys
            class_name = str(meta.get("handler_class") or "").strip()
            if not class_name:
                continue
            cls = getattr(module, class_name, None)
            preview_name = str(getattr(cls, "name", "") or "") if cls else ""
            handler_name = preview_name or py_file.stem
            pending[handler_name] = (module, meta, py_file)
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
            instance = _instantiate_handler(module, meta)
            handler_name = str(getattr(instance, "name", "") or py_file.stem)
            handlers[handler_name] = instance
            loaded.add(handler_name)
            priority = meta.get("priority", getattr(instance, "priority", None))
            for hook_point, method_name in _iter_hook_points(meta):
                method = getattr(instance, method_name)
                registry.register(str(hook_point), method, priority=priority)
            _register_declared_tools(module_name, meta, tool_registry)
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


def _instantiate_handler(module: Any, meta: dict[str, Any]) -> Any:
    """Instantiate the handler class named by PLUGIN_META."""
    # Why: each handler module owns its class name through metadata. How: look up
    # handler_class and call it without arguments. Purpose: keep registration
    # declarative while preserving handler-owned state such as timers and cursors.
    class_name = str(meta.get("handler_class") or "").strip()
    if not class_name:
        raise ValueError("PLUGIN_META.handler_class is required")
    cls = getattr(module, class_name)
    return cls()


def _register_declared_tools(module_name: str, meta: dict[str, Any], tool_registry: "ToolRegistry | None") -> None:
    """Register PLUGIN_META.tools declarations into the provided ToolRegistry."""
    # Why: built-in plugins can now own their tool implementations and schemas.
    # How: when a ToolRegistry is provided, validate each metadata declaration and
    # pass it through register_builtin_tool(). Purpose: remove hard-coded knowledge
    # tools from toolbox.registry.py while keeping loader failures localized.
    raw_tools = meta.get("tools")
    if raw_tools is None:
        return
    if tool_registry is None:
        return
    if not isinstance(raw_tools, list):
        raise ValueError(f"{module_name} PLUGIN_META.tools must be a list")

    register_builtin_tool = getattr(tool_registry, "register_builtin_tool", None)
    if not callable(register_builtin_tool):
        raise TypeError("tool_registry must provide register_builtin_tool")

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
        register_builtin_tool(name, description, input_schema, func)


def _iter_hook_points(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Validate and return hook point declarations from PLUGIN_META."""
    # Why: malformed metadata should fail one module clearly instead of registering
    # a partial handler. How: require a list of two-item declarations. Purpose:
    # keep auto-discovery predictable and easy to diagnose.
    raw_points = meta.get("hook_points")
    if not isinstance(raw_points, list):
        raise ValueError("PLUGIN_META.hook_points must be a list")
    points: list[tuple[str, str]] = []
    for item in raw_points:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"invalid hook point declaration: {item!r}")
        hook_point = str(item[0]).strip()
        method_name = str(item[1]).strip()
        if not hook_point or not method_name:
            raise ValueError(f"invalid hook point declaration: {item!r}")
        points.append((hook_point, method_name))
    return points


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
