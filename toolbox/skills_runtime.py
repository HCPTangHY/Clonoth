from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
#  Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns ``(meta_dict, body_text)``.  When no valid frontmatter is
    found, returns ``({}, original_text)``.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 5:]
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


# ---------------------------------------------------------------------------
#  Keyword compilation & matching
# ---------------------------------------------------------------------------

def _compile_keyword(kw: str) -> re.Pattern[str] | str:
    """Compile a keyword entry.

    If *kw* matches ``/pattern/flags``, compile as a regular expression.
    Otherwise return the lowercased string for substring matching.
    """
    kw = (kw or "").strip()
    if not kw:
        return ""
    if kw.startswith("/"):
        last_slash = kw.rfind("/")
        if last_slash > 0:
            pattern = kw[1:last_slash]
            flags_str = kw[last_slash + 1:]
            flags = 0
            if "i" in flags_str:
                flags |= re.IGNORECASE
            if "s" in flags_str:
                flags |= re.DOTALL
            if "m" in flags_str:
                flags |= re.MULTILINE
            try:
                return re.compile(pattern, flags)
            except re.error:
                pass
    return kw.lower()


def _match_keywords(compiled: list[re.Pattern[str] | str], text: str) -> bool:
    """Return ``True`` if any compiled keyword matches *text*."""
    if not compiled or not text:
        return False
    text_lower = text.lower()
    for kw in compiled:
        if not kw:
            continue
        if isinstance(kw, re.Pattern):
            if kw.search(text):
                return True
        elif kw in text_lower:
            return True
    return False


# ---------------------------------------------------------------------------
#  Catalog loading
# ---------------------------------------------------------------------------


class _SkillCache:
    """Per-workspace skill catalog cache keyed on file mtimes."""

    _lock = threading.Lock()
    _entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # ws_path -> (ts, items)
    _mtimes: dict[str, dict[str, float]] = {}  # ws_path -> {file_posix: mtime}
    _TTL = 2.0  # seconds before rechecking mtimes

    @classmethod
    def get(cls, workspace_root: Path) -> list[dict[str, Any]] | None:
        key = str(workspace_root)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None  # stale, caller should reload
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]]) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)


def load_skill_catalog(workspace_root: Path, *, _use_cache: bool = True) -> list[dict[str, Any]]:
    """Scan ``skills/*/SKILL.md`` and return metadata + body for each skill.

    Results are cached for up to 2 seconds to avoid repeated filesystem
    reads within the same request cycle.
    """
    if _use_cache:
        cached = _SkillCache.get(workspace_root)
        if cached is not None:
            return cached

    skills_dir = workspace_root / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta, body = parse_skill_frontmatter(text)
            name = str(meta.get("name") or skill_md.parent.name).strip() or skill_md.parent.name
            description = str(meta.get("description") or "").strip()
            enabled = bool(meta.get("enabled", True))

            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"

            raw_kw = meta.get("keywords")
            keywords: list[str] = []
            if isinstance(raw_kw, list):
                keywords = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            elif isinstance(raw_kw, str) and raw_kw.strip():
                keywords = [raw_kw.strip()]

            order = 0
            if isinstance(meta.get("order"), (int, float)):
                order = int(meta["order"])
            priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                priority = int(meta["priority"])
            scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                scan_depth = max(0, int(meta["scan_depth"]))

            items.append({
                "name": name,
                "description": description,
                "enabled": enabled,
                "path": skill_md.relative_to(workspace_root).as_posix(),
                "strategy": strategy,
                "keywords": keywords,
                "compiled_keywords": [_compile_keyword(k) for k in keywords],
                "order": order,
                "priority": priority,
                "scan_depth": scan_depth,
                "body": body.strip(),
            })
        except Exception:
            continue
    _SkillCache.put(workspace_root, items)
    return items


# ---------------------------------------------------------------------------
#  Scan text building
# ---------------------------------------------------------------------------

