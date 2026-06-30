from __future__ import annotations

from .registry import HookRegistry
from .types import Handler, HookContext, HookResult
from .loader import load_external_plugins

# Why: built-in handlers need one shared registry that ai_step can fire.
# How: expose a module-level singleton while keeping HookRegistry instantiable for
# tests. Purpose: let production code and tests use the same hook contract.
hook_registry = HookRegistry()

# Why: plugin files import from engine.hooks as their stable public surface.
# How: export the external loader together with the core hook types. Purpose:
# keep the documented plugin protocol compact and compatible with examples.
__all__ = [
    "Handler",
    "HookContext",
    "HookResult",
    "HookRegistry",
    "hook_registry",
    "load_external_plugins",
]
