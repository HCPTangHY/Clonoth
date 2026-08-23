"""Engine-side plugin context: the single entry point to all registration surfaces.

Why: plugins can register three categories of things — channel (providers:
outbound model backends), interception (hooks: procedural handlers fired at
fixed pipeline points), and declarative (contributions: content/capability
entries the kernel reads on demand). How: EngineContext holds one field per
category; loaders pass it to plugins at load time. Purpose: plugins use the
same ``ctx.<category>...`` pattern regardless of which internal module owns
each surface.

Fields are optional: the supervisor process builds a context with only
``hooks`` populated, since tool/provider/prompt-section registries live in
the engine process.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from engine.signals import Signal, get_bus
from .registry_core import DisposalLedger


@dataclass
class RunContext:
    """单个 task 执行片段的运行上下文。"""

    workspace_root: Path
    supervisor_url: str
    session_id: str
    worker_id: str
    http: httpx.AsyncClient
    llm_http: httpx.AsyncClient
    parent_session_id: str = ""
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-4o-mini"
    user_text: str = ""
    task_id: str = ""
    session_generation: int = 0
    source_inbound_seq: int | None = None
    task_context: dict = field(default_factory=dict)
    child_session_id: str = ""
    tool_call_log: list = field(default_factory=list)
    total_usage: dict = field(default_factory=dict)
    first_shadow_message_id: str = ""
    last_shadow_message_id: str = ""
    completed_steps: int = 0
    current_llm_request_id: str = ""
    llm_request_index: int = 0
    workspace: Path | None = None
    workspace_name: str = ""

    def begin_llm_request(self) -> str:
        self.llm_request_index += 1
        base = self.task_id or self.session_id or "llm"
        self.current_llm_request_id = f"{base}:llm:{self.llm_request_index}:{uuid.uuid4().hex[:8]}"
        return self.current_llm_request_id

    @property
    def is_system_task(self) -> bool:
        return bool((self.task_context or {}).get("is_system_task"))

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.source_inbound_seq is not None:
            payload.setdefault("source_inbound_seq", self.source_inbound_seq)
        if self.is_system_task:
            payload.setdefault("is_system_task", True)
        if self.current_llm_request_id:
            payload.setdefault("llm_request_id", self.current_llm_request_id)
        route_session_id = self.parent_session_id or self.session_id
        if self.parent_session_id and self.parent_session_id != self.session_id:
            payload.setdefault("parent_session_id", self.parent_session_id)
            payload.setdefault("branch_session_id", self.session_id)
        get_bus().emit(Signal(name=event_type, payload=dict(payload)))
        try:
            await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{route_session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception:
            pass

    async def check_cancelled(self) -> bool:
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.session_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def check_preempted(self) -> dict:
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempted"
                )
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {"preempted": False, "message": "", "attachments": []}

    async def consume_preempt(self) -> None:
        try:
            if self.task_id:
                await self.http.post(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempt_consumed"
                )
        except Exception:
            pass


class Contributions:
    """Container for declarative registration faces, mounted by name.

    Why: the declarative category is an open set — adding a face (prompt
    sections, tools, a future frontend manifest registry) must not require
    editing this framework class. How: faces are mounted by name; mount()
    returns an identity-checked disposer and wires the shared disposal ledger
    into faces that support it. Purpose: one entry point for every
    content/capability declaration, including faces contributed by plugins
    themselves.
    """

    def __init__(self) -> None:
        self._faces: dict[str, Any] = {}
        self._ledger: DisposalLedger | None = None

    def set_ledger(self, ledger: DisposalLedger | None) -> None:
        """Attach the shared disposal ledger and propagate it to mounted faces."""
        self._ledger = ledger
        for face in self._faces.values():
            setter = getattr(face, "set_disposal_ledger", None)
            if callable(setter):
                setter(ledger)

    def mount(self, name: str, face: Any) -> Callable[[], None]:
        """Mount one declarative face and return its unmount disposer.

        Why: core faces (prompt sections, tools) are mounted once at startup,
        while plugin-contributed faces must be unloadable with their plugin.
        How: the returned disposer removes only this exact face (identity
        check), detaches its ledger, and — while a loader collecting block is
        active — is archived in the shared ledger under that plugin. Purpose:
        mounting follows the same reversible-registration rule as every other
        surface.
        """
        clean = (name or "").strip()
        if not clean:
            raise ValueError("contribution face name is required")
        if face is None:
            raise ValueError(f"contribution face is None: {clean}")
        self._faces[clean] = face
        if self._ledger is not None:
            setter = getattr(face, "set_disposal_ledger", None)
            if callable(setter):
                setter(self._ledger)

        def _dispose() -> None:
            if self._faces.get(clean) is face:
                del self._faces[clean]
                setter = getattr(face, "set_disposal_ledger", None)
                if callable(setter):
                    setter(None)

        if self._ledger is not None:
            self._ledger.record(_dispose)
        return _dispose

    def get(self, name: str) -> Any | None:
        """Return the face mounted under one name, or None."""
        return self._faces.get(name)

    def list(self) -> list[str]:
        """Return mounted face names in sorted order."""
        return sorted(self._faces)

    def __getattr__(self, name: str) -> Any:
        # Why: ctx.contributions.tools reads better than .get("tools") for the
        # common faces. How: fall back to the mounted-face dict after normal
        # attribute lookup fails. Purpose: attribute sugar without enumerating
        # fields, so new faces stay framework-change-free.
        if name.startswith("_"):
            raise AttributeError(name)
        faces = self.__dict__.get("_faces") or {}
        if name in faces:
            return faces[name]
        raise AttributeError(name)


@dataclass
class EngineContext:
    """References to the three categories of plugin registration surface.

    providers: 渠道型 — outbound model backends (providers.ProviderRegistry).
    hooks: 拦截型 — procedural handlers fired at fixed points (HookRegistry).
    contributions: 声明型 — content/capability faces read on demand.
    """

    providers: Any = None  # providers.ProviderRegistry (engine only)
    hooks: Any = None  # engine.hooks.HookRegistry
    contributions: Contributions = field(default_factory=Contributions)

    def __post_init__(self) -> None:
        # Why: hook registrations and contribution registrations must land in
        # the same per-plugin ledger. How: reuse the HookRegistry's ledger when
        # one is present; otherwise create a standalone one. Purpose: one
        # unload_plugin() call undoes a plugin's effects across all surfaces.
        ledger = getattr(self.hooks, "_ledger", None) if self.hooks is not None else None
        if ledger is None:
            ledger = DisposalLedger()
        self.contributions.set_ledger(ledger)
