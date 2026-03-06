from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_str


@dataclass
class PromptRef:
    pack: str = ""
    assembly: str = ""
    prefill: str = ""


@dataclass
class AgentProfile:
    id: str
    name: str
    runtime: str
    mode: str
    description: str = ""
    model_route: str = ""
    prompt: PromptRef = field(default_factory=PromptRef)
    raw: dict[str, Any] = field(default_factory=dict)


def _load_yaml_dict(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _load_text_file(path: Path, max_chars: int = 200_000) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = text.strip()
        if not text:
            return ""
        if len(text) > max_chars:
            return text[:max_chars] + "\n...<truncated>"
        return text
    except Exception:
        return ""


def load_agent_profile(*, workspace_root: Path, profile_id: str) -> AgentProfile | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None

    path = workspace_root / "config" / "agents" / f"{pid}.yaml"
    data = _load_yaml_dict(path)
    if data is None:
        return None

    if str(data.get("kind") or "agent_profile").strip() != "agent_profile":
        return None

    runtime = str(data.get("runtime") or "").strip()
    mode = str(data.get("mode") or "").strip()
    if not runtime or not mode:
        return None

    prompt_raw = data.get("prompt") if isinstance(data.get("prompt"), dict) else {}
    prompt = PromptRef(
        pack=str(prompt_raw.get("pack") or "").strip(),
        assembly=str(prompt_raw.get("assembly") or "").strip(),
        prefill=str(prompt_raw.get("prefill") or "").strip(),
    )

    return AgentProfile(
        id=str(data.get("id") or pid).strip() or pid,
        name=str(data.get("name") or pid).strip() or pid,
        runtime=runtime,
        mode=mode,
        description=str(data.get("description") or "").strip(),
        model_route=str(data.get("model_route") or "").strip(),
        prompt=prompt,
        raw=data,
    )


def load_prompt_pack_manifest(*, workspace_root: Path, pack_id: str) -> dict[str, Any] | None:
    pid = (pack_id or "").strip()
    if not pid:
        return None
    path = workspace_root / "config" / "prompt_packs" / pid / "manifest.yaml"
    data = _load_yaml_dict(path)
    if data is None:
        return None
    if str(data.get("kind") or "prompt_pack").strip() != "prompt_pack":
        return None
    return data


def assemble_prompt(*, workspace_root: Path, pack_id: str, assembly_id: str) -> str:
    pack = load_prompt_pack_manifest(workspace_root=workspace_root, pack_id=pack_id)
    if pack is None:
        return ""

    root_rel = str(pack.get("fragments_root") or "fragments").strip() or "fragments"
    assemblies = pack.get("assemblies")
    if not isinstance(assemblies, dict):
        return ""

    raw_items = assemblies.get(assembly_id)
    if not isinstance(raw_items, list) or not raw_items:
        return ""

    pack_root = workspace_root / "config" / "prompt_packs" / pack_id
    parts: list[str] = []
    for item in raw_items:
        if not isinstance(item, str) or not item.strip():
            return ""
        frag_path = (pack_root / root_rel / item.strip()).resolve()
        try:
            frag_path.relative_to(pack_root.resolve())
        except ValueError:
            return ""
        text = _load_text_file(frag_path)
        if not text:
            return ""
        parts.append(text)

    return "\n\n".join(parts).strip()


def assemble_prompt_for_profile(*, workspace_root: Path, profile_id: str) -> str:
    profile = load_agent_profile(workspace_root=workspace_root, profile_id=profile_id)
    if profile is None:
        return ""
    if not profile.prompt.pack or not profile.prompt.assembly:
        return ""
    prompt = assemble_prompt(
        workspace_root=workspace_root,
        pack_id=profile.prompt.pack,
        assembly_id=profile.prompt.assembly,
    )
    if not prompt:
        return ""
    prefill = (profile.prompt.prefill or "").strip()
    if not prefill:
        return prompt
    return prompt + "\n\n# Prefill\n" + prefill


def _load_model_routing(*, workspace_root: Path) -> dict[str, Any] | None:
    return _load_yaml_dict(workspace_root / "config" / "model_routing.yaml")


def resolve_openai_model_for_profile(
    *,
    workspace_root: Path,
    runtime_cfg: dict[str, Any],
    profile_id: str,
    provider_default_model: str,
    legacy_runtime_key: str = "",
) -> str:
    provider_default = (provider_default_model or "").strip() or "gpt-4o-mini"

    profile = load_agent_profile(workspace_root=workspace_root, profile_id=profile_id)
    if profile is not None and profile.model_route:
        routing = _load_model_routing(workspace_root=workspace_root)
        routes = routing.get("routes") if isinstance(routing, dict) else None
        route = routes.get(profile.model_route) if isinstance(routes, dict) else None
        candidates = route.get("candidates") if isinstance(route, dict) else None
        if isinstance(candidates, list):
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                provider = str(cand.get("provider") or "").strip().lower()
                if provider and provider != "openai":
                    continue

                direct_model = str(cand.get("model") or "").strip()
                if direct_model:
                    return direct_model

                model_runtime_key = str(cand.get("model_runtime_key") or "").strip()
                if model_runtime_key:
                    model_override = get_str(runtime_cfg, model_runtime_key, "").strip()
                    if model_override:
                        return model_override

                fallback_to_provider_cfg = bool(cand.get("fallback_to_provider_config_model", False))
                if fallback_to_provider_cfg and provider_default:
                    return provider_default

                fallback_model = str(cand.get("fallback_model") or "").strip()
                if fallback_model:
                    return fallback_model

    if legacy_runtime_key:
        legacy = get_str(runtime_cfg, legacy_runtime_key, "").strip()
        if legacy:
            return legacy

    return provider_default
