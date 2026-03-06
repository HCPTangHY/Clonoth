from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 5 :]
    try:
        meta = yaml.safe_load(head) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _short_text(s: str, max_chars: int = 240) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def load_skill_catalog(workspace_root: Path) -> list[dict[str, Any]]:
    skills_dir = workspace_root / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = _parse_skill_frontmatter(text)
            name = str(meta.get("name") or skill_md.parent.name).strip() or skill_md.parent.name
            description = str(meta.get("description") or "").strip()
            enabled = bool(meta.get("enabled", True))
            items.append(
                {
                    "name": name,
                    "description": description,
                    "enabled": enabled,
                    "path": skill_md.relative_to(workspace_root).as_posix(),
                }
            )
        except Exception:
            continue
    return items


_WORD_RE = re.compile(r"[A-Za-z0-9_-]+")


def _infer_explicit_skill_mentions(text: str, skills: list[dict[str, Any]]) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []

    hits: list[str] = []
    lower = raw.lower()
    words = set(w.lower() for w in _WORD_RE.findall(raw))

    for item in skills:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        nlow = name.lower()
        if f"${nlow}" in lower or nlow in words:
            hits.append(name)
    return hits


def format_skill_discovery_message(workspace_root: Path, *, task_text: str = "") -> str:
    skills = [s for s in load_skill_catalog(workspace_root) if bool(s.get("enabled", True))]
    if not skills:
        return ""

    lines: list[str] = [
        "[CLONOTH_SKILLS_INDEX v1]",
        "仅加载 skill 元数据（name / description / path）。",
        "按 progressive disclosure 使用：只有当任务明显匹配 skill 的 description，或用户明确提到 skill 名时，才去读取对应 SKILL.md 全文。",
        "不要一次性读取所有 SKILL.md。",
        "",
    ]

    explicit = set(_infer_explicit_skill_mentions(task_text, skills))
    if explicit:
        lines.append("用户显式提到的 skill 候选：" + ", ".join(sorted(explicit)))
        lines.append("")

    for item in skills:
        lines.append(f"- name: {item['name']}")
        lines.append(f"  description: {_short_text(str(item.get('description') or ''))}")
        lines.append(f"  path: {item['path']}")

    lines.append("[/CLONOTH_SKILLS_INDEX]")
    return "\n".join(lines)