def _build_scan_text(
    instruction_text: str,
    history: list[dict[str, Any]] | None,
    scan_depth: int,
) -> str:
    """Build text to scan for keyword matching.

    *instruction_text* is always included.  When *scan_depth* > 0,
    the content of the last *scan_depth* **rounds** (each round is a
    user message followed by an optional assistant reply) from
    *history* is appended.  A round boundary is defined as the start
    of a user message that is preceded by a non-user message (or is
    the first message).
    """
    parts: list[str] = [instruction_text or ""]
    if history and scan_depth > 0:
        # Walk backwards to find round boundaries.
        # A "round" starts at a user message that is either the first
        # message or follows a non-user message.
        round_starts: list[int] = []
        for i in range(len(history) - 1, -1, -1):
            role = history[i].get("role", "")
            if role != "user":
                continue
            # This user message starts a new round if:
            #   - it is the first message, OR
            #   - the previous message is not a user message
            if i == 0 or history[i - 1].get("role", "") != "user":
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break

        if round_starts:
            start_idx = round_starts[-1]  # earliest round start
            for msg in history[start_idx:]:
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def build_skill_messages(
    workspace_root: Path,
    *,
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    max_budget_chars: int = 0,
) -> list[dict[str, str]]:
    """Build system messages for skill injection.

    Returns a list of ``{"role": "system", "content": "..."}`` dicts
    arranged for prompt-cache friendliness:

    * ``[0]`` — constant skills block (stable across turns)
    * ``[1]`` — dynamic skills + discovery index (may vary per turn)

    Either or both may be absent when there is nothing to inject.
    """
    catalog = load_skill_catalog(workspace_root)

    # --- filter by enabled + node-level access --------------------------
    skills = [s for s in catalog if s.get("enabled", True)]
    if skill_mode == "none":
        return []
    if skill_mode == "allowlist" and skill_allow is not None:
        allow_set = set(skill_allow)
        skills = [s for s in skills if s.get("name") in allow_set]
    if not skills:
        return []

    # --- classify -------------------------------------------------------
    constant_skills: list[dict[str, Any]] = []
    keyword_skills: list[dict[str, Any]] = []
    index_only_skills: list[dict[str, Any]] = []

    for s in skills:
        if s["strategy"] == "constant":
            constant_skills.append(s)
        elif s["keywords"]:
            keyword_skills.append(s)
        else:
            index_only_skills.append(s)

    # --- keyword matching for dynamic skills ----------------------------
    dynamic_skills: list[dict[str, Any]] = []
    for s in keyword_skills:
        scan_text = _build_scan_text(instruction_text, history, s["scan_depth"])
        if _match_keywords(s["compiled_keywords"], scan_text):
            dynamic_skills.append(s)
        else:
            index_only_skills.append(s)

    # --- sort by order --------------------------------------------------
    constant_skills.sort(key=lambda x: x["order"])
    dynamic_skills.sort(key=lambda x: x["order"])
    index_only_skills.sort(key=lambda x: x["order"])

    # --- budget enforcement ---------------------------------------------
    if max_budget_chars > 0:
        all_injectable: list[tuple[str, dict[str, Any]]] = []
        for s in constant_skills:
            all_injectable.append(("constant", s))
        for s in dynamic_skills:
            all_injectable.append(("dynamic", s))

        # Higher priority → keep first
        all_injectable.sort(key=lambda x: x[1]["priority"], reverse=True)

        kept_constant: list[dict[str, Any]] = []
        kept_dynamic: list[dict[str, Any]] = []
        used = 0
        for kind, s in all_injectable:
            body_len = len(s.get("body") or "")
            if used + body_len <= max_budget_chars:
                used += body_len
                if kind == "constant":
                    kept_constant.append(s)
                else:
                    kept_dynamic.append(s)
            else:
                index_only_skills.append(s)

        kept_constant.sort(key=lambda x: x["order"])
        kept_dynamic.sort(key=lambda x: x["order"])
        constant_skills = kept_constant
        dynamic_skills = kept_dynamic

    # --- build messages -------------------------------------------------
    messages: list[dict[str, str]] = []

    # constant block
    if constant_skills:
        parts: list[str] = ["[SKILLS:CONSTANT]"]
        for s in constant_skills:
            parts.append(f"\n## Skill: {s['name']}\n")
            parts.append(s.get("body") or "")
        parts.append("\n[/SKILLS:CONSTANT]")
        messages.append({"role": "system", "content": "\n".join(parts)})

    # dynamic + index block
    dynamic_parts: list[str] = []
    if dynamic_skills:
        dynamic_parts.append("[SKILLS:ACTIVE]")
        for s in dynamic_skills:
            dynamic_parts.append(f"\n## Skill: {s['name']}\n")
            dynamic_parts.append(s.get("body") or "")
        dynamic_parts.append("\n[/SKILLS:ACTIVE]")

    if index_only_skills:
        if dynamic_parts:
            dynamic_parts.append("")
        dynamic_parts.append("[SKILLS:INDEX]")
        dynamic_parts.append(
            "以下 skill 未被激活。如果当前任务需要，可通过 read_file 读取对应 path 的全文。"
        )
        for s in index_only_skills:
            dynamic_parts.append(f"- name: {s['name']}")
            dynamic_parts.append(f"  description: {_short_text(s.get('description') or '')}")
            dynamic_parts.append(f"  path: {s['path']}")
        dynamic_parts.append("[/SKILLS:INDEX]")

    if dynamic_parts:
        messages.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return messages
