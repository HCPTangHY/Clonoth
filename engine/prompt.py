from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime, timezone
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


def _render_variables(text: str, variables: dict[str, str]) -> str:
    """替换 {{key}} 占位符。未匹配的保留原样。"""
    def _repl(m: re.Match) -> str:
        key = m.group(1).strip()
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{(\w+)\}\}', _repl, text)


def assemble_prompt(workspace_root: Path, node: Node, *, variables: dict[str, str] | None = None) -> str:
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

    result = "\n\n".join(parts).strip() or DEFAULT_PROMPT

    # 合并变量：manifest variables 为默认值，运行时传入的覆盖
    merged: dict[str, str] = {}
    pack_vars = pack.get("variables")
    if isinstance(pack_vars, dict):
        for k, v in pack_vars.items():
            merged[str(k)] = str(v)
    if variables:
        merged.update(variables)
    # 系统保留变量
    merged.setdefault("node_id", node.id)
    merged.setdefault("node_name", node.name)
    merged.setdefault("now", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    if merged:
        result = _render_variables(result, merged)
    return result
