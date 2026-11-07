"""Shared disposal bookkeeping for every plugin registration surface.

Why: hooks, tools, prompt sections, and providers are three different
categories of registration surface (interception, declarative, channel), but
every registration a plugin makes must leave a matching undo operation. How:
one ledger per process; each surface's register() records its disposer here
while a loader is inside collecting(name), so the disposer is attributed to
the plugin being loaded. Purpose: unloading a plugin is "replay its archived
disposers in reverse order", independent of which surfaces it touched.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


class DisposalLedger:
    """Per-process archive of plugin-owned disposers."""

    def __init__(self) -> None:
        self._plugin_disposers: dict[str, dict[object, Callable[[], None]]] = {}
        self._collecting_stack: list[str] = []

    @contextmanager
    def collecting(self, plugin_name: str) -> Iterator[None]:
        """Attribute record() calls inside the block to one plugin."""
        name = (plugin_name or "").strip()
        if not name:
            yield
            return
        self._collecting_stack.append(name)
        try:
            yield
        finally:
            self._collecting_stack.pop()

    def record(self, disposer: Callable[[], None], *, key: object | None = None) -> None:
        """Archive a disposer under the plugin currently being loaded, if any.

        Why: startup wiring (core faces, built-in registries) also produces
        disposers that must not be attributed to a plugin. How: record only
        while a collecting block is active. Purpose: keep plugin unload scoped
        to plugin-owned effects.
        """
        # Keyed registrations replace the live entry on their surface. Forget
        # the old undo closure without calling it: it may belong to a callback
        # currently executing in another task. Unkeyed resources still stack.
        if self._collecting_stack and callable(disposer):
            self.add(self._collecting_stack[-1], disposer, key=key)
        elif key is not None:
            self._forget_key(key)

    def add(self, plugin_name: str, disposer: Callable[[], None], *, key: object | None = None) -> None:
        """Archive an undo callback; an optional surface key replaces its predecessor."""
        name = (plugin_name or "").strip()
        if not name or not callable(disposer):
            return
        if key is not None:
            self._forget_key(key)
        # dict insertion order preserves reverse-order unloading after replacement.
        self._plugin_disposers.setdefault(name, {})[key if key is not None else object()] = disposer

    def _forget_key(self, key: object) -> None:
        for owner, entries in list(self._plugin_disposers.items()):
            entries.pop(key, None)
            if not entries:
                self._plugin_disposers.pop(owner, None)

    def forget(self, disposer: Callable[[], None]) -> None:
        """Drop a callback whose resource has already been disposed explicitly."""
        for owner, entries in list(self._plugin_disposers.items()):
            for key, callback in list(entries.items()):
                if callback is disposer:
                    del entries[key]
            if not entries:
                self._plugin_disposers.pop(owner, None)

    def counts(self) -> dict[str, int]:
        """Metadata-only diagnostics; never expose callbacks or captured data."""
        return {owner: len(entries) for owner, entries in self._plugin_disposers.items()}

    def current_owner(self) -> str | None:
        """Return the plugin currently being loaded, if any.

        Why: registration surfaces need to attribute declarations to the plugin
        being loaded without trusting self-declared metadata. How: expose the
        collecting stack top. Purpose: the routes face derives its owner and
        default prefix from this value.
        """
        return self._collecting_stack[-1] if self._collecting_stack else None

    def owned(self, plugin_name: str) -> int:
        """Return how many disposers are archived for one plugin."""
        return len(self._plugin_disposers.get((plugin_name or "").strip(), []))

    def unload(self, plugin_name: str) -> dict[str, Any]:
        """Replay every disposer owned by one plugin, last-in-first-out.

        Why: a failing disposer must not prevent the remaining undo operations.
        How: call each entry in reverse registration order, recording errors
        instead of raising. Purpose: make unload a mechanical operation with
        per-disposer error isolation.
        """
        name = (plugin_name or "").strip()
        disposers = list(self._plugin_disposers.pop(name, {}).values())
        errors: list[str] = []
        for dispose in reversed(disposers):
            try:
                dispose()
            except Exception as exc:
                logger.warning("Disposer for plugin %s failed: %s", name, exc)
                errors.append(str(exc))
        return {"plugin": name, "disposed": len(disposers), "errors": errors}
