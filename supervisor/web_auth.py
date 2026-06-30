"""Web JWT authentication for Clonoth Studio admin panel.

Stores credentials in data/web_auth.json, separate from the machine-to-machine
admin token. Web UI credential leaks do not compromise the internal API token.

Dependencies: bcrypt, PyJWT (both optional — falls back to token-only mode).
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

_HAS_DEPS = True
try:
    import bcrypt
    import jwt as pyjwt
except ImportError:
    _HAS_DEPS = False


class WebAuthManager:
    """Manages data/web_auth.json: users, JWT secret, setup state."""

    def __init__(self, data_dir: Path):
        self._path = data_dir / "web_auth.json"
        self._data: dict[str, Any] = {}
        self.available = _HAS_DEPS
        if _HAS_DEPS:
            self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    @property
    def setup_completed(self) -> bool:
        return bool(self._data.get("setup_completed", False))

    @property
    def jwt_secret(self) -> str:
        return str(self._data.get("jwt_secret", ""))

    @property
    def jwt_expire_hours(self) -> int:
        # [AutoC 2026-06-30] Hardcode 720h (30 days). The per-file value written
        # by old setup() defaults was 24h which is too short, and there is no UI
        # to configure this, so reading from data is pointless.
        return 720

    def setup(
        self, username: str, password: str, jwt_expire_hours: int = 720,
    ) -> dict[str, Any]:
        """Initial setup: create first user and generate JWT secret.

        Returns {"ok": True, "token": "<jwt>", "expires_in": N} on success.
        Only works once — subsequent calls return an error.
        """
        if self.setup_completed:
            return {"ok": False, "error": "already configured"}
        if not username.strip() or not password:
            return {"ok": False, "error": "username and password required"}

        pw_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")

        self._data = {
            "jwt_secret": secrets.token_urlsafe(32),
            "jwt_expire_hours": jwt_expire_hours,
            "setup_completed": True,
            "users": [{"username": username.strip(), "password_hash": pw_hash}],
        }
        self._save()

        token = self.generate_jwt(username.strip())
        return {"ok": True, "token": token, "expires_in": jwt_expire_hours * 3600}

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate and return JWT.

        Returns {"ok": True, "token": "<jwt>", "expires_in": N} on success.
        """
        if not self.setup_completed:
            return {"ok": False, "error": "setup not completed"}
        for user in self._data.get("users", []):
            if user.get("username") == username:
                try:
                    if bcrypt.checkpw(
                        password.encode("utf-8"),
                        user["password_hash"].encode("utf-8"),
                    ):
                        token = self.generate_jwt(username)
                        return {
                            "ok": True,
                            "token": token,
                            "expires_in": self.jwt_expire_hours * 3600,
                        }
                except Exception:
                    pass
                return {"ok": False, "error": "invalid credentials"}
        return {"ok": False, "error": "invalid credentials"}

    def generate_jwt(self, username: str) -> str:
        """Sign a JWT for the given username."""
        return pyjwt.encode(
            {
                "sub": username,
                "iss": "clonoth",
                "iat": int(time.time()),
                "exp": int(time.time()) + self.jwt_expire_hours * 3600,
            },
            self.jwt_secret,
            algorithm="HS256",
        )

    def verify_jwt(self, token: str) -> dict[str, Any] | None:
        """Decode and verify a JWT. Returns payload dict or None on failure."""
        if not self.jwt_secret:
            return None
        try:
            payload = pyjwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
                options={"require": ["sub", "exp"]},
            )
            if payload.get("iss") != "clonoth":
                return None
            return payload
        except Exception:
            return None

    def change_password(self, username: str, new_password: str) -> bool:
        """Change password for an existing user."""
        for user in self._data.get("users", []):
            if user.get("username") == username:
                user["password_hash"] = bcrypt.hashpw(
                    new_password.encode("utf-8"), bcrypt.gensalt(),
                ).decode("utf-8")
                self._save()
                return True
        return False
