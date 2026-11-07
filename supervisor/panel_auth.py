"""Revocable, short-lived access to registered plugin static resources.

This is not a second API login: normal API authentication never accepts these
cookies. Grants are bounded and process-local; a Supervisor restart clears them.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.routing import Match

from engine.faces.routes import _iter_http_routes
from .admin_api import get_web_auth, verify_admin_token

TTL = 120
MAX_GRANTS = 1024
STATIC_PATH = re.compile(r"/v1/plugins/([A-Za-z0-9_-]{1,64})/web/.*", re.ASCII)


def cookie_name(owner: str, scheme: str) -> str:
    return f"clonoth_panel_{scheme}_{owner}"


def cookie_path(owner: str) -> str:
    return f"/v1/plugins/{owner}/web/"


def static_owner(request: Request) -> str | None:
    """Check the actual registered handler, not just a path prefix."""
    path = request.url.path
    match = STATIC_PATH.fullmatch(path)
    if request.method not in {"GET", "HEAD"} or not match:
        return None
    if "\\" in path or "%" in path or any(p in {".", ".."} for p in path.split("/")):
        return None
    for route in _iter_http_routes(request.app.router):
        matched, _ = route.matches(dict(request.scope))
        if matched == Match.FULL:
            return match[1] if getattr(getattr(route, "endpoint", None), "_clonoth_panel_static", False) else None
    return None


def cross_origin(request: Request) -> bool:
    """Any explicit cross-origin signal disqualifies the request.

    [AutoC 2026-09-12] A same-origin iframe document load sends neither Origin
    nor a cross-site Sec-Fetch-Site, so plain panel GET/HEAD stays valid. A
    caller that sets an Origin at all (foreign host, sibling same-site host,
    other port/scheme, or the literal "null" of a sandboxed context) is refused
    even when its panel Cookie is otherwise valid: the cookie is not a
    same-origin proof. Why this is shared: permits() is the quieter door than
    _bearer(), so both must answer an identical same-origin question or the
    cookie would be a weaker credential than the token that minted it.
    """
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        return True
    return request.headers.get("sec-fetch-site") == "cross-site"


def _bearer(request: Request) -> tuple[str, int]:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(401, "Bearer authentication required")
    # A query token or panel cookie cannot bootstrap/renew/revoke grants.
    verify_admin_token(Request({**request.scope, "query_string": b""}))
    if cross_origin(request):
        raise HTTPException(403, "Cross-origin panel authentication refused")
    ttl = TTL
    wa = get_web_auth()
    payload = wa.verify_jwt(token) if wa and wa.available and wa.setup_completed else None
    if payload is not None:
        ttl = min(ttl, int(payload["exp"] - time.time()))
    if ttl <= 0:
        raise HTTPException(401, "Authentication expired")
    return hashlib.sha256(token.encode()).hexdigest(), ttl


class PanelEntry(BaseModel):
    entry: str = Field(min_length=1, max_length=2048)


@dataclass
class Grant:
    owner: str
    credential: str
    scheme: str
    expires: float


class PanelAuth:
    def __init__(self, app: FastAPI):
        self.grants: dict[str, Grant] = {}
        app.add_api_route("/v1/admin/auth/panel-session", self.issue, methods=["POST"])
        app.add_api_route("/v1/admin/auth/panel-session", self.revoke, methods=["DELETE"])

    def permits(self, request: Request) -> bool:
        owner = static_owner(request)
        if owner is None or cross_origin(request):
            return False
        scheme = request.url.scheme
        grant = self.grants.get(request.cookies.get(cookie_name(owner, scheme), ""))
        return bool(grant and grant.owner == owner and grant.scheme == scheme
                    and grant.expires > time.monotonic())

    async def issue(self, request: Request, body: PanelEntry) -> JSONResponse:
        credential, ttl = _bearer(request)
        try:
            url = urlsplit(body.entry)
        except ValueError:
            raise HTTPException(400, "Invalid plugin entry URL") from None
        # No arbitrary origins, redirects or caller-selected Cookie Path.
        if url.scheme or url.netloc or url.fragment or not body.entry.startswith("/v1/plugins/"):
            raise HTTPException(400, "Expected a same-origin plugin entry")
        scope = {**request.scope, "method": "GET", "path": unquote(url.path), "query_string": b""}
        owner = static_owner(Request(scope))
        if owner is None:
            raise HTTPException(400, "Entry is not a registered static panel")
        now = time.monotonic()
        scheme = request.url.scheme
        self.grants = {key: grant for key, grant in self.grants.items() if grant.expires > now}
        # Concurrent same-plugin panels reuse their grant, avoiding out-of-order
        # Set-Cookie responses that would otherwise invalidate each other.
        key = next((key for key, g in self.grants.items()
                    if g.owner == owner and g.credential == credential and g.scheme == scheme), None)
        if key is None:
            if len(self.grants) >= MAX_GRANTS:
                raise HTTPException(503, "Panel session capacity reached")
            key = secrets.token_urlsafe(32)
        self.grants[key] = Grant(owner, credential, scheme, now + ttl)
        response = JSONResponse({"ok": True, "expires_in": ttl}, headers={"Cache-Control": "no-store"})
        response.set_cookie(cookie_name(owner, scheme), key, max_age=ttl, path=cookie_path(owner),
                            httponly=True, samesite="strict", secure=scheme == "https")
        return response

    async def revoke(self, request: Request) -> JSONResponse:
        credential, _ = _bearer(request)
        scopes = {(g.owner, g.scheme) for g in self.grants.values() if g.credential == credential}
        self.grants = {key: g for key, g in self.grants.items() if g.credential != credential}
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        for owner, scheme in sorted(scopes):
            response.delete_cookie(cookie_name(owner, scheme), path=cookie_path(owner), httponly=True,
                                   samesite="strict", secure=scheme == "https")
        return response
