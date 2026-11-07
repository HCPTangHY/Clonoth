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
    """Include effective paths from FastAPI's lazy router wrappers (0.141+).

    Older FastAPI exposes the already-expanded APIRoute objects directly.
    Keep this compatibility detail shared by auth, listing and conflict checks.
    """
    routes: list[Any] = []
    for route in getattr(router, "routes", None) or []:
        expand = getattr(route, "effective_route_contexts", None)
        candidates = expand() if callable(expand) else [route]
        routes.extend(r for r in candidates if getattr(r, "methods", None))
    return routes


class _Declaration:
    """One plugin route declaration and its mount bookkeeping."""

    __slots__ = (
        "owner", "router", "prefix", "public", "description",
        "key", "disposer", "mounted_routes", "public_paths",
    )

    def __init__(
        self,
        owner: str,
        router: Any,
        prefix: str,
        public: bool,
        description: str,
        key: str = "",
    ) -> None:
        self.owner = owner
        self.router = router
        self.prefix = prefix
        self.public = public
        self.description = description
        # Resource key: a re-registration with the same key replaces this
        # declaration instead of adding a second, conflicting one.
        self.key = key
        self.disposer: Callable[[], None] | None = None
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
        # key -> declaration, for re-registrations of the same resource.
        self._by_key: dict[str, _Declaration] = {}
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
        key: str = "",
    ) -> Callable[[], None]:
        """Declare one APIRouter and return its disposer.

        owner comes from the loader's collecting block (callers outside a
        plugin load are rejected); mount overrides the default
        ``/v1/plugins/{owner}`` prefix with ``/v1/{mount}``; public exempts
        the served paths from the global /v1/ auth middleware.

        ``key`` names the resource this router serves. Registering the same key
        again (plugin reload, or a handler re-entered on the same load) replaces
        the previous declaration, so a reload cannot accumulate a second copy
        that then fails the route-conflict check. Declarations without a key are
        always independent — the same owner may mount several routers under one
        prefix (e.g. two ``admin/config`` routers).
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

        clean_key = str(key or "").strip()
        if clean_key:
            previous = self._by_key.pop(clean_key, None)
            if previous is not None:
                # Replace in place: unmount the old router before the new one is
                # mounted, otherwise the conflict check would reject it.
                if previous in self._declarations:
                    self._declarations.remove(previous)
                self._unmount(previous)
                if ledger is not None and previous.disposer is not None:
                    # Its own dispose closure is now stale; it must not remove
                    # the declaration that replaces it.
                    ledger.forget(previous.disposer)

        decl = _Declaration(owner, router, prefix, bool(public), str(description or ""), clean_key)

        def _dispose() -> None:
            if decl in self._declarations:
                self._declarations.remove(decl)
            if decl.key and self._by_key.get(decl.key) is decl:
                self._by_key.pop(decl.key, None)
            self._unmount(decl)

        decl.disposer = _dispose

        # Why: like every registration surface, the disposer joins the shared
        # per-plugin ledger. How: record while the loader's collecting block
        # is active. Purpose: unload_plugin(owner) also removes the routes.
        if ledger is not None:
            ledger.record(_dispose, key=("route", self, clean_key) if clean_key else None)
        self._declarations.append(decl)
        if clean_key:
            self._by_key[clean_key] = decl
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


def static_router(directory: "str | PathLike[str]") -> Any:
    """Build an APIRouter serving static files from one directory.

    Why: plugin panels are plain HTML/JS pages the plugin itself ships; there
    is no build toolchain and no bundler. How: one wildcard GET route resolves
    the requested relative path inside ``directory`` with a resolve()
    containment check (path traversal refused), maps directory requests to
    index.html, and returns a FileResponse (Starlette guesses the media type);
    no directory listing. Keep the declaration authenticated; the web host
    exchanges its Bearer credential for a short-lived panel cookie before
    loading the iframe. Purpose: standalone plugin pages without public files.
    """
    from pathlib import Path

    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse

    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f"static_router: directory does not exist: {root}")

    router = APIRouter()

    @router.api_route("/{file_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _serve_static(file_path: str) -> FileResponse:
        cleaned = (file_path or "").strip("/")
        if not cleaned:
            cleaned = "index.html"
        target = (root / cleaned).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=404, detail="Not Found")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(target, headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"})

    # Only this read-only handler may be reached with a panel cookie. Merely
    # registering an API below /web/ does not give it the same permission.
    _serve_static._clonoth_panel_static = True
    return router
