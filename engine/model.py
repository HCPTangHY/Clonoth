from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

from clonoth_runtime import resolve_env_ref

from .node import Node


_current_sticky_sid_var: ContextVar[str] = ContextVar("_current_sticky_sid_var", default="")
_PROVIDER_OPTION_KEYS = (
    "reasoning_field", "reasoning_effort", "max_tokens", "max_completion_tokens", "thought_summary",
)


def parse_sticky_session_config(raw: Any) -> dict[str, Any] | None:
    """Normalize the production boolean/dict configuration without truthy strings."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {"enabled": raw, "header": "X-Forwarded-For", "ip_prefix": "10"}
    if not isinstance(raw, dict):
        raise ValueError("sticky_session must be a boolean, a dict or None")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("sticky_session.enabled must be a boolean")
    return {
        "enabled": enabled,
        "header": str(raw.get("header") or "X-Forwarded-For").strip() or "X-Forwarded-For",
        "ip_prefix": str(raw.get("ip_prefix") or "10").strip().strip(".") or "10",
    }


def compute_sticky_ip(session_key: str, ip_prefix: str = "10") -> str:
    """Map a session key to the same deterministic IPv4 value used in production."""
    parts = [part.strip() for part in str(ip_prefix or "10").strip().strip(".").split(".") if part.strip()]
    octets: list[int] = []
    for part in parts[:4]:
        try:
            value = int(part)
        except ValueError:
            continue
        if 0 <= value <= 255:
            octets.append(value)
    if not octets:
        octets = [10]
    if len(octets) >= 4:
        return ".".join(str(value) for value in octets[:4])

    digest = hashlib.md5(str(session_key or "default").encode("utf-8")).digest()
    needed = 4 - len(octets)
    for index in range(needed):
        byte = digest[index]
        # Avoid .0 and .255 in the final host octet, matching production routing.
        octets.append(1 + byte % 254 if index == needed - 1 else byte)
    return ".".join(str(value) for value in octets)


class StickyHttpProxy:
    """Inject a session header while borrowing the caller's existing HTTP client."""

    def __init__(self, client: Any, header_name: str, header_value: str):
        self._client = client
        self._header_name = header_name
        self._header_value = header_value

    def _merge_headers(self, headers: Any) -> dict[str, Any]:
        merged = dict(headers) if headers else {}
        if self._header_name.lower() not in {str(key).lower() for key in merged}:
            merged[self._header_name] = self._header_value
        return merged

    async def post(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.post(url, **kwargs)

    async def get(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.get(url, **kwargs)

    async def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.request(method, url, **kwargs)

    def stream(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.stream(method, url, **kwargs)

    def build_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.build_request(method, url, **kwargs)

    async def send(self, request: Any, **kwargs: Any) -> Any:
        if hasattr(request, "headers") and self._header_name.lower() not in {
            str(key).lower() for key in request.headers
        }:
            request.headers[self._header_name] = self._header_value
        return await self._client.send(request, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_client"), name)


def wrap_http_with_sticky_session(http: Any, sticky_cfg_raw: Any, session_key: str) -> Any:
    """Apply the configured header without stacking proxies or replacing the pool."""
    if http is None or not session_key:
        return http
    config = parse_sticky_session_config(sticky_cfg_raw)
    if config is None or not config["enabled"]:
        return http
    base_client = http._client if isinstance(http, StickyHttpProxy) else http
    return StickyHttpProxy(base_client, config["header"], compute_sticky_ip(session_key, config["ip_prefix"]))


@dataclass
class ResolvedProvider:
    model: str
    # [provider-registry 2026-05-03] provider_type 是 ProviderRegistry 的 key。
    # 原因：provider 名称已插件化；做法：这里只保存字符串，不列死内置集合；目的：保持模型解析层与具体 provider 解耦。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认
    # Keep session overrides separate so provider defaults cannot override node options.
    provider_options: dict[str, Any] = field(default_factory=dict)
    provider_defaults: dict[str, Any] = field(default_factory=dict)


def resolve_provider(
    workspace_root: Path,
    node: Node,
    provider_default: str,
    session_override: dict[str, Any] | None = None,
    provider_configs: dict[str, Any] | None = None,
    default_provider_type: str = "openai",
) -> ResolvedProvider:
    """根据全局、节点和 session 配置解析模型和可选 api_key/base_url。

    provider_configs: 从 config.yaml 加载的所有 provider section（如 openai,
    deepseek, openai-responses 等），用于在 model/base_url/api_key 为空时从对应 provider 取默认值。
    default_provider_type: config.yaml 里的全局 active provider。节点未写 provider 时使用它。
    """
    model = node.model.strip() if node.model else ""

    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None

    # 从 node.provider 获取 ProviderRegistry key；节点未写 provider 时使用全局 active provider。
    # [AutoC 2026-06-16] Why: Settings → Model → ACTIVE 表示全局默认渠道，
    # but empty node.provider used to hard-code openai. How: let callers pass the
    # config.yaml provider field as default_provider_type. Purpose: selecting
    # deepseek/openai-responses as active changes default AI nodes without editing YAML.
    provider_type = (node.provider.strip() if node.provider else "") or (default_provider_type or "").strip() or "openai"

    override = session_override if isinstance(session_override, dict) else {}
    # [AutoC 2026-06-01] Why: provider selection now has a session-scoped layer
    # used by the supervisor API. How: compute the existing global→node result
    # first, then overlay non-empty session_override fields for provider/model,
    # api_key, and base_url. Purpose: the priority is session > node > global
    # without changing older resolve_provider call sites.
    override_provider = str(override.get("provider") or override.get("provider_type") or "").strip().lower()
    override_model = str(override.get("model") or "").strip()
    override_api_key = str(override.get("api_key") or "").strip()
    override_base_url = str(override.get("base_url") or "").strip()
    if override_provider:
        provider_type = override_provider
    if override_model:
        model = override_model
    if override_api_key:
        api_key = override_api_key
    if override_base_url:
        base_url = override_base_url

    raw_options = override.get("provider_options")
    override_options = deepcopy(raw_options) if isinstance(raw_options, dict) else {}
    for key in _PROVIDER_OPTION_KEYS:
        if key in override and override[key] is not None:
            override_options[key] = deepcopy(override[key])
    thinking = override.get("thinking_config")
    if isinstance(thinking, dict):
        existing = override_options.get("thinking_config")
        override_options["thinking_config"] = {
            **(existing if isinstance(existing, dict) else {}), **deepcopy(thinking),
        }
    provider_defaults: dict[str, Any] = {}

    # [2026-06-07] Why: when a node sets provider but leaves model/base_url/api_key
    # empty, the old code fell back to the global active provider's values, not the
    # target provider's own config section. How: if fields are still empty after
    # node and session override, look up provider_configs[provider_type]. Purpose:
    # provider selection becomes authoritative: provider=X inherits X's defaults
    # unless overridden by session or node fields.
    pcfg = (provider_configs or {}).get(provider_type)
    if isinstance(pcfg, dict):
        if not model and pcfg.get("model"):
            model = resolve_env_ref(str(pcfg["model"]).strip())
        if not base_url and pcfg.get("base_url"):
            base_url = resolve_env_ref(str(pcfg["base_url"]).strip())
        if not api_key and pcfg.get("api_key"):
            api_key = resolve_env_ref(str(pcfg["api_key"]).strip())
        for key in _PROVIDER_OPTION_KEYS:
            if key in pcfg and pcfg[key] is not None:
                provider_defaults[key] = deepcopy(pcfg[key])
        if isinstance(pcfg.get("thinking_config"), dict):
            provider_defaults["thinking_config"] = deepcopy(pcfg["thinking_config"])
        if "sticky_session" in pcfg:
            provider_defaults["sticky_session"] = deepcopy(pcfg["sticky_session"])
    if not model:
        model = provider_default or "gpt-4o-mini"

    return ResolvedProvider(
        model=model,
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
        provider_options=override_options,
        provider_defaults=provider_defaults,
    )
