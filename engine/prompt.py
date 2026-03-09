from __future__ import annotations

from pathlib import Path
from typing import Any

from clonoth_runtime import load_text_file, load_yaml_dict

from .node import Node

DEFAULT_PROMPT = "你是一个 AI 节点。请根据指令完成处理。\n"


def _load_pack_manifest(workspace_root: Path, pack_id: str) -> dict[str, Any] | None:
    path = workspace_root / "config" / "prompt_packs" / pack_id / "manifest.yaml"
    data = load_yaml_dict(path)
    if not isinstance(data, dict):
        return None
    if str(data.get("kind") or "prompt_pack").strip() != "prompt_pack":
        return None
    return data


def assemble_prompt(workspace_root: Path, node: Node) -> str:
    """根据节点定义的 prompt 引用，组装完整 system prompt。"""
    if not node.prompt.pack or not node.prompt.assembly:
        return DEFAULT_PROMPT

    pack = _load_pack_manifest(workspace_root, node.prompt.pack)
    if pack is None:
        return DEFAULT_PROMPT

    root_rel = str(pack.get("fragments_root") or "fragments").strip()
    assemblies = pack.get("assemblies")
    if not isinstance(assemblies, dict):
        return DEFAULT_PROMPT

    items = assemblies.get(node.prompt.assembly)
    if not isinstance(items, list) or not items:
        return DEFAULT_PROMPT

    pack_dir = workspace_root / "config" / "prompt_packs" / node.prompt.pack
    parts: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            return DEFAULT_PROMPT
        frag = (pack_dir / root_rel / item.strip()).resolve()
        try:
            frag.relative_to(pack_dir.resolve())
        except ValueError:
            return DEFAULT_PROMPT
        text = load_text_file(frag)
        if not text:
            return DEFAULT_PROMPT
        parts.append(text)

    return "\n\n".join(parts).strip() or DEFAULT_PROMPT
