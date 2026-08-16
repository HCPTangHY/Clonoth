"""Engine-side plugin context: the single entry point to all registration surfaces.

Why: as hook points and registries multiply, plugins should not need to know
which internal module owns which registry. How: one object holds references to
every registration surface; loaders pass it to plugins at load time. Purpose:
adding a new registry changes this file by one field, and plugins keep using
the same ``ctx.<surface>.register(...)`` pattern.

Fields are optional: the supervisor process builds a context with only
``hooks`` populated, since tool/provider/prompt-section registries live in
the engine process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EngineContext:
    """References to all plugin-facing registration surfaces."""

    hooks: Any = None  # engine.hooks.HookRegistry
    tools: Any = None  # toolbox.registry.ToolRegistry (engine only)
    providers: Any = None  # providers.ProviderRegistry (engine only)
    prompt_sections: Any = None  # engine.inference.prompt_sections.PromptSectionRegistry


def accepts_context(cls: Any) -> bool:
    """Return whether a handler class constructor takes an explicit argument.

    Why: existing handler classes use zero-argument constructors; new ones may
    declare ``__init__(self, ctx)``. How: inspect the signature, ignoring
    *args/**kwargs catch-alls. Purpose: let both loaders inject EngineContext
    without breaking legacy handler classes.
    """
    import inspect

    try:
        init = cls.__init__
        if init is object.__init__:
            return False
        params = [
            p
            for p in inspect.signature(init).parameters.values()
            if p.name != "self"
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        return bool(params)
    except (TypeError, ValueError):
        return False
