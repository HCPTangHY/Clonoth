from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clonoth_runtime import resolve_env_ref

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    # [provider-registry 2026-05-03] provider_type 是 ProviderRegistry 的 key。
    # 原因：provider 名称已插件化；做法：这里只保存字符串，不列死内置集合；目的：保持模型解析层与具体 provider 解耦。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认


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
    if not model:
        model = provider_default or "gpt-4o-mini"

    return ResolvedProvider(
        model=model,
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
    )
