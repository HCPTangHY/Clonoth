"""Plugin static panel short-lived auth: migration checks.

Run: python3.11 -B tests/test_panel_auth_migration.py

Every app in this file is a throwaway FastAPI instance built in a temp dir; the
admin credential is a fake in-process string (``admin_api._admin_token``), so no
real token is read, no service is contacted and nothing is persisted.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import json
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api
import supervisor.panel_auth as panel_auth
import supervisor.api as supervisor_api
from engine.faces.routes import PluginRoutesFace, _iter_http_routes, static_router
from engine.registry_core import DisposalLedger
from supervisor.panel_auth import PanelAuth, cookie_name, cookie_path, static_owner

ADMIN_TOKEN = "fake-admin-token-for-panel-auth-tests"
HOST = "panel.test"
STATIC_TEXT = "<html>plugin panel</html>"


# ── fixtures ─────────────────────────────────────────────────────────────────


async def _health() -> dict:
    return {"ok": True}


async def _config() -> dict:
    return {"config": "service-api"}


async def _api_payload() -> dict:
    return {"secret": "panel-api"}


def _write_panel(root: Path) -> Path:
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text(STATIC_TEXT, encoding="utf-8")
    (web / "app.js").write_text("console.log('panel')", encoding="utf-8")
    return web


def build_app(root: Path, *, public_static: bool = False) -> FastAPI:
    """Temp supervisor-shaped app: same exempt set, auth middleware, panel auth.

    The middleware body mirrors supervisor/api.py's (which cannot be imported
    here without a real SupervisorState).
    """
    app = FastAPI()
    exempt = {"/v1/admin/auth/status", "/v1/health"}
    app.state.auth_exempt_paths = exempt
    app.add_api_route("/v1/health", _health, methods=["GET"])
    app.add_api_route("/v1/config", _config, methods=["GET"])
    app.state.panel_auth = PanelAuth(app)

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        path = request.url.path
        is_exempt = path in exempt or any(
            "{" in pattern and supervisor_api._path_matches_template(path, pattern)
            for pattern in exempt
        )
        if path.startswith("/v1/") and not is_exempt:
            try:
                admin_api.verify_admin_token(request)
            except HTTPException:
                if app.state.panel_auth.permits(request):
                    return await call_next(request)
                return Response(
                    content=json.dumps({"detail": "Unauthorized"}),
                    status_code=401,
                    media_type="application/json",
                )
        return await call_next(request)

    face = PluginRoutesFace()
    ledger = DisposalLedger()
    face.set_disposal_ledger(ledger)
    for owner in ("demo", "other"):
        panel = APIRouter()
        panel.include_router(static_router(_write_panel(root / owner)), prefix="/web")
        api = APIRouter()
        api.add_api_route("/api/data", _api_payload, methods=["GET"])
        with ledger.collecting(owner):
            face.register(panel, public=public_static, description=f"{owner} panel static")
            face.register(api, description=f"{owner} panel api")
    face.attach(app)
    app.state.routes_face = face
    app.state.disposal_ledger = ledger
    return app


def _client(app: FastAPI, scheme: str = "http") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://{HOST}")


def _issued(response: httpx.Response) -> tuple[str, str]:
    """(cookie name, cookie value) from the Set-Cookie header."""
    raw = response.headers.get("set-cookie", "")
    name, _, rest = raw.partition("=")
    return name.strip(), rest.split(";")[0]


def _request(
    app: FastAPI,
    path: str,
    *,
    method: str = "GET",
    scheme: str = "http",
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [(b"host", HOST.encode())]
    if cookie:
        raw_headers.append((b"cookie", cookie.encode()))
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 45678),
        "server": (HOST, 80),
        "app": app,
    }
    return Request(scope)


class _FakeWebAuth:
    """Stand-in for WebAuthManager; returns one canned JWT payload."""

    available = True
    setup_completed = True

    def __init__(self, payload: dict | None) -> None:
        self._payload = payload

    def verify_jwt(self, token: str) -> dict | None:
        return self._payload


class _PanelCase(unittest.IsolatedAsyncioTestCase):
    """Shared temp app + fake admin credential."""

    public_static = False

    def setUp(self) -> None:
        admin_api._admin_token = ADMIN_TOKEN
        admin_api._web_auth = None
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.app = build_app(self.root, public_static=self.public_static)
        self.auth: PanelAuth = self.app.state.panel_auth

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------
    async def _issue(self, *, entry: str = "/v1/plugins/demo/web/index.html",
                     token: str = ADMIN_TOKEN, scheme: str = "http",
                     headers: dict[str, str] | None = None,
                     url_path: str = "/v1/admin/auth/panel-session") -> httpx.Response:
        merged = {"Authorization": f"Bearer {token}"} if token else {}
        merged.update(headers or {})
        async with _client(self.app, scheme) as client:
            return await client.post(url_path, json={"entry": entry}, headers=merged)

    async def _get(self, path: str, *, method: str = "GET", cookie: str | None = None,
                   scheme: str = "http", headers: dict[str, str] | None = None) -> httpx.Response:
        async with _client(self.app, scheme) as client:
            extra = dict(headers or {})
            if cookie:
                extra["Cookie"] = cookie
            return await client.request(method, path, headers=extra)


# ── issue / renew / revoke ───────────────────────────────────────────────────


class PanelSessionLifecycleTests(_PanelCase):
    async def test_issue_sets_short_httponly_strict_cookie(self) -> None:
        response = await self._issue()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "expires_in": panel_auth.TTL})
        self.assertLessEqual(response.json()["expires_in"], 120)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        name, value = _issued(response)
        self.assertEqual(name, cookie_name("demo", "http"))
        self.assertTrue(value)
        raw = response.headers["set-cookie"].lower()
        self.assertIn("httponly", raw)
        self.assertIn("samesite=strict", raw)
        self.assertIn(f"path={cookie_path('demo')}", raw)
        self.assertIn(f"max-age={panel_auth.TTL}", raw)
        self.assertNotIn("secure", raw)  # http scheme must not mark Secure

    async def test_issue_over_https_marks_secure_and_names_cookie_by_scheme(self) -> None:
        response = await self._issue(scheme="https", headers={"Origin": f"https://{HOST}"})
        self.assertEqual(response.status_code, 200, response.text)
        name, _ = _issued(response)
        self.assertEqual(name, cookie_name("demo", "https"))
        self.assertIn("secure", response.headers["set-cookie"].lower())

    async def test_entry_query_string_is_dropped_and_still_issues(self) -> None:
        response = await self._issue(entry="/v1/plugins/demo/web/index.html?tab=1")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_renew_reuses_grant_and_extends_expiry(self) -> None:
        first = await self._issue()
        name, value = _issued(first)
        self.assertEqual(len(self.auth.grants), 1)
        self.auth.grants[value].expires = time.monotonic() + 1
        renewed = await self._issue()
        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(_issued(renewed), (name, value))
        self.assertEqual(len(self.auth.grants), 1)
        self.assertGreater(self.auth.grants[value].expires, time.monotonic() + 60)

    async def test_concurrent_same_plugin_issues_do_not_invalidate_each_other(self) -> None:
        responses = await asyncio.gather(*[self._issue() for _ in range(6)])
        self.assertTrue(all(r.status_code == 200 for r in responses), [r.status_code for r in responses])
        cookies = {_issued(r) for r in responses}
        self.assertEqual(len(cookies), 1, "same owner + credential must share one grant")
        self.assertEqual(len(self.auth.grants), 1)
        # A stale response landing last cannot revoke the other panel windows.
        for _, value in cookies:
            ok = await self._get("/v1/plugins/demo/web/index.html",
                                 cookie=f"{cookie_name('demo', 'http')}={value}")
            self.assertEqual(ok.status_code, 200)

    async def test_revoke_clears_grant_and_cookie(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        cookie = f"{name}={value}"
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 200)

        async with _client(self.app) as client:
            revoked = await client.request(
                "DELETE", "/v1/admin/auth/panel-session",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json(), {"ok": True})
        self.assertIn("max-age=0", revoked.headers["set-cookie"].lower())
        self.assertEqual(self.auth.grants, {})
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 401)

    async def test_revoke_only_touches_the_calling_credential(self) -> None:
        issued = await self._issue(entry="/v1/plugins/demo/web/index.html")
        name, value = _issued(issued)
        # a second credential's grant for another panel must survive the revoke
        foreign = hashlib.sha256(b"other-credential").hexdigest()
        self.auth.grants["foreign-grant"] = panel_auth.Grant(
            "other", foreign, "http", time.monotonic() + 60)
        async with _client(self.app) as client:
            revoked = await client.request(
                "DELETE", "/v1/admin/auth/panel-session",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(list(self.auth.grants), ["foreign-grant"])
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html",
                                          cookie=f"{name}={value}")).status_code, 401)
        survivor = await self._get("/v1/plugins/other/web/index.html",
                                   cookie=f"{cookie_name('other', 'http')}=foreign-grant")
        self.assertEqual(survivor.status_code, 200)

    async def test_capacity_limit_refuses_new_owner(self) -> None:
        with patch.object(panel_auth, "MAX_GRANTS", 1):
            self.assertEqual((await self._issue()).status_code, 200)
            response = await self._issue(entry="/v1/plugins/other/web/index.html")
        self.assertEqual(response.status_code, 503)


# ── cookie scope: what a panel cookie may reach ──────────────────────────────


class PanelCookieScopeTests(_PanelCase):
    async def _cookie(self, owner: str = "demo", *, scheme: str = "http") -> str:
        response = await self._issue(entry=f"/v1/plugins/{owner}/web/index.html", scheme=scheme,
                                     headers={"Origin": f"{scheme}://{HOST}"})
        self.assertEqual(response.status_code, 200, response.text)
        name, value = _issued(response)
        return f"{name}={value}"

    async def test_marked_static_handler_allows_get_and_head(self) -> None:
        cookie = await self._cookie()
        for method in ("GET", "HEAD"):
            response = await self._get("/v1/plugins/demo/web/index.html", method=method, cookie=cookie)
            self.assertEqual(response.status_code, 200, f"{method} {response.text}")
            self.assertEqual(response.headers.get("cache-control"), "private, no-store")
            vary = response.headers.get("vary", "").lower()
            self.assertIn("authorization", vary)
            self.assertIn("cookie", vary)
        body = await self._get("/v1/plugins/demo/web/app.js", cookie=cookie)
        self.assertEqual(body.status_code, 200)

    async def test_cookie_does_not_authorize_ordinary_plugin_api(self) -> None:
        cookie = await self._cookie()
        response = await self._get("/v1/plugins/demo/api/data", cookie=cookie)
        self.assertEqual(response.status_code, 401)

    async def test_cookie_does_not_authorize_service_api(self) -> None:
        cookie = await self._cookie()
        for path in ("/v1/config", "/v1/sessions", "/v1/admin/state"):
            self.assertEqual((await self._get(path, cookie=cookie)).status_code, 401, path)

    async def test_non_static_methods_are_refused(self) -> None:
        cookie = await self._cookie()
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response = await self._get("/v1/plugins/demo/web/index.html", method=method, cookie=cookie)
            self.assertIn(response.status_code, (401, 405), f"{method} -> {response.status_code}")

    async def test_unregistered_path_under_web_prefix_is_refused(self) -> None:
        cookie = await self._cookie()
        # /v1/plugins/ghost/web/... matches the path regex but has no handler.
        self.assertEqual((await self._get("/v1/plugins/ghost/web/index.html", cookie=cookie)).status_code, 401)

    async def test_other_owner_cookie_name_cannot_reuse_grant(self) -> None:
        cookie = await self._cookie()
        value = cookie.split("=", 1)[1]
        stolen = f"{cookie_name('other', 'http')}={value}"
        self.assertEqual((await self._get("/v1/plugins/other/web/index.html", cookie=stolen)).status_code, 401)
        # the same value under its own owner name still works
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 200)

    async def test_cross_site_fetch_metadata_is_refused(self) -> None:
        cookie = await self._cookie()
        response = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                   headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 401)

    async def test_cross_site_fetch_metadata_alone_blocks_cookie(self) -> None:
        cookie = await self._cookie()
        for value in ("cross-site", "same-site"):
            response = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                       headers={"Sec-Fetch-Site": value})
            self.assertEqual(response.status_code, 401 if value == "cross-site" else 200)

    async def test_explicit_origin_cannot_bypass_the_panel_cookie(self) -> None:
        cookie = await self._cookie()
        # _bearer already refuses these; a plain GET must not be a quieter door.
        for origin in ("http://evil.test", f"https://{HOST}", f"http://{HOST}:8443",
                       f"http://sub.{HOST}", "null"):
            response = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                       headers={"Origin": origin})
            self.assertEqual(response.status_code, 401, origin)
            self.assertIn("Unauthorized", response.text)

    async def test_origin_free_and_same_origin_fetches_stay_usable(self) -> None:
        cookie = await self._cookie()
        # A same-origin iframe document load carries no Origin header at all.
        for headers in ({}, {"Origin": f"http://{HOST}"},
                        {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"}):
            response = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                       headers=headers)
            self.assertEqual(response.status_code, 200, headers)
        head = await self._get("/v1/plugins/demo/web/index.html", method="HEAD", cookie=cookie)
        self.assertEqual(head.status_code, 200)

    async def test_public_static_declaration_stays_exempt_and_needs_no_cookie(self) -> None:
        app = build_app(self.root / "public", public_static=True)
        async with _client(app) as client:
            response = await client.get("/v1/plugins/demo/web/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/v1/plugins/demo/web/{file_path:path}", app.state.auth_exempt_paths)

    async def test_bearer_still_reaches_everything(self) -> None:
        response = await self._get("/v1/plugins/demo/api/data",
                                   headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"secret": "panel-api"})


# ── issuing rules: Bearer only, same-origin only, static entries only ────────


class PanelIssueBoundaryTests(_PanelCase):
    async def test_panel_cookie_cannot_bootstrap_a_new_grant(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        async with _client(self.app) as client:
            response = await client.post(
                "/v1/admin/auth/panel-session",
                json={"entry": "/v1/plugins/demo/web/index.html"},
                headers={"Cookie": f"{name}={value}"},
            )
        self.assertEqual(response.status_code, 401)

    async def test_query_token_cannot_bootstrap_a_grant(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                f"/v1/admin/auth/panel-session?token={ADMIN_TOKEN}",
                json={"entry": "/v1/plugins/demo/web/index.html"},
            )
        self.assertEqual(response.status_code, 401)

    async def test_cookie_cannot_revoke(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        async with _client(self.app) as client:
            response = await client.request("DELETE", "/v1/admin/auth/panel-session",
                                            headers={"Cookie": f"{name}={value}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.auth.grants), 1)

    async def test_missing_bearer_is_rejected(self) -> None:
        self.assertEqual((await self._issue(token="")).status_code, 401)
        self.assertEqual((await self._issue(token="wrong-token")).status_code, 401)

    async def test_non_same_origin_entry_is_rejected(self) -> None:
        for entry in (
            "http://evil.test/v1/plugins/demo/web/index.html",
            "https://evil.test/v1/plugins/demo/web/index.html",
            "//evil.test/v1/plugins/demo/web/index.html",
            "/v1/plugins/demo/web/index.html#fragment",
        ):
            response = await self._issue(entry=entry)
            self.assertEqual(response.status_code, 400, entry)

    async def test_non_static_entries_are_rejected(self) -> None:
        for entry in (
            "/v1/config",
            "/v1/plugins/demo/api/data",
            "/v1/plugins/demo/web/../api/data",
            "/v1/plugins/demo/web/%2e%2e/api/data",
            "/v1/plugins/demo/web2/index.html",
            "/v1/plugins/demo/index.html",
        ):
            response = await self._issue(entry=entry)
            self.assertEqual(response.status_code, 400, entry)
        self.assertEqual(self.auth.grants, {})

    async def test_entry_body_validation(self) -> None:
        async with _client(self.app) as client:
            empty = await client.post("/v1/admin/auth/panel-session", json={},
                                      headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
            too_long = await client.post("/v1/admin/auth/panel-session",
                                         json={"entry": "/v1/plugins/demo/web/" + "a" * 4096},
                                         headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(too_long.status_code, 422)

    async def test_cross_origin_issue_is_refused(self) -> None:
        for headers in ({"Origin": "http://evil.test"},
                        {"Origin": "null"},
                        {"Origin": f"https://{HOST}"},
                        {"Sec-Fetch-Site": "cross-site"}):
            response = await self._issue(headers=headers)
            self.assertEqual(response.status_code, 403, headers)
        self.assertEqual(self.auth.grants, {})

    async def test_same_origin_headers_are_accepted(self) -> None:
        response = await self._issue(headers={"Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200, response.text)


# ── credential handling: hashed, bounded, expiring ───────────────────────────


class PanelCredentialTests(_PanelCase):
    async def test_only_the_credential_hash_is_kept(self) -> None:
        response = await self._issue()
        _, value = _issued(response)
        grant = self.auth.grants[value]
        self.assertEqual(grant.credential, hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest())
        self.assertNotIn(ADMIN_TOKEN, repr(self.auth.grants))
        self.assertNotIn(ADMIN_TOKEN, json.dumps(response.json()))
        self.assertNotIn(ADMIN_TOKEN, response.headers["set-cookie"])
        self.assertNotIn(ADMIN_TOKEN, value)

    async def test_jwt_ttl_is_clamped_to_the_jwt_remaining_lifetime(self) -> None:
        fake = _FakeWebAuth({"exp": time.time() + 30})
        with patch.object(panel_auth, "get_web_auth", return_value=fake):
            response = await self._issue()
        self.assertEqual(response.status_code, 200, response.text)
        ttl = response.json()["expires_in"]
        self.assertLessEqual(ttl, 30)
        self.assertGreaterEqual(ttl, 28)
        self.assertLessEqual(ttl, panel_auth.TTL)
        self.assertIn(f"max-age={ttl}", response.headers["set-cookie"].lower())
        self.assertLess(self.auth.grants[next(iter(self.auth.grants))].expires,
                        time.monotonic() + ttl + 1)

    async def test_expired_jwt_cannot_issue(self) -> None:
        fake = _FakeWebAuth({"exp": time.time() - 5})
        with patch.object(panel_auth, "get_web_auth", return_value=fake):
            response = await self._issue()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.auth.grants, {})

    async def test_expired_grant_stops_permitting_and_is_pruned(self) -> None:
        issued = await self._issue()
        _, value = _issued(issued)
        cookie = f"{cookie_name('demo', 'http')}={value}"
        self.auth.grants[value].expires = time.monotonic() - 1
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 401)
        self.assertEqual((await self._issue()).status_code, 200)
        # the stale grant was pruned, so re-issuing allocates a fresh key
        self.assertEqual(len(self.auth.grants), 1)
        self.assertNotIn(value, self.auth.grants)
        self.assertGreater(next(iter(self.auth.grants.values())).expires, time.monotonic())
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 401)


# ── permit()/static_owner() boundary units ───────────────────────────────────


class PanelPermitBoundaryTests(_PanelCase):
    async def test_permits_requires_marked_route_and_get_or_head(self) -> None:
        self.assertEqual(static_owner(_request(self.app, "/v1/plugins/demo/web/index.html")), "demo")
        self.assertEqual(static_owner(_request(self.app, "/v1/plugins/other/web/app.js")), "other")
        self.assertIsNone(static_owner(_request(self.app, "/v1/plugins/demo/api/data")))
        self.assertIsNone(static_owner(_request(self.app, "/v1/plugins/ghost/web/index.html")))
        self.assertIsNone(static_owner(_request(self.app, "/v1/plugins/demo/web/index.html", method="POST")))

    async def test_path_tricks_are_not_marked_static(self) -> None:
        for path in (
            "/v1/plugins/demo/web/../api/data",
            "/v1/plugins/demo/web/./index.html",
            "/v1/plugins/demo/web/%2e%2e/api/data",
            "/v1/plugins/demo/web/index.html%00",
            "/v1/plugins/demo/web\\..\\api\\data",
            "/v1/plugins/demo/webx/index.html",
        ):
            self.assertIsNone(static_owner(_request(self.app, path)), path)

    async def test_scheme_mismatch_is_refused(self) -> None:
        issued = await self._issue(scheme="https", headers={"Origin": f"https://{HOST}"})
        name, value = _issued(issued)
        https_scope = _request(self.app, "/v1/plugins/demo/web/index.html", scheme="https",
                               cookie=f"{name}={value}")
        self.assertTrue(self.auth.permits(https_scope))
        # the same grant value presented over plain http (other cookie name) is inert
        http_scope = _request(self.app, "/v1/plugins/demo/web/index.html", scheme="http",
                              cookie=f"{cookie_name('demo', 'http')}={value}")
        self.assertFalse(self.auth.permits(http_scope))

    async def test_permits_rejects_any_explicit_cross_origin(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        cookie = f"{name}={value}"
        path = "/v1/plugins/demo/web/index.html"
        for origin in ("http://evil.test", f"https://{HOST}", f"http://{HOST}:8443",
                       f"http://sub.{HOST}", "null"):
            self.assertFalse(self.auth.permits(_request(self.app, path, cookie=cookie,
                                                       headers={"Origin": origin})), origin)
        self.assertTrue(self.auth.permits(_request(self.app, path, cookie=cookie,
                                                   headers={"Origin": f"http://{HOST}"})))
        self.assertTrue(self.auth.permits(_request(self.app, path, cookie=cookie)))

    async def test_permits_false_without_cookie(self) -> None:
        self.assertFalse(self.auth.permits(_request(self.app, "/v1/plugins/demo/web/index.html")))

    async def test_permits_false_for_unknown_cookie_value(self) -> None:
        await self._issue()
        forged = _request(self.app, "/v1/plugins/demo/web/index.html",
                          cookie=f"{cookie_name('demo', 'http')}=forged-value")
        self.assertFalse(self.auth.permits(forged))

    async def test_permits_refuses_cross_site_metadata_with_a_same_origin_origin(self) -> None:
        # Both signals are evaluated independently: a matching Origin cannot
        # whitewash an explicit cross-site Sec-Fetch-Site.
        issued = await self._issue()
        name, value = _issued(issued)
        request = _request(self.app, "/v1/plugins/demo/web/index.html",
                           cookie=f"{name}={value}",
                           headers={"Origin": f"http://{HOST}", "Sec-Fetch-Site": "cross-site"})
        self.assertFalse(self.auth.permits(request))

    async def test_permits_https_same_origin_is_accepted(self) -> None:
        issued = await self._issue(scheme="https", headers={"Origin": f"https://{HOST}"})
        name, value = _issued(issued)
        same = _request(self.app, "/v1/plugins/demo/web/index.html", scheme="https",
                        cookie=f"{name}={value}", headers={"Origin": f"https://{HOST}"})
        self.assertTrue(self.auth.permits(same))
        downgraded = _request(self.app, "/v1/plugins/demo/web/index.html", scheme="https",
                             cookie=f"{name}={value}", headers={"Origin": f"http://{HOST}"})
        self.assertFalse(self.auth.permits(downgraded))

    async def test_permits_origin_match_is_exact_not_normalised(self) -> None:
        # A default-port Origin string is a different string than the request's
        # own scheme://netloc, so it stays refused: the panel cookie never gets
        # to be more permissive than the Bearer path that issued it.
        issued = await self._issue()
        name, value = _issued(issued)
        for origin in (f"http://{HOST}:80", f"HTTP://{HOST.upper()}", f"http://{HOST}/"):
            self.assertFalse(self.auth.permits(_request(
                self.app, "/v1/plugins/demo/web/index.html", cookie=f"{name}={value}",
                headers={"Origin": origin})), origin)

    async def test_permits_and_bearer_share_one_same_origin_rule(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        path = "/v1/plugins/demo/web/index.html"
        for headers in ({}, {"Origin": f"http://{HOST}"}, {"Origin": "http://evil.test"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-origin"}):
            request = _request(self.app, path, cookie=f"{name}={value}", headers=headers)
            self.assertEqual(panel_auth.cross_origin(request), not self.auth.permits(request),
                             headers)

    async def test_cross_origin_refusal_does_not_consume_the_grant(self) -> None:
        issued = await self._issue()
        name, value = _issued(issued)
        cookie = f"{name}={value}"
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                          headers={"Origin": "http://evil.test"})).status_code, 401)
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html",
                                          cookie=cookie)).status_code, 200)


# ── routes face: iter compatibility + machine-readable marking ──────────────


class RoutesFaceCompatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        admin_api._admin_token = ADMIN_TOKEN
        admin_api._web_auth = None
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_static_handler_is_marked_read_only(self) -> None:
        app = build_app(self.root)
        marked = [r for r in _iter_http_routes(app.router)
                  if getattr(getattr(r, "endpoint", None), "_clonoth_panel_static", False)]
        self.assertEqual(len(marked), 2)  # demo + other
        for route in marked:
            self.assertEqual(set(route.methods), {"GET", "HEAD"})
            self.assertRegex(route.path, r"^/v1/plugins/[A-Za-z0-9_-]+/web/\{file_path:path\}$")

    async def test_plugin_api_routes_are_not_marked(self) -> None:
        app = build_app(self.root)
        api_route = next(r for r in _iter_http_routes(app.router)
                         if getattr(r, "path", "") == "/v1/plugins/demo/api/data")
        self.assertFalse(getattr(getattr(api_route, "endpoint", None), "_clonoth_panel_static", False))

    async def test_iter_expands_lazy_router_wrappers(self) -> None:
        route = SimpleNamespace(path="/v1/plugins/demo/web/{file_path:path}", methods={"GET"})
        wrapper = SimpleNamespace(effective_route_contexts=lambda: [route])
        self.assertEqual(_iter_http_routes(SimpleNamespace(routes=[wrapper])), [route])
        app = build_app(self.root)
        paths = {getattr(r, "path", "") for r in _iter_http_routes(app.router)}
        self.assertIn("/v1/plugins/demo/web/{file_path:path}", paths)

    async def test_iter_accepts_old_style_plain_routes(self) -> None:
        old_route = SimpleNamespace(path="/legacy", methods={"GET"}, endpoint=None)
        websocket_like = SimpleNamespace(path="/ws", endpoint=None)
        router = SimpleNamespace(routes=[old_route, websocket_like])
        self.assertEqual(_iter_http_routes(router), [old_route])

    async def test_iter_tolerates_router_without_routes(self) -> None:
        self.assertEqual(_iter_http_routes(SimpleNamespace()), [])

    async def test_unkeyed_ledger_disposal_unmounts_the_panel(self) -> None:
        app = build_app(self.root)
        ledger: DisposalLedger = app.state.disposal_ledger
        issued = await self.issue(app)
        name, value = issued
        cookie = f"{name}={value}"
        self.assertEqual((await self.get(app, cookie)).status_code, 200)
        self.assertGreater(ledger.owned("demo"), 0)
        ledger.unload("demo")
        self.assertEqual((await self.get(app, cookie)).status_code, 401)
        self.assertEqual(app.state.auth_exempt_paths,
                         {"/v1/admin/auth/status", "/v1/health"})

    async def issue(self, app: FastAPI) -> tuple[str, str]:
        async with _client(app) as client:
            response = await client.post("/v1/admin/auth/panel-session",
                                         json={"entry": "/v1/plugins/demo/web/index.html"},
                                         headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        return _issued(response)

    async def get(self, app: FastAPI, cookie: str) -> httpx.Response:
        async with _client(app) as client:
            return await client.get("/v1/plugins/demo/web/index.html", headers={"Cookie": cookie})


class RealCreateAppPanelTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end against the genuine create_app (no mirrored middleware).

    Why: the other classes rebuild the middleware to keep the suite light, which
    would hide a wiring bug between panel_auth, the real /v1/ middleware and the
    router the framework actually produces. How: a real SupervisorState and a
    real create_app in a throwaway workspace, then a real PluginRoutesFace
    (public=False) with the real static_router. Purpose: the cookie flow is
    proven against this repo's FastAPI/Starlette route table, not a mock.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from supervisor.api import create_app
        from supervisor.config_store import ConfigStore
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState

        cls._saved_token = admin_api._admin_token
        cls._saved_web_auth = admin_api._web_auth
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "data").mkdir(parents=True, exist_ok=True)
        # Set the fake credential before create_app so it neither generates nor
        # persists a token: only the throwaway workspace ever sees this string.
        admin_api._admin_token = ADMIN_TOKEN
        admin_api._web_auth = None
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            state = SupervisorState(
                workspace_root=cls.root,
                eventlog=EventLog(cls.root / "data" / "events.jsonl", "panel-auth-test"),
                policy=PolicyEngine(workspace_root=cls.root),
            )
            app = create_app(state=state, process_manager=None,
                             config_store=ConfigStore(path=cls.root / "data" / "config.yaml"))
            panel = APIRouter()
            panel.include_router(static_router(_write_panel(cls.root / "demo")), prefix="/web")
            api = APIRouter()
            api.add_api_route("/api/data", _api_payload, methods=["GET"])
            face = PluginRoutesFace()
            ledger = DisposalLedger()
            face.set_disposal_ledger(ledger)
            with ledger.collecting("demo"):
                face.register(panel, description="demo panel static")
                face.register(api, description="demo panel api")
            face.attach(app)
        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        admin_api._admin_token = cls._saved_token
        admin_api._web_auth = cls._saved_web_auth
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.auth: PanelAuth = self.app.state.panel_auth
        self.auth.grants.clear()

    async def _issue(self, *, entry: str = "/v1/plugins/demo/web/index.html") -> str:
        async with _client(self.app) as client:
            response = await client.post("/v1/admin/auth/panel-session", json={"entry": entry},
                                         headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        name, value = _issued(response)
        return f"{name}={value}"

    async def _get(self, path: str, *, method: str = "GET", cookie: str | None = None,
                   headers: dict[str, str] | None = None) -> httpx.Response:
        async with _client(self.app) as client:
            extra = dict(headers or {})
            if cookie:
                extra["Cookie"] = cookie
            return await client.request(method, path, headers=extra)

    async def test_anonymous_panel_fetch_is_refused(self) -> None:
        response = await self._get("/v1/plugins/demo/web/index.html")
        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(response.text, STATIC_TEXT)

    async def test_real_panel_cookie_loads_the_static_panel(self) -> None:
        cookie = await self._issue()
        for method in ("GET", "HEAD"):
            response = await self._get("/v1/plugins/demo/web/index.html", method=method, cookie=cookie)
            self.assertEqual(response.status_code, 200, f"{method} {response.text[:200]}")
            self.assertEqual(response.headers.get("cache-control"), "private, no-store")
        body = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)
        self.assertEqual(body.text, STATIC_TEXT)

    async def test_real_panel_cookie_cannot_reach_service_or_plugin_api(self) -> None:
        cookie = await self._issue()
        for path in ("/v1/config", "/v1/admin/auth/check", "/v1/plugins/demo/api/data"):
            response = await self._get(path, cookie=cookie)
            self.assertEqual(response.status_code, 401, path)

    async def test_real_panel_cookie_with_cross_origin_is_refused(self) -> None:
        cookie = await self._issue()
        for origin in ("http://evil.test", f"https://{HOST}", "null"):
            response = await self._get("/v1/plugins/demo/web/index.html", cookie=cookie,
                                       headers={"Origin": origin})
            self.assertEqual(response.status_code, 401, origin)
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html",
                                          cookie=cookie)).status_code, 200)

    async def test_real_revoke_kills_the_cookie(self) -> None:
        cookie = await self._issue()
        async with _client(self.app) as client:
            revoked = await client.request("DELETE", "/v1/admin/auth/panel-session",
                                           headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual((await self._get("/v1/plugins/demo/web/index.html", cookie=cookie)).status_code, 401)

    async def test_real_route_table_marks_the_static_handler(self) -> None:
        # The mounted router object is a lazy wrapper on this FastAPI version;
        # static_owner must still resolve the handler inside it.
        self.assertEqual(static_owner(_request(self.app, "/v1/plugins/demo/web/index.html")), "demo")
        self.assertIsNone(static_owner(_request(self.app, "/v1/plugins/demo/api/data")))
        # A built-in plugin panel registered through the same face resolves too.
        for owner in ("knowledge_inject", "mcp"):
            path = f"/v1/plugins/{owner}/web/index.html"
            self.assertEqual(static_owner(_request(self.app, path)), owner, path)

    async def test_builtin_panels_require_their_own_short_lived_grants(self) -> None:
        for owner in ("knowledge_inject", "mcp"):
            path = f"/v1/plugins/{owner}/web/index.html"
            self.assertEqual((await self._get(path)).status_code, 401)
            cookie = await self._issue(entry=path)
            response = await self._get(path, cookie=cookie)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "private, no-store")


# ── supervisor/api.py wiring ────────────────────────────────────────────────


class ApiWiringTests(unittest.TestCase):
    """The module must actually be mounted, not merely present."""

    def test_create_app_installs_and_consults_panel_auth(self) -> None:
        source = inspect.getsource(supervisor_api.create_app)
        self.assertIn("app.state.panel_auth = PanelAuth(app)", source)
        self.assertIn("app.state.panel_auth.permits(request)", source)
        self.assertIn("from .panel_auth import PanelAuth", inspect.getsource(supervisor_api))

    def test_panel_routes_are_registered_by_the_auth_object(self) -> None:
        app = FastAPI()
        PanelAuth(app)
        routes = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
        self.assertIn(("/v1/admin/auth/panel-session", ("POST",)), routes)
        self.assertIn(("/v1/admin/auth/panel-session", ("DELETE",)), routes)

    def test_panel_session_path_is_not_auth_exempt(self) -> None:
        source = inspect.getsource(supervisor_api.create_app)
        exempt_block = source.split("_AUTH_EXEMPT_PATHS = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("panel-session", exempt_block)
        self.assertIn("/v1/health", exempt_block)

    def test_static_router_handler_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp)
            (web / "index.html").write_text(STATIC_TEXT, encoding="utf-8")
            router = static_router(web)
        endpoint = _iter_http_routes(router)[0].endpoint
        self.assertTrue(getattr(endpoint, "_clonoth_panel_static", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
