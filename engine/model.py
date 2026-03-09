from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clonoth_runtime import get_str, load_yaml_dict, resolve_env_ref

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认


def resolve_provider(workspace_root: Path, runtime_cfg: dict[str, Any], node: Node, provider_default: str) -> ResolvedProvider:
    """根据节点的 model_route 解析模型名称和可选的独立 api_key/base_url。"""
    fallback = ResolvedProvider(model=provider_default or "gpt-4o-mini")

    if not node.model_route:
        return fallback

    routing = load_yaml_dict(workspace_root / "config" / "model_routing.yaml")
    if not isinstance(routing, dict):
        return fallback

    routes = routing.get("routes")
    if not isinstance(routes, dict):
        return fallback

    route = routes.get(node.model_route)
    if not isinstance(route, dict):
        return fallback

    candidates = route.get("candidates")
    if not isinstance(candidates, list):
        return fallback

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        prov = str(cand.get("provider") or "").strip().lower()
        if prov and prov != "openai":
            continue

        # 解析模型名
        model = ""
        direct = str(cand.get("model") or "").strip()
        if direct:
            model = direct
        else:
            key = str(cand.get("model_runtime_key") or "").strip()
            if key:
                val = get_str(runtime_cfg, key, "").strip()
                if val:
                    model = val
        if not model:
            if cand.get("fallback_to_provider_config_model") and provider_default:
                model = provider_default
            else:
                fb = str(cand.get("fallback_model") or "").strip()
                model = fb or provider_default or "gpt-4o-mini"

        # 解析可选的独立 api_key
        api_key = _resolve_secret(cand, "api_key", runtime_cfg)

        # 解析可选的独立 base_url
        base_url = _resolve_secret(cand, "base_url", runtime_cfg)

        return ResolvedProvider(model=model, api_key=api_key, base_url=base_url)

    return fallback


def _resolve_secret(cand: dict[str, Any], field: str, runtime_cfg: dict[str, Any]) -> str | None:
    """从 candidate 配置中解析一个可选的秘密值。

    支持三种方式（优先级从高到低）：
    1. {field}_env: 环境变量名
    2. {field}_runtime_key: runtime.yaml 中的配置键路径
    3. {field}: 直接值（支持 $ENV{VAR} 语法）
    """
    # 方式 1: 环境变量名
    env_key = str(cand.get(f"{field}_env") or "").strip()
    if env_key:
        val = os.environ.get(env_key, "").strip()
        if val:
            return val

    # 方式 2: runtime 配置键
    rt_key = str(cand.get(f"{field}_runtime_key") or "").strip()
    if rt_key:
        val = get_str(runtime_cfg, rt_key, "").strip()
        if val:
            return resolve_env_ref(val)

    # 方式 3: 直接值
    direct = str(cand.get(field) or "").strip()
    if direct:
        return resolve_env_ref(direct)

    return None
