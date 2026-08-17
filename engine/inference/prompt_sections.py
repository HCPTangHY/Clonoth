"""Prompt section registration surface.

Why: plugins that contribute prompt content should declare what they add instead
of rewriting the message list inside a hook handler. How: a section is a named
content provider with an order and a scope (static = cache-friendly, placed
before history; dynamic = per-turn, placed before the instruction); the
assembler collects registered sections at build time. Purpose: give plugins a
declarative prompt contribution point, parallel to how hooks give them a
procedural interception point.

Framework only: no built-in content is registered here yet. Migrating
knowledge_inject's skill/memory injection onto this surface is a later step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SCOPES = ("static", "dynamic")

# render(ctx) -> str | None; None or empty string means "nothing this turn".
SectionRender = Callable[["PromptSectionContext"], "str | None"]


@dataclass
class PromptSectionContext:
    """Data available to section providers at prompt build time."""

    workspace_root: Path
    node: Any = None
    session_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    instruction: str = ""
    task_context: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Section:
    name: str
    render: SectionRender
    order: int
    scope: str
    meta: dict[str, Any]


class PromptSectionRegistry:
    """Registry of prompt content sections contributed by plugins."""

    def __init__(self) -> None:
        self._sections: dict[str, _Section] = {}

    def register_section(
        self,
        name: str,
        render: SectionRender,
        *,
        order: int = 100,
        scope: str = "dynamic",
        meta: dict[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register one prompt section and return its disposer.

        Why: consistent with hook/tool/provider registration, every registration
        leaves a matching undo operation. How: same-name re-registration
        replaces; the disposer removes only this exact registration, so a stale
        disposer cannot remove a later replacement. Purpose: prompt contributions
        become unloadable like any other plugin effect.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("section name is required")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
        if not callable(render):
            raise TypeError(f"section render is not callable: {clean_name}")
        self._sections[clean_name] = _Section(
            name=clean_name,
            render=render,
            order=int(order),
            scope=scope,
            meta=dict(meta or {}),
        )

        def _dispose() -> None:
            entry = self._sections.get(clean_name)
            if entry is not None and entry.render is render:
                self._sections.pop(clean_name, None)

        # Why: section disposers join the shared per-plugin ledger like hooks
        # and tools. How: record when a ledger is attached and a loader
        # collecting block is active. Purpose: one unload undoes all surfaces.
        ledger = getattr(self, "_disposal_ledger", None)
        if ledger is not None:
            ledger.record(_dispose)
        return _dispose

    def set_disposal_ledger(self, ledger: Any) -> None:
        """Attach the shared disposal ledger (see engine/registry_core.py)."""
        self._disposal_ledger = ledger

    def list_sections(self, scope: str | None = None) -> list[dict[str, Any]]:
        """List registered sections in render order (order asc, name asc)."""
        entries = sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        if scope is not None:
            entries = [s for s in entries if s.scope == scope]
        return [
            {"name": s.name, "order": s.order, "scope": s.scope, "meta": dict(s.meta)}
            for s in entries
        ]

    def render_scope(self, scope: str, ctx: PromptSectionContext) -> list[str]:
        """Render all sections of one scope in order; failures are isolated."""
        parts: list[str] = []
        for section in sorted(self._sections.values(), key=lambda s: (s.order, s.name)):
            if section.scope != scope:
                continue
            try:
                text = section.render(ctx)
            except Exception as exc:
                logger.warning("Prompt section %r render failed: %s", section.name, exc)
                continue
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return parts


# Why: prompt assembly runs in the engine process and needs one shared registry.
# How: module-level singleton, same pattern as providers.registry. Purpose: hook
# handlers register sections on this instance and the assembler reads from it.
prompt_section_registry = PromptSectionRegistry()
