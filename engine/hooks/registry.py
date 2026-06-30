from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from .types import HookResult

logger = logging.getLogger(__name__)


def _copy_plugin_meta(meta: dict) -> dict:
    """Copy plugin metadata without assuming every extra value is deep-copyable."""
    # Why: PLUGIN_META has simple documented fields, but plugins may publish extra
    # values. How: copy the dict and clone known mutable list fields. Purpose:
    # protect registry state without making unusual extra values break plugin loading.
    copied = dict(meta)
    for key in ("hooks", "hook_points"):
        if isinstance(copied.get(key), list):
            copied[key] = list(copied[key])
    return copied


@dataclass
class _RegisteredHook:
    """One normalized hook registration entry."""

    # Why: engine handlers and supervisor handlers now share one registry. How:
    # store the normalized callable, display name, and priority together. Purpose:
    # keep execution and idempotent replacement independent of handler shape.
    priority: int
    name: str
    callback: Any


class HookRegistry:
    """Unified registry for engine and supervisor hook handlers.

    Why: built-in hook handlers now live in one package and are discovered by
    metadata. How: accept either objects with handle(ctx) or plain callables, then
    expose both sync fire() and async afire(). Purpose: let engine and supervisor
    use the same registration and discovery path while keeping separate process-local
    registry instances.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[_RegisteredHook]] = {}
        # Why: external and built-in plugin files can declare metadata that should
        # be visible after startup. How: keep one normalized dict per plugin name.
        # Purpose: expose loaded plugin information without changing handler execution.
        self._loaded_plugins: list[dict] = []

    def register(self, hook_point: str, handler: Any, priority: int | None = None) -> None:
        """Register one handler to a hook point.

        Why: engine handlers are objects with .handle(ctx), while supervisor
        handlers are bound methods. How: normalize both forms to a callable and
        derive name/priority from the handler or its bound instance. Purpose: make
        repeated auto-discovery idempotent across all built-in hook points.
        """
        point = _normalize_hook_point(hook_point)
        callback = _handler_callback(handler)
        name = _handler_name(handler)
        resolved_priority = _handler_priority(handler, priority)
        handlers = self._hooks.setdefault(point, [])
        handlers[:] = [entry for entry in handlers if entry.name != name]
        handlers.append(_RegisteredHook(priority=resolved_priority, name=name, callback=callback))
        handlers.sort(key=lambda entry: entry.priority, reverse=True)

    def unregister(self, hook_point: str, handler_name: str) -> bool:
        """Remove a handler by name and report whether anything changed."""
        point = _normalize_hook_point(hook_point)
        handlers = self._hooks.get(point)
        if not handlers:
            return False
        before = len(handlers)
        handlers[:] = [entry for entry in handlers if entry.name != handler_name]
        if not handlers:
            self._hooks.pop(point, None)
        return len(handlers) != before

    def register_plugin_meta(self, meta: dict) -> None:
        """Register or replace metadata for one loaded plugin."""
        # Why: startup can scan the same built-in or external plugin repeatedly.
        # How: replace a previous record with the same name instead of appending a
        # duplicate, while storing a copy of the caller's dict. Purpose: make
        # metadata registration idempotent like handler registration.
        stored = _copy_plugin_meta(meta)
        name = stored.get("name") or stored.get("handler_class") or "unknown"
        stored["name"] = name
        for index, plugin in enumerate(self._loaded_plugins):
            if plugin.get("name") == name:
                self._loaded_plugins[index] = stored
                return
        self._loaded_plugins.append(stored)

    def list_plugins(self) -> list[dict]:
        """Return metadata for plugins that loaded successfully."""
        # Why: callers should inspect plugin state without mutating registry internals.
        # How: return copied metadata dicts and copied hook lists. Purpose: keep the
        # registry state owned by HookRegistry.
        return [_copy_plugin_meta(plugin) for plugin in self._loaded_plugins]

    def fire(self, hook_point: str, ctx: Any) -> HookResult:
        """Synchronously run handlers for one hook point.

        Why: supervisor hook points run on synchronous routing and scheduler paths.
        How: call registered callbacks directly and apply the same stop rules as
        engine hooks. Purpose: replace the old supervisor-only registry without making
        supervisor code async.
        """
        modified = False
        for entry in list(self._hooks.get(_normalize_hook_point(hook_point), [])):
            try:
                result = entry.callback(ctx)
                if inspect.isawaitable(result):
                    _close_awaitable(result)
                    raise RuntimeError("async hook handler registered on sync fire(); use afire()")
                stop_result, modified = _process_hook_result(result, modified)
                if stop_result is not None:
                    return stop_result
            except Exception as exc:
                logger.warning("Hook %s.%s failed: %s", hook_point, entry.name, exc)
        return HookResult(modified=modified)

    async def afire(self, hook_point: str, ctx: Any) -> HookResult:
        """Asynchronously run handlers for one hook point."""
        # Why: engine inference handlers can be async and often await runtime checks.
        # How: call each normalized callback and await awaitable results. Purpose:
        # keep engine control-flow semantics while sharing registration with sync hooks.
        modified = False
        for entry in list(self._hooks.get(_normalize_hook_point(hook_point), [])):
            try:
                result = entry.callback(ctx)
                if inspect.isawaitable(result):
                    result = await result
                stop_result, modified = _process_hook_result(result, modified)
                if stop_result is not None:
                    return stop_result
            except Exception as exc:
                logger.warning("Hook %s.%s failed: %s", hook_point, entry.name, exc)
        return HookResult(modified=modified)

    def list_hooks(self) -> dict[str, list[str]]:
        """Return registered hook points and handler names."""
        return {
            hook_point: [entry.name for entry in handlers]
            for hook_point, handlers in self._hooks.items()
        }


def _normalize_hook_point(hook_point: Any) -> str:
    """Convert enum-like or string hook point values to the registry key."""
    # Why: older supervisor code used enum values while the unified registry uses
    # globally unique strings. How: read .value when present and stringify the result.
    # Purpose: keep the transition tolerant without retaining supervisor enums.
    value = getattr(hook_point, "value", hook_point)
    return str(value)


def _handler_callback(handler: Any) -> Any:
    """Return the callable used to execute one handler."""
    # Why: engine handlers are objects with handle(ctx), while supervisor handlers
    # are already bound methods. How: prefer handle when present, otherwise accept
    # the handler itself if callable. Purpose: normalize execution at registration.
    handle = getattr(handler, "handle", None)
    if callable(handle):
        return handle
    if callable(handler):
        return handler
    raise TypeError(f"Hook handler is not callable: {handler!r}")


def _handler_name(handler: Any) -> str:
    """Derive a stable display and idempotency name for a handler."""
    # Why: auto-discovery registers bound methods, whose method name would otherwise
    # hide the owning handler identity. How: prefer explicit name on the handler or
    # bound instance, then fall back to callable metadata. Purpose: repeated scans
    # replace the same handler instead of accumulating duplicates.
    owner = getattr(handler, "__self__", None)
    for source in (handler, owner):
        explicit = getattr(source, "name", None)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
    qualname = getattr(handler, "__qualname__", None)
    if isinstance(qualname, str) and qualname.strip():
        return qualname.strip()
    name = getattr(handler, "__name__", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return handler.__class__.__qualname__


def _handler_priority(handler: Any, priority: int | None) -> int:
    """Resolve handler priority from explicit argument or handler attributes."""
    # Why: existing external plugins rely on handler.priority when they omit a
    # register() priority argument. How: prefer the explicit argument, then the
    # handler or bound instance attribute, then default to 100. Purpose: preserve
    # old engine ordering while supporting supervisor-style callable registration.
    if priority is not None:
        return int(priority)
    owner = getattr(handler, "__self__", None)
    for source in (handler, owner):
        value = getattr(source, "priority", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                break
    return 100


def _process_hook_result(result: Any, modified: bool) -> tuple[Any | None, bool]:
    """Apply shared hook-result chain rules."""
    # Why: sync and async fire must stop on the same result shapes. How: use duck
    # typing for HookResult-compatible objects and aggregate non-terminal mutation.
    # Purpose: support engine.builtin.result.HookResultLike without importing it here.
    if result is None:
        return None, modified
    result_modified = bool(getattr(result, "modified", False))
    modified = modified or result_modified
    should_stop = bool(getattr(result, "block", False) or getattr(result, "skip_step", False) or getattr(result, "action", None) is not None)
    if should_stop:
        if modified and not result_modified and hasattr(result, "modified"):
            result.modified = True
        return result, modified
    return None, modified


def _close_awaitable(value: Any) -> None:
    """Best-effort close for a coroutine accidentally returned in sync fire."""
    # Why: calling an async handler from sync fire creates an unawaited coroutine.
    # How: close coroutine-like values when possible before logging the misuse.
    # Purpose: avoid RuntimeWarning noise while making the incorrect call visible.
    close = getattr(value, "close", None)
    if callable(close):
        close()
