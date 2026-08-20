"""Plugin HTTP route registration face.

Why: plugins need to serve their own HTTP API (IDE file search, dashboards,
game boards), but the FastAPI app exists only in the supervisor process and
is created after the plugin loaders run. A single-process router service
(the DSH approach) cannot work across Clonoth's process boundary, so this
face is declarative and two-phase: plugins declare APIRouter objects while
loading, and the supervisor mounts them once the app exists.

How: register() attributes each router to the plugin being loaded — taken
from the disposal ledger's collecting stack, never from self-declared
metadata — and computes a mount prefix. The default prefix is
``/v1/plugins/{owner}``; an explicit ``mount=`` puts the router under an
existing REST hierarchy (e.g. ``mount="sessions/{session_id}"`` keeps
``/v1/sessions/{session_id}/retry`` stable if retry ever becomes a plugin).
include_router is deferred until attach(app). Authentication is the
supervisor's global /v1/ middleware: default declarations need no
per-router dependency, and ``public=True`` adds the served paths to the
middleware's exempt set (``app.state.auth_exempt_paths``). Path conflicts
with core or already-mounted plugin routes reject the whole declaration
with an error log; the core always registers first, so plugins cannot
shadow it. Unloading removes exactly the route objects include_router
created and clears the OpenAPI cache.

Handlers reach supervisor state the FastAPI-native way:
``request.app.state.state`` (SupervisorState), ``request.app.state.
workspace_root``. Prefer read-only access; locked methods are off-limits.

WebSocket routes are not supported in this first version; declarations
carrying them log a warning and skip those routes.

Purpose: plugins declare what they serve; the supervisor owns where and
whether it is reachable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _is_websocket(route: Any) -> bool:
    return type(route).__name__ == "WebSocketRoute"


def _iter_http_routes(router: Any) -> list[Any]:
    return [r for r in (getattr(router, "routes", None) or []) if not _is_websocket(r)]


class _Declaration:
    """One plugin route declaration and its mount bookkeeping."""

    __slots__ = (
        "owner", "router", "prefix", "public", "description",
        "mounted_routes", "public_paths",
    )

    def __init__(self, owner: str, router: Any, prefix: str, public: bool, description: str) -> None:
        self.owner = owner
        self.router = router
        self.prefix = prefix
        self.public = public
        self.description = description
        # Route objects include_router created; None while unmounted.
        self.mounted_routes: list[Any] | None = None
        # Full paths this declaration added to the auth exempt set.
        self.public_paths: list[str] = []


class PluginRoutesFace:
    """Declarative HTTP route face for plugins (supervisor process only).

    Two phases: register() during plugin load (routers accumulate), then
    attach(app) once the FastAPI application exists. The engine process
    never mounts this face — ``ctx.contributions.get("routes")`` returns
    None there and plugin code skips route registration.
    """

    def __init__(self) -> None:
        self._declarations: list[_Declaration] = []
        self._app: Any = None
        self._disposal_ledger: Any = None

    # ── ledger wiring ──────────────────────────────────────────────────

    def set_disposal_ledger(self, ledger: Any) -> None:
        """Attach the shared disposal ledger (see engine/registry_core.py)."""
        self._disposal_ledger = ledger

    # ── declaration ────────────────────────────────────────────────────

    def register(
        self,
        router: Any,
        *,
        mount: str | None = None,
        public: bool = False,
        description: str = "",
    ) -> Callable[[], None]:
        """Declare one APIRouter and return its disposer.

        owner comes from the loader's collecting block (callers outside a
        plugin load are rejected); mount overrides the default
        ``/v1/plugins/{owner}`` prefix with ``/v1/{mount}``; public exempts
        the served paths from the global /v1/ auth middleware.
        """
        ledger = self._disposal_ledger
        owner = ledger.current_owner() if ledger is not None else None
        if not owner:
            raise ValueError(
                "routes.register() must be called while a plugin is loading "
                "(inside the loader's collecting block); standalone "
                "registration is not supported"
            )
        clean_mount = (mount or "").strip().strip("/")
        if ".." in clean_mount:
            raise ValueError(f"invalid mount path: {mount!r}")
        prefix = f"/v1/{clean_mount}" if clean_mount else f"/v1/plugins/{owner}"

        decl = _Declaration(owner, router, prefix, bool(public), str(description or ""))

        def _dispose() -> None:
            if decl in self._declarations:
                self._declarations.remove(decl)
            self._unmount(decl)

        # Why: like every registration surface, the disposer joins the shared
        # per-plugin ledger. How: record while the loader's collecting block
        # is active. Purpose: unload_plugin(owner) also removes the routes.
        if ledger is not None:
            ledger.record(_dispose)
        self._declarations.append(decl)
        if self._app is not None:
            self._mount(decl)
        return _dispose

    # ── attach / detach ────────────────────────────────────────────────

    def attach(self, app: Any, *, workspace_root: Any = None) -> None:
        """Mount every accumulated declaration onto one FastAPI app."""
        if self._app is app:
            return
        if self._app is not None:
            self.detach()
        self._app = app
        if workspace_root is not None and getattr(app.state, "workspace_root", None) is None:
            app.state.workspace_root = workspace_root
        for decl in list(self._declarations):
            self._mount(decl)

    def detach(self) -> None:
        """Unmount every declaration and forget the app."""
        for decl in list(self._declarations):
            self._unmount(decl)
        self._app = None

    # ── mounting internals ─────────────────────────────────────────────

    def _app_signatures(self) -> set[tuple[str, str]]:
        """(path, method) pairs currently registered on the app."""
        sigs: set[tuple[str, str]] = set()
        for route in _iter_http_routes(self._app.router if self._app is not None else []):
            for method in getattr(route, "methods", None) or set():
                if method == "HEAD":
                    continue
                sigs.add((getattr(route, "path", ""), str(method)))
        return sigs

    def _declaration_signatures(self, decl: _Declaration) -> set[tuple[str, str]]:
        sigs: set[tuple[str, str]] = set()
        for route in _iter_http_routes(decl.router):
            for method in getattr(route, "methods", None) or set():
                if method == "HEAD":
                    continue
                sigs.add((decl.prefix + getattr(route, "path", ""), str(method)))
        return sigs

    def _mount(self, decl: _Declaration) -> bool:
        """include_router one declaration with conflict detection."""
        app = self._app
        if app is None:
            return False
        if decl.mounted_routes is not None:
            return True

        skipped_ws = [r for r in (getattr(decl.router, "routes", None) or []) if _is_websocket(r)]
        for _ws in skipped_ws:
            logger.warning(
                "Plugin %s declared a WebSocket route under %s; "
                "WebSocket is not supported by the routes face yet, skipping",
                decl.owner, decl.prefix,
            )

        conflicts = self._declaration_signatures(decl) & self._app_signatures()
        if conflicts:
            logger.error(
                "Plugin route declaration %s (owner=%s) rejected; "
                "conflicts with existing routes: %s",
                decl.prefix, decl.owner, sorted(conflicts),
            )
            return False

        old_len = len(app.routes)
        app.include_router(decl.router, prefix=decl.prefix)
        decl.mounted_routes = list(app.routes[old_len:])
        app.openapi_schema = None

        if decl.public:
            exempt = getattr(app.state, "auth_exempt_paths", None)
            if isinstance(exempt, set):
                for route in _iter_http_routes(decl.router):
                    full = decl.prefix + getattr(route, "path", "")
                    decl.public_paths.append(full)
                    exempt.add(full)
        return True

    def _unmount(self, decl: _Declaration) -> None:
        """Remove exactly the route objects this declaration added."""
        app = self._app
        if decl.mounted_routes is not None and app is not None:
            mounted_ids = {id(r) for r in decl.mounted_routes}
            app.router.routes[:] = [r for r in app.router.routes if id(r) not in mounted_ids]
            decl.mounted_routes = None
            app.openapi_schema = None
        if decl.public_paths:
            exempt = getattr(app.state, "auth_exempt_paths", None) if app is not None else None
            if isinstance(exempt, set):
                for path in decl.public_paths:
                    exempt.discard(path)
            decl.public_paths = []

    # ── inspection ─────────────────────────────────────────────────────

    def list_routes(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Serializable records of declarations, optionally filtered by owner."""
        records: list[dict[str, Any]] = []
        for decl in self._declarations:
            if owner and decl.owner != owner:
                continue
            routes: list[dict[str, str]] = []
            for route in _iter_http_routes(decl.router):
                for method in sorted(getattr(route, "methods", None) or set()):
                    if method == "HEAD":
                        continue
                    routes.append({
                        "method": method,
                        "path": decl.prefix + getattr(route, "path", ""),
                        "summary": str(getattr(route, "summary", "") or ""),
                    })
            records.append({
                "owner": decl.owner,
                "prefix": decl.prefix,
                "public": decl.public,
                "description": decl.description,
                "mounted": decl.mounted_routes is not None,
                "routes": routes,
            })
        return records
