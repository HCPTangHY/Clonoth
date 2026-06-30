from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

from .types import (
    AppConfigPublic,
    AppConfigSecret,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
)


def _resolve_env_value(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return os.getenv(s[2:-1].strip(), "").strip()
    if s.startswith("$ENV{") and s.endswith("}") and len(s) > 6:
        return os.getenv(s[5:-1].strip(), "").strip()
    return s


def _redact_api_key(api_key: str) -> tuple[bool, str]:
    k = (api_key or "").strip()
    if not k:
        return False, ""
    # Environment reference (recommended)
    if k.startswith("${") and k.endswith("}") and len(k) > 3:
        var = k[2:-1].strip()
        return True, f"<env:{var}>"
    if len(k) <= 4:
        return True, "****"
    return True, "****" + k[-4:]


class ConfigStore:
    """YAML-backed config store.

    - Canonical file: data/config.yaml
    - Contains provider selection + OpenAI settings.

    注意：api_key 属于敏感信息，本仓库会将 data/config.yaml 加入 .gitignore。
    """

    def __init__(self, *, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._config: AppConfigSecret = AppConfigSecret()
        self._extra_keys: dict[str, Any] = {}  # preserve unknown top-level keys

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    def _load_yaml_dict(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None

        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return None

        data = yaml.safe_load(text)
        if data is None:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_yaml(self, cfg: AppConfigSecret) -> None:
        # [2026-05-24] Preserve unknown top-level keys (e.g. fallbacks) that
        # plugins may have added to config.yaml but are not part of the
        # AppConfigSecret Pydantic model. Without this, reload() would strip
        # any key not declared in the model.
        data = cfg.model_dump(mode="json")
        if self._extra_keys:
            data.update(self._extra_keys)
        text = yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
        )
        self.path.write_text(text, encoding="utf-8")

    def reload(self) -> AppConfigSecret:
        with self._lock:
            data = self._load_yaml_dict()
            if data is None:
                self._config = AppConfigSecret()
                self._save_yaml(self._config)
                return self._config.model_copy(deep=True)

            # Capture top-level keys not in the Pydantic model
            _known_keys = set(AppConfigSecret.model_fields.keys())
            self._extra_keys = {k: v for k, v in data.items() if k not in _known_keys}
            try:
                self._config = AppConfigSecret.model_validate(data)
            except Exception:
                # 备份坏配置，写回默认值
                try:
                    bak = self.path.with_suffix(self.path.suffix + ".bak")
                    if self.path.exists() and not bak.exists():
                        self.path.replace(bak)
                except Exception:
                    pass

                self._config = AppConfigSecret()
                self._save_yaml(self._config)
                return self._config.model_copy(deep=True)

            # 写回一次，确保字段齐全且格式统一
            self._save_yaml(self._config)
            return self._config.model_copy(deep=True)

    def get_secret(self) -> AppConfigSecret:
        with self._lock:
            return self._config.model_copy(deep=True)

    def get_public(self) -> AppConfigPublic:
        with self._lock:
            cfg = self._config.model_copy(deep=True)
            cfg.openai.base_url = _resolve_env_value(cfg.openai.base_url)
            cfg.openai.api_key = _resolve_env_value(cfg.openai.api_key)
            cfg.openai.model = _resolve_env_value(cfg.openai.model)
            present, redacted = _redact_api_key(cfg.openai.api_key)
            openai_pub = OpenAIConfigPublic(
                base_url=cfg.openai.base_url,
                model=cfg.openai.model,
                api_key_present=present,
                api_key=redacted,
            )
            data = self._load_raw()
            pm: dict[str, str] = {}
            for k, v in data.items():
                if k in self._META_KEYS:
                    continue
                if self._is_provider_block(v):
                    m = str(_resolve_env_value(v.get("model", ""))).strip()
                    if m:
                        pm[k] = m
            return AppConfigPublic(version=cfg.version, provider=cfg.provider, openai=openai_pub, provider_models=pm)

    def get_openai_secret(self) -> OpenAIConfigSecret:
        with self._lock:
            cfg = self._config.openai.model_copy(deep=True)
            cfg.base_url = _resolve_env_value(cfg.base_url)
            cfg.api_key = _resolve_env_value(cfg.api_key)
            cfg.model = _resolve_env_value(cfg.model)
            return cfg

    def get_openai_public(self) -> OpenAIConfigPublic:
        with self._lock:
            cfg = self._config.model_copy(deep=True)
            cfg.openai.base_url = _resolve_env_value(cfg.openai.base_url)
            cfg.openai.api_key = _resolve_env_value(cfg.openai.api_key)
            cfg.openai.model = _resolve_env_value(cfg.openai.model)
            present, redacted = _redact_api_key(cfg.openai.api_key)
            return OpenAIConfigPublic(
                base_url=cfg.openai.base_url,
                model=cfg.openai.model,
                api_key_present=present,
                api_key=redacted,
            )

    def update_openai(self, update: OpenAIConfigUpdateIn) -> AppConfigPublic:
        with self._lock:
            cfg = self._config

            if update.base_url is not None:
                cfg.openai.base_url = update.base_url.strip()
            if update.api_key is not None:
                cfg.openai.api_key = update.api_key.strip()
            if update.model is not None:
                cfg.openai.model = update.model.strip()

            # 当前阶段只接入 openai；provider 字段保持 openai
            cfg.provider = "openai"

            self._save_yaml(cfg)
            self._config = cfg

            present, redacted = _redact_api_key(cfg.openai.api_key)
            openai_pub = OpenAIConfigPublic(
                base_url=cfg.openai.base_url,
                model=cfg.openai.model,
                api_key_present=present,
                api_key=redacted,
            )
            return AppConfigPublic(version=cfg.version, provider=cfg.provider, openai=openai_pub)

    # ================================================================
    #  Multi-provider CRUD (operates on raw YAML dict)
    # ================================================================

    _META_KEYS = frozenset({"version", "provider", "fallbacks"})

    def _load_raw(self) -> dict[str, Any]:
        """Load raw YAML dict from disk."""
        return self._load_yaml_dict() or {"version": 1, "provider": "openai"}

    def _save_raw(self, data: dict[str, Any]) -> None:
        """Write raw dict to YAML file."""
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        self.path.write_text(text, encoding="utf-8")

    def _is_provider_block(self, val: Any) -> bool:
        return isinstance(val, dict) and any(k in val for k in ("base_url", "api_key", "model"))

    def get_providers_public(self) -> dict[str, Any]:
        """Return all providers with redacted api_keys, plus active_provider and fallbacks."""
        with self._lock:
            data = self._load_raw()
            active = data.get("provider", "openai")
            fallbacks = data.get("fallbacks", []) or []

            providers: dict[str, dict[str, Any]] = {}
            for key, val in data.items():
                if key in self._META_KEYS:
                    continue
                if self._is_provider_block(val):
                    raw_key = _resolve_env_value(val.get("api_key", ""))
                    present, redacted = _redact_api_key(raw_key)
                    providers[key] = {
                        "base_url": _resolve_env_value(val.get("base_url", "")),
                        "model": _resolve_env_value(val.get("model", "")),
                        "api_key_present": present,
                        "api_key_redacted": redacted,
                    }

            return {
                "active_provider": active,
                "providers": providers,
                "fallbacks": fallbacks,
            }

    def upsert_provider(self, name: str, *, base_url: str | None = None,
                        api_key: str | None = None, model: str | None = None) -> dict[str, Any]:
        """Create or update a provider entry."""
        with self._lock:
            data = self._load_raw()
            if name not in data or not self._is_provider_block(data.get(name)):
                # [2026-06-07] Why: _is_provider_block requires at least one of
                # base_url/api_key/model to be present. Old code wrote empty dict
                # {}, which passes isinstance(dict) but fails _is_provider_block.
                # How: check _is_provider_block instead of isinstance, and always
                # reinitialize entries that lack provider fields. Purpose: stale
                # empty entries from old code are repaired on next upsert.
                data[name] = {"base_url": "", "model": "", "api_key": ""}
            if base_url is not None:
                data[name]["base_url"] = base_url.strip()
            if api_key is not None:
                data[name]["api_key"] = api_key.strip()
            if model is not None:
                data[name]["model"] = model.strip()
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def set_active_provider(self, name: str) -> dict[str, Any]:
        """Switch the active provider."""
        with self._lock:
            data = self._load_raw()
            if name not in data or not self._is_provider_block(data.get(name)):
                raise ValueError(f"Provider '{name}' not found in config")
            data["provider"] = name
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def update_fallbacks(self, fallbacks: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the fallback chain."""
        with self._lock:
            data = self._load_raw()
            data["fallbacks"] = fallbacks
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()

    def delete_provider(self, name: str) -> dict[str, Any]:
        """Delete a provider entry."""
        with self._lock:
            data = self._load_raw()
            if name not in data:
                return self.get_providers_public()
            if data.get("provider") == name:
                raise ValueError(f"Cannot delete active provider '{name}'; switch to another first")
            del data[name]
            fb = data.get("fallbacks", []) or []
            data["fallbacks"] = [f for f in fb if f.get("provider") != name]
            self._save_raw(data)
            self.reload()
            return self.get_providers_public()
