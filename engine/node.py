from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict


@dataclass
class ToolAccess:
    mode: str = "none"  # "none" | "all" | "allowlist"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class SkillAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class PromptRef:
    pack: str = ""
    assembly: str = ""


@dataclass
class Node:
    id: str
    type: str  # "ai" | "tool"
    name: str = ""
    description: str = ""
    model_route: str = ""
    prompt: PromptRef = field(default_factory=PromptRef)
    tool_access: ToolAccess = field(default_factory=ToolAccess)
    skill_access: SkillAccess = field(default_factory=SkillAccess)
    output_mode: str = "draft"  # "draft" | "reply"


def load_node(workspace_root: Path, node_id: str) -> Node | None:
    nid = (node_id or "").strip()
    if not nid:
        return None
    data = load_yaml_dict(workspace_root / "config" / "nodes" / f"{nid}.yaml")
    if not isinstance(data, dict) or str(data.get("kind") or "node").strip() != "node":
        return None
    node_type = str(data.get("type") or "ai").strip().lower()
    if node_type not in {"ai", "tool"}:
        return None

    pr = data.get("prompt") if isinstance(data.get("prompt"), dict) else {}
    prompt = PromptRef(
        pack=str(pr.get("pack") or "").strip(),
        assembly=str(pr.get("assembly") or "").strip(),
    )

    ta_raw = data.get("tool_access")
    if isinstance(ta_raw, str):
        m = ta_raw.strip().lower()
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none")
    elif isinstance(ta_raw, dict):
        m = str(ta_raw.get("mode") or "none").strip().lower()
        allow = [
            str(x).strip()
            for x in (ta_raw.get("allow") or [])
            if isinstance(x, str) and x.strip()
        ]
        deny = [
            str(x).strip()
            for x in (ta_raw.get("deny") or [])
            if isinstance(x, str) and x.strip()
        ]
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none", allow=allow, deny=deny)
    else:
        ta = ToolAccess()

    sa_raw = data.get("skills")
    if isinstance(sa_raw, str):
        sm = sa_raw.strip().lower()
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all")
    elif isinstance(sa_raw, dict):
        sm = str(sa_raw.get("mode") or "all").strip().lower()
        sa_allow = [str(x).strip() for x in (sa_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all", allow=sa_allow)
    else:
        sa = SkillAccess()

    return Node(
        id=str(data.get("id") or nid).strip(),
        type=node_type,
        name=str(data.get("name") or nid).strip(),
        description=str(data.get("description") or "").strip(),
        model_route=str(data.get("model_route") or "").strip(),
        prompt=prompt,
        tool_access=ta,
        skill_access=sa,
        output_mode=str(data.get("output_mode") or "draft").strip().lower(),
    )
