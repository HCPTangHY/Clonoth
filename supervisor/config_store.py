from __future__ import annotations

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
        self._lock = threading.Lock()
        self._config: AppConfigSecret = AppConfigSecret()

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
        data = cfg.model_dump(mode="json")
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
            cfg = self._config
            present, redacted = _redact_api_key(cfg.openai.api_key)
            openai_pub = OpenAIConfigPublic(
                base_url=cfg.openai.base_url,
                model=cfg.openai.model,
                api_key_present=present,
                api_key=redacted,
            )
            return AppConfigPublic(version=cfg.version, provider=cfg.provider, openai=openai_pub)

    def get_openai_secret(self) -> OpenAIConfigSecret:
        with self._lock:
            return self._config.openai.model_copy(deep=True)

    def get_openai_public(self) -> OpenAIConfigPublic:
        with self._lock:
            cfg = self._config
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
