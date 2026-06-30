from __future__ import annotations

import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_int
from engine.node import Node
from toolbox._common import request_guard, resolve_under_allowed_roots
from toolbox.builtins import SKILL_NAME_RE
from toolbox.context import ToolContext

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: skill and memory injection now share one before_prompt_build handler.
# How: declare one metadata entry with the higher previous priority. Purpose:
# preserve hook discovery while removing the old two-step injector chain.
PLUGIN_META = {
    "handler_class": "KnowledgeInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 50,
    # Why: the six skill and memory CRUD tools now belong to the knowledge
    # plugin rather than toolbox.registry.py. How: the concrete declarations are
    # attached after their functions are defined below. Purpose: keep one source
    # of truth for knowledge behavior and tool registration metadata.
    "tools": [],
}



# ---------------------------------------------------------------------------
#  Keyword matching
# ---------------------------------------------------------------------------

def compile_keyword(kw: str) -> re.Pattern[str] | str:
    """Compile a skill or memory keyword entry."""
    # Why: skill and memory keyword matching now lives inside the knowledge
    # plugin instead of the old standalone matcher file. How: keep the exact legacy
    # /pattern/flags parsing and lower-case substring fallback. Purpose: preserve
    # activation behavior while removing the standalone matcher module.
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


def match_keywords(compiled: list[re.Pattern[str] | str], text: str) -> bool:
    """Return True when any compiled keyword matches text."""
    # Why: both skill and memory entries use the same activation semantics. How:
    # run regex entries against original text and literal entries against lowered
    # text, matching the old helper exactly. Purpose: prevent behavior drift while
    # the duplicated matcher files are removed.
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


def build_scan_text(
    instruction_text: str,
    history: list[dict[str, Any]] | None,
    scan_depth: int,
) -> str:
    """Build the text scanned for keyword activation."""
    # Why: skill and memory scan-depth handling must stay identical. How: always
    # include the current instruction and append the last scan_depth conversation
    # rounds using the legacy user-message boundary rule. Purpose: keep keyword
    # activation scope unchanged after the files are merged.
    parts: list[str] = [instruction_text or ""]
    if history and scan_depth > 0:
        # Why: the old implementation defined a round as a user message that
        # starts after a non-user message. How: walk backward until enough round
        # starts are found. Purpose: preserve keyword activation scope exactly.
        round_starts: list[int] = []
        for i in range(len(history) - 1, -1, -1):
            role = history[i].get("role", "")
            if role != "user":
                continue
            if i == 0 or history[i - 1].get("role", "") != "user":
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break

        if round_starts:
            start_idx = round_starts[-1]
            for msg in history[start_idx:]:
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Skill frontmatter, catalog loading, and legacy skill builder
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md content."""
    # Why: skill CRUD and skill injection now share this module after deleting
    # the old separate skill runtime file. How: keep the same frontmatter delimiter
    # and YAML fallback behavior. Purpose: preserve SKILL.md compatibility during
    # the move.
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


class _SkillCache:
    """Per-workspace skill catalog cache keyed on file mtimes."""

    # Why: skill catalog scans can happen several times in one prompt build. How:
    # keep the old short time-based cache in the merged plugin. Purpose: avoid
    # extra filesystem reads without changing cache lifetime.
    _lock = threading.Lock()
    _entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _mtimes: dict[str, dict[str, float]] = {}
    _TTL = 2.0

    @classmethod
    def get(cls, workspace_root: Path) -> list[dict[str, Any]] | None:
        key = str(workspace_root)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]]) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)


def load_skill_catalog(workspace_root: Path, *, _use_cache: bool = True) -> list[dict[str, Any]]:
    """Scan ``skills/*/SKILL.md`` and return metadata + body for each skill."""
    # Why: skill scanning is now owned by the knowledge plugin. How: move the
    # former skill catalog loader here byte-for-byte except for local matcher
    # references. Purpose: keep skill injection output stable while removing the
    # old runtime module.
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

            raw_node_ids = meta.get("node_ids")
            node_ids: list[str] = []
            if isinstance(raw_node_ids, list):
                node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
            elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                node_ids = [raw_node_ids.strip()]

            items.append({
                "name": name,
                "description": description,
                "enabled": enabled,
                "path": skill_md.relative_to(workspace_root).as_posix(),
                "strategy": strategy,
                "keywords": keywords,
                "compiled_keywords": [compile_keyword(k) for k in keywords],
                "order": order,
                "priority": priority,
                "scan_depth": scan_depth,
                "body": body.strip(),
                "node_ids": node_ids,
            })
        except Exception:
            continue
    _SkillCache.put(workspace_root, items)
    return items


def build_skill_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    max_budget_chars: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for skill injection through the unified pipeline."""
    # Why: legacy callers need a compatible skill message builder after the old
    # runtime module is removed. How: delegate to the unified knowledge pipeline
    # with memory disabled. Purpose: preserve the public behavior while keeping
    # one injection implementation.
    skill_static, skill_dynamic, _memory_static, _memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_skill_entries(load_skill_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode="none",
        memory_allow=None,
        skill_max_budget_chars=max_budget_chars,
        memory_max_budget_chars=0,
        knowledge_max_budget_chars=0,
    )
    return skill_static, skill_dynamic


# ---------------------------------------------------------------------------
#  Memory catalog loading, hit tracking, and legacy memory builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  In-memory hit timestamp cache (replaces per-call YAML read/write)
# ---------------------------------------------------------------------------
# [2026-05-24] Why: _update_last_hit_bg was yaml.safe_load-ing 660KB+ files
# on every LLM call, eating 45% CPU. How: track hits in a memory dict,
# flush to a lightweight JSON sidecar every 10 min or at shutdown.
# Purpose: zero-IO hot path, dream reads the JSON when it runs.

import atexit as _atexit
import json as _json

_hit_cache: dict[str, str] = {}          # {entry_id: iso_timestamp}
_hit_cache_dirty: bool = False
_hit_cache_lock = threading.Lock()
_hit_cache_last_flush: float = 0.0
_HIT_CACHE_FLUSH_INTERVAL = 600          # 10 minutes


def _hit_cache_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "memory" / ".hit_cache.json"


def _load_hit_cache(workspace_root: Path) -> None:
    """Load hit cache from disk on first access."""
    global _hit_cache, _hit_cache_last_flush
    p = _hit_cache_path(workspace_root)
    if p.exists():
        try:
            _hit_cache = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _hit_cache = {}
    _hit_cache_last_flush = time.monotonic()


def _flush_hit_cache(workspace_root: Path | None = None) -> None:
    """Write dirty hit cache to disk."""
    global _hit_cache_dirty, _hit_cache_last_flush
    with _hit_cache_lock:
        if not _hit_cache_dirty:
            return
        if workspace_root is None:
            return
        try:
            p = _hit_cache_path(workspace_root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(_hit_cache), encoding="utf-8")
            _hit_cache_dirty = False
            _hit_cache_last_flush = time.monotonic()
        except Exception:
            pass


def _record_hits(workspace_root: Path, entries: list[dict[str, Any]]) -> None:
    """Record keyword hits in memory. No YAML, no threads."""
    global _hit_cache_dirty
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    with _hit_cache_lock:
        if not _hit_cache and _hit_cache_last_flush == 0.0:
            _load_hit_cache(workspace_root)
        for e in entries:
            eid = e.get("id", "")
            if eid:
                _hit_cache[eid] = now_iso
                _hit_cache_dirty = True
        # Periodic flush
        if _hit_cache_dirty and (time.monotonic() - _hit_cache_last_flush) > _HIT_CACHE_FLUSH_INTERVAL:
            threading.Thread(target=_flush_hit_cache, args=(workspace_root,), daemon=True).start()


class _MemoryCache:
    """Per-workspace memory catalog cache keyed on time."""

    # Why: memory catalog scans are now in this plugin, but prompt builds still
    # need the same short cache. How: preserve the old cache API including
    # invalidate(). Purpose: keep memory extraction and CRUD invalidation working.
    # [2026-05-28] cache key 从 str(workspace_root) 改为 (str(workspace_root), memory_book)。
    # 为什么：记忆 namespace 隔离后，不同 memory_book 的 catalog 互不相同。
    # 怎么改：所有方法增加 keyword-only memory_book 参数，cache key 改为元组。
    # 目的：不同 namespace 各自独立缓存，互不污染。
    _lock = threading.Lock()
    _entries: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
    _TTL = 2.0

    @classmethod
    def get(cls, workspace_root: Path, *, memory_book: str = "") -> list[dict[str, Any]] | None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]], *, memory_book: str = "") -> None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)

    @classmethod
    def invalidate(cls, workspace_root: Path, *, memory_book: str = "") -> None:
        key = (str(workspace_root), memory_book)
        with cls._lock:
            cls._entries.pop(key, None)


def memory_dir(workspace_root: Path, memory_book: str = "") -> Path:
    """Return the memory storage directory path.

    [2026-05-28] 增加 memory_book 参数支持 namespace 隔离。
    为什么：持久化子节点的记忆应隔离到独立子目录，避免互相污染。
    怎么改：memory_book 非空时返回 data/memory/{memory_book}/，否则返回 data/memory/。
    目的：save_memory 和 load_memory_catalog 共用此路径计算。
    """
    base = workspace_root / "data" / "memory"
    if memory_book:
        return base / memory_book
    return base


def load_memory_catalog(
    workspace_root: Path,
    *,
    memory_book: str = "",
    _use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Scan memory yaml files and return parsed entries.

    [2026-05-28] 增加 memory_book 参数支持 namespace 隔离。
    为什么：持久化子节点的记忆应存储在独立子目录，与主节点互不干扰。
    怎么改：memory_book 非空时扫 data/memory/{memory_book}/*.yaml，
    否则扫 data/memory/*.yaml（现有行为，不递归）。
    缓存 key 用 (workspace_root, memory_book) 元组区分。
    """
    if _use_cache:
        cached = _MemoryCache.get(workspace_root, memory_book=memory_book)
        if cached is not None:
            return cached

    mem_dir = memory_dir(workspace_root, memory_book)
    if not mem_dir.exists() or not mem_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            text = yaml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                continue
            book = str(data.get("book") or yaml_path.stem).strip()
            entries = data.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                if not entry.get("enabled", True):
                    continue

                content = str(entry.get("content") or "").strip()
                if not content:
                    continue

                raw_kw = entry.get("keywords")
                keywords: list[str] = []
                if isinstance(raw_kw, list):
                    keywords = [
                        str(k).strip()
                        for k in raw_kw
                        if isinstance(k, str) and str(k).strip()
                    ]
                elif isinstance(raw_kw, str) and raw_kw.strip():
                    keywords = [raw_kw.strip()]

                constant = bool(entry.get("constant", False))
                priority = 0
                if isinstance(entry.get("priority"), (int, float)):
                    priority = int(entry["priority"])
                scan_depth = 0
                if isinstance(entry.get("scan_depth"), (int, float)):
                    scan_depth = max(0, int(entry["scan_depth"]))

                # Why: old memory books accepted node_ids as either a list or a
                # comma-separated string. How: keep the same coercion. Purpose:
                # node-scoped memories continue to load without migration.
                raw_node_ids = entry.get("node_ids")
                node_ids: list[str] = []
                if isinstance(raw_node_ids, list):
                    node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
                elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                    node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

                source = str(entry.get("source", "")).strip()
                created_at = str(entry.get("created_at", "")).strip()
                last_hit_at = str(entry.get("last_hit_at", "")).strip()

                items.append({
                    "book": book,
                    "id": eid,
                    "keywords": keywords,
                    "compiled_keywords": [compile_keyword(k) for k in keywords],
                    "content": content,
                    "constant": constant,
                    "priority": priority,
                    "scan_depth": scan_depth,
                    "node_ids": node_ids,
                    "source": source,
                    "created_at": created_at,
                    "last_hit_at": last_hit_at,
                })
        except Exception:
            continue

    _MemoryCache.put(workspace_root, items, memory_book=memory_book)
    return items


# [2026-05-24] _update_last_hit_bg REMOVED.
# Was: full yaml.safe_load + safe_dump of 660KB+ files per LLM call.
# Replaced by: _record_hits() → in-memory dict → periodic JSON flush.
    _MemoryCache.invalidate(workspace_root)


def build_memory_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    max_budget_chars: int = 0,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for memory injection through the unified pipeline."""
    # Why: legacy callers still need build_memory_messages after the old memory
    # module is deleted. How: delegate to the unified knowledge pipeline with skills
    # disabled. Purpose: keep public memory message behavior compatible.
    _skill_static, _skill_dynamic, memory_static, memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_memory_entries(load_memory_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode="none",
        skill_allow=None,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
        skill_max_budget_chars=0,
        memory_max_budget_chars=max_budget_chars,
        knowledge_max_budget_chars=0,
    )
    return memory_static, memory_dynamic

def _short_text(s: str, max_chars: int = 240) -> str:
    """Return the same short skill description used by the legacy index."""
    # Why: skill INDEX rendering moved into this module. How: keep the old
    # truncation helper byte-compatible. Purpose: avoid changing INDEX text.
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def _as_string_list(value: Any) -> list[str]:
    """Normalize loose catalog values into a list of non-empty strings."""
    # Why: catalogs are loaded from YAML/frontmatter and may contain either a
    # scalar or a list. How: mirror the existing loader coercion rules. Purpose:
    # let the unified pipeline accept both raw and already-normalized catalogs.
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_int(value: Any, default: int = 0) -> int:
    """Return integer metadata while preserving legacy numeric-only coercion."""
    # Why: priority, order, and scan_depth were accepted only when YAML parsed
    # them as numbers. How: keep that rule here instead of parsing arbitrary
    # strings. Purpose: prevent a subtle behavior change during normalization.
    if isinstance(value, (int, float)):
        return int(value)
    return default


def normalize_skill_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map skill catalog entries to the unified knowledge-entry dictionary."""
    # Why: skill and memory now share filtering, matching, budgeting, and
    # rendering orchestration. How: convert skill-specific fields into the
    # common Entry shape while preserving skill-only render metadata. Purpose:
    # make global skill/memory budget selection possible without merging storage.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        keywords = _as_string_list(raw.get("keywords"))
        raw_strategy = str(raw.get("strategy") or "normal").strip().lower()
        if raw_strategy == "constant":
            strategy = "constant"
        elif keywords:
            strategy = "keyword"
        else:
            strategy = "index"

        name = str(raw.get("name") or raw.get("id") or "").strip()
        if not name:
            continue

        entries.append({
            "id": name,
            "kind": "skill",
            "content": str(raw.get("body") or raw.get("content") or ""),
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": _as_int(raw.get("order"), 0),
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), 0)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": str(raw.get("description") or ""),
            "path": str(raw.get("path") or ""),
            "book": "",
            "source": "",
            "created_at": "",
            "last_hit_at": "",
        })
    return entries


def normalize_memory_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map memory catalog entries to the unified knowledge-entry dictionary."""
    # Why: memory injection should use the same pipeline as skills, but memory
    # has no discovery INDEX. How: map constant memories to constant entries,
    # keyword memories to keyword entries, and skip keywordless non-constant
    # memories. Purpose: preserve existing memory visibility exactly.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        memory_id = str(raw.get("id") or "").strip()
        content = str(raw.get("content") or "").strip()
        if not memory_id or not content:
            continue

        keywords = _as_string_list(raw.get("keywords"))
        if bool(raw.get("constant", False)):
            strategy = "constant"
        elif keywords:
            strategy = "keyword"
        else:
            continue

        entries.append({
            "id": memory_id,
            "kind": "memory",
            "content": content,
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": 0,
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), 0)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": "",
            "path": "",
            "book": str(raw.get("book") or ""),
            "source": str(raw.get("source") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "last_hit_at": str(raw.get("last_hit_at") or ""),
        })
    return entries


def _filter_entries(
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply node visibility and node-level skill/memory access rules."""
    # Why: both old builders performed the same node_id and allowlist filtering
    # in separate files. How: branch only on kind-specific access semantics.
    # Purpose: give the unified budget stage one already-authorized entry list.
    filtered: list[dict[str, Any]] = []
    skill_allow_set = set(skill_allow or []) if skill_allow is not None else None
    memory_allow_set = set(memory_allow or []) if memory_allow else None

    for entry in entries:
        node_ids = entry.get("node_ids") or []
        if node_id and node_ids and node_id not in node_ids:
            continue

        if entry.get("kind") == "skill":
            if skill_mode == "none":
                continue
            # Why: the legacy skill builder only skipped all skills for an empty
            # allowlist when allow was an explicit list, not None. How: preserve
            # that exact None-versus-empty distinction. Purpose: keep wrappers
            # and node config behavior backward compatible.
            if skill_mode == "allowlist" and skill_allow_set is not None and entry.get("id") not in skill_allow_set:
                continue
        elif entry.get("kind") == "memory":
            if memory_mode == "none":
                continue
            # Why: the legacy memory builder filtered allowlist only when the
            # allow collection was truthy. How: keep that condition here.
            # Purpose: knowledge.max_budget_chars=0 remains behavior-compatible.
            if memory_mode == "allowlist" and memory_allow_set is not None and entry.get("book") not in memory_allow_set:
                continue
        else:
            continue
        filtered.append(entry)
    return filtered


def _new_buckets() -> dict[str, list[dict[str, Any]]]:
    """Create render buckets shared by budget and rendering stages."""
    # Why: prompt layout still needs separate skill/static, skill/dynamic,
    # memory/static, and memory/dynamic messages. How: keep explicit buckets
    # after unified matching. Purpose: unify selection without changing labels.
    return {
        "skill_constant": [],
        "skill_active": [],
        "skill_index": [],
        "memory_constant": [],
        "memory_active": [],
    }


def _classify_and_match_entries(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify entries and run keyword activation for keyword entries."""
    # Why: the old builders both used constant/keyword/index phases. How: run
    # the same phases over unified entries and route results back to render
    # buckets by kind. Purpose: make later budgeting global while preserving
    # memory's lack of an INDEX block.
    buckets = _new_buckets()
    matched_memories: list[dict[str, Any]] = []

    for entry in entries:
        kind = entry.get("kind")
        strategy = entry.get("strategy")
        if strategy == "constant":
            if kind == "skill":
                buckets["skill_constant"].append(entry)
            elif kind == "memory":
                buckets["memory_constant"].append(entry)
            continue

        if strategy == "keyword":
            scan_text = build_scan_text(instruction_text, history, int(entry.get("scan_depth") or 0))
            if match_keywords(list(entry.get("compiled_keywords") or []), scan_text):
                if kind == "skill":
                    buckets["skill_active"].append(entry)
                elif kind == "memory":
                    buckets["memory_active"].append(entry)
                    matched_memories.append(entry)
            elif kind == "skill":
                buckets["skill_index"].append(entry)
            continue

        if strategy == "index" and kind == "skill":
            buckets["skill_index"].append(entry)

    if matched_memories:
        # [2026-05-24] Record hits in memory dict (zero IO).
        # Old _update_last_hit_bg was yaml.safe_load-ing 660KB per call.
        _record_hits(workspace_root, matched_memories)

    return buckets


def _sort_render_buckets(buckets: dict[str, list[dict[str, Any]]]) -> None:
    """Sort buckets exactly as the legacy renderers did before budgeting."""
    # Why: prompt cache stability depends on deterministic ordering. How: sort
    # skills by (order, id) and memories by id, matching the old builders.
    # Purpose: keep render order stable after moving logic into one module.
    for key in ("skill_constant", "skill_active", "skill_index"):
        buckets[key].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    for key in ("memory_constant", "memory_active"):
        buckets[key].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_skill_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply the legacy skill-only budget to skill injectable buckets."""
    # Why: knowledge.max_budget_chars=0 must use the old independent skill
    # budget. How: select constant and active skills by priority and append
    # rejected skills to INDEX. Purpose: preserve both injected content and
    # discovery behavior for existing configs.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    for entry in buckets["skill_constant"]:
        all_injectable.append(("skill_constant", entry))
    for entry in buckets["skill_active"]:
        all_injectable.append(("skill_active", entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept_constant: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            if origin == "skill_constant":
                kept_constant.append(entry)
            else:
                kept_active.append(entry)
        else:
            buckets["skill_index"].append(entry)

    buckets["skill_constant"] = kept_constant
    buckets["skill_active"] = kept_active
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))


def _apply_memory_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply the legacy memory-only budget to memory injectable buckets."""
    # Why: the default path must keep memory's old independent budget. How:
    # select constant and active memories by priority and drop rejected memories
    # because memories never had an INDEX. Purpose: preserve existing memory
    # prompt output when no global knowledge budget is configured.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    for entry in buckets["memory_constant"]:
        all_injectable.append(("memory_constant", entry))
    for entry in buckets["memory_active"]:
        all_injectable.append(("memory_active", entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept_constant: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            if origin == "memory_constant":
                kept_constant.append(entry)
            else:
                kept_active.append(entry)

    buckets["memory_constant"] = kept_constant
    buckets["memory_active"] = kept_active
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_global_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply one priority-sorted budget pool across skills and memories."""
    # Why: Phase 3 requires high-priority memories and skills to compete in one
    # pool. How: rank all injectable entries by priority while remembering their
    # render bucket, then restore kept entries to their original labels. Purpose:
    # change budget selection without changing final tag names or prompt layout.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    # Why: equal-priority ties need deterministic behavior. How: start from the
    # prompt render order before the stable priority sort. Purpose: avoid random
    # output while still making priority the only cross-kind ranking key.
    for bucket_name in ("skill_constant", "memory_constant", "skill_active", "memory_active"):
        for entry in buckets[bucket_name]:
            all_injectable.append((bucket_name, entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept: dict[str, list[dict[str, Any]]] = {
        "skill_constant": [],
        "skill_active": [],
        "memory_constant": [],
        "memory_active": [],
    }
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            kept[origin].append(entry)
        elif origin.startswith("skill_"):
            # Why: over-budget skill bodies used to remain discoverable through
            # SKILLS:INDEX. How: append rejected injectable skills to the index
            # bucket. Purpose: global budgeting does not hide skill metadata.
            buckets["skill_index"].append(entry)

    for bucket_name, entries in kept.items():
        buckets[bucket_name] = entries
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _render_skill_messages(
    constant_skills: list[dict[str, Any]],
    dynamic_skills: list[dict[str, Any]],
    index_only_skills: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render skill buckets using the legacy SKILLS tags."""
    # Why: callers and tests depend on exact SKILLS tag names and section
    # structure. How: copy the old renderer's joins and header strings while
    # reading unified entry fields. Purpose: make the refactor output-compatible.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_skills:
        parts: list[str] = ["[SKILLS:CONSTANT]"]
        for entry in constant_skills:
            parts.append(f"\n## Skill: {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/SKILLS:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_skills:
        dynamic_parts.append("[SKILLS:ACTIVE]")
        for entry in dynamic_skills:
            dynamic_parts.append(f"\n## Skill: {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/SKILLS:ACTIVE]")

    if index_only_skills:
        if dynamic_parts:
            dynamic_parts.append("")
        dynamic_parts.append("[SKILLS:INDEX]")
        dynamic_parts.append(
            "以下 skill 未被激活。如果当前任务需要，可通过 read_file 读取对应 path 的全文。"
        )
        for entry in index_only_skills:
            dynamic_parts.append(f"- name: {entry['id']}")
            dynamic_parts.append(f"  description: {_short_text(str(entry.get('description') or ''))}")
            dynamic_parts.append(f"  path: {entry.get('path') or ''}")
        dynamic_parts.append("[/SKILLS:INDEX]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def _render_memory_messages(
    constant_entries: list[dict[str, Any]],
    dynamic_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render memory buckets using the legacy MEMORY tags."""
    # Why: memories did not have an INDEX block and used different section
    # headers from skills. How: keep the old MEMORY renderer's exact tag names
    # and joins. Purpose: preserve prompt text for existing memory users.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_entries:
        parts: list[str] = ["[MEMORY:CONSTANT]"]
        for entry in constant_entries:
            parts.append(f"\n## {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/MEMORY:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_entries:
        dynamic_parts.append("[MEMORY:ACTIVE]")
        for entry in dynamic_entries:
            dynamic_parts.append(f"\n## {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/MEMORY:ACTIVE]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def build_knowledge_messages(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
    skill_max_budget_chars: int = 0,
    memory_max_budget_chars: int = 0,
    knowledge_max_budget_chars: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Build skill and memory messages from already-normalized entries."""
    # Why: build_knowledge_context and legacy wrappers need the same pipeline.
    # How: filter, classify, match, budget, and render unified entries in one
    # helper. Purpose: avoid reintroducing divergent skill and memory behavior.
    filtered_entries = _filter_entries(
        entries,
        node_id=node_id,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
    )
    if not filtered_entries:
        return [], [], [], []

    buckets = _classify_and_match_entries(
        workspace_root,
        filtered_entries,
        instruction_text=instruction_text,
        history=history,
    )
    _sort_render_buckets(buckets)

    if knowledge_max_budget_chars > 0:
        _apply_global_budget(buckets, knowledge_max_budget_chars)
    else:
        _apply_skill_budget(buckets, skill_max_budget_chars)
        _apply_memory_budget(buckets, memory_max_budget_chars)

    skill_static, skill_dynamic = _render_skill_messages(
        buckets["skill_constant"],
        buckets["skill_active"],
        buckets["skill_index"],
    )
    memory_static, memory_dynamic = _render_memory_messages(
        buckets["memory_constant"],
        buckets["memory_active"],
    )
    return skill_static, skill_dynamic, memory_static, memory_dynamic


def build_knowledge_context(
    workspace_root: Path,
    node: Node,
    instruction_text: str,
    history: list[dict],
    runtime_cfg: dict,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return ``(skill_static, skill_dynamic, memory_static, memory_dynamic)``.

    Why: inference and preempt paths must not know how skill and memory builders
    are wired. How: load both catalogs, normalize them into one Entry shape, and
    run one filtering/matching/budget/render pipeline. Purpose: support global
    skill/memory budgeting while keeping the public context boundary stable.
    """
    safe_runtime_cfg = runtime_cfg or {}

    # Why: the knowledge plugin now owns both storage loaders and the unified
    # injection pipeline. How: load skill and memory catalogs locally, normalize
    # them into one Entry shape, and render through build_knowledge_messages.
    # Purpose: keep prompt injection behavior stable while deleting the old files.
    entries = normalize_skill_entries(load_skill_catalog(workspace_root))
    # [2026-05-28] namespace 隔离：从 node.extra 获取 memory_book，加载对应子目录的记忆。
    # 为什么：持久化子节点的记忆存储在 data/memory/{memory_book}/ 下。
    # 怎么改：传 memory_book 给 load_memory_catalog，让它扫描正确的目录。
    # 目的：节点只看到自己 namespace 下的记忆条目。
    _mb = str(node.extra.get("memory_book") or "").strip()
    entries.extend(normalize_memory_entries(load_memory_catalog(workspace_root, memory_book=_mb)))

    return build_knowledge_messages(
        workspace_root,
        entries,
        node_id=node.id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
        memory_mode=node.memory_access.mode,
        memory_allow=node.memory_access.allow,
        skill_max_budget_chars=get_int(safe_runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
        memory_max_budget_chars=get_int(safe_runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
        knowledge_max_budget_chars=get_int(safe_runtime_cfg, "knowledge.max_budget_chars", 0, min_value=0),
    )



# ---------------------------------------------------------------------------
#  Skill CRUD tools
# ---------------------------------------------------------------------------


def _tool_ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: knowledge tools return paths, ids, and catalog lists
    # that should live under data while data.result remains readable. How: use one
    # helper for skill and memory success payloads. Purpose: keep plugin-owned tools
    # aligned with the ok/data/error response schema.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _tool_err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: knowledge-tool validation and guard failures should
    # expose data.result. How: wrap error text and mirror optional fields under data
    # and top level. Purpose: keep failures readable and migration-compatible.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


async def create_or_update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a skill under skills/<name>/SKILL.md."""
    # Why: skill management tools now live beside skill loading and injection.
    # How: move the old skill-tool implementation here and keep the
    # async tool signature unchanged. Purpose: let PLUGIN_META register the tool
    # without hard-coding it in toolbox.registry.py.
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    content = args.get("content")
    enabled = bool(args.get("enabled", True))

    strategy = str(args.get("strategy", "")).strip().lower() or None
    raw_keywords = args.get("keywords")
    keywords: list[str] | None = None
    if isinstance(raw_keywords, list):
        keywords = [str(k).strip() for k in raw_keywords if isinstance(k, str) and str(k).strip()]

    order: int | None = None
    if args.get("order") is not None:
        try:
            order = int(args["order"])
        except (TypeError, ValueError):
            pass
    priority: int | None = None
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass
    scan_depth: int | None = None
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    if not name:
        return _tool_err("empty skill name")
    if not SKILL_NAME_RE.fullmatch(name):
        return _tool_err("invalid skill name: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed")

    path = f"skills/{name}/SKILL.md"
    if not isinstance(content, str) or not content.strip():
        meta: dict[str, Any] = {
            "name": name,
            "description": description,
            "enabled": enabled,
        }
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        body = description or f"Skill {name}"
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"
    else:
        meta, body = parse_skill_frontmatter(content)
        if not isinstance(meta, dict):
            meta = {}
        meta["name"] = name
        if description:
            meta["description"] = description
        elif not isinstance(meta.get("description"), str):
            meta["description"] = ""
        meta["enabled"] = enabled
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"

    # Why: write_file already centralizes policy approval and path checks. How:
    # import it lazily from toolbox.builtins after moving this function out of that
    # package. Purpose: preserve the guarded write behavior without a module cycle.
    from toolbox.builtins.write_file import write_file

    res = await write_file({"path": path, "content": content}, ctx)
    if not res.get("ok"):
        return res
    return _tool_ok(f"Skill written: {path}", path=path, name=name, enabled=enabled)


async def list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List local skills under skills/*/SKILL.md."""
    # Why: the listing tool was moved with the skill parser. How: keep the same
    # filesystem scan and metadata coercion. Purpose: preserve tool output shape.
    skills_dir = ctx.workspace_root / "skills"
    if not skills_dir.exists():
        return _tool_ok("0 skills", skills=[])

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            rel = skill_md.relative_to(ctx.workspace_root).as_posix()
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = parse_skill_frontmatter(text)
            if not isinstance(meta, dict):
                meta = {}
            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"
            raw_kw = meta.get("keywords")
            kw_list: list[str] = []
            if isinstance(raw_kw, list):
                kw_list = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            item_order = 0
            if isinstance(meta.get("order"), (int, float)):
                item_order = int(meta["order"])
            item_priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                item_priority = int(meta["priority"])
            item_scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                item_scan_depth = max(0, int(meta["scan_depth"]))
            items.append(
                {
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "strategy": strategy,
                    "keywords": kw_list,
                    "order": item_order,
                    "priority": item_priority,
                    "scan_depth": item_scan_depth,
                    "path": rel,
                }
            )
        except Exception as e:
            items.append({"name": skill_md.parent.name, "path": skill_md.relative_to(ctx.workspace_root).as_posix(), "error": str(e)})

    return _tool_ok(f"{len(items)} skills", skills=items)


async def delete_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a skill directory under skills/<name>/."""
    # Why: delete_skill is now plugin-owned like the other knowledge tools. How:
    # keep the old guard and shutil.rmtree behavior. Purpose: preserve approval
    # and deletion semantics while removing the old skill-tool file.
    name = str(args.get("name", "")).strip()
    if not name:
        return _tool_err("empty skill name")
    if not SKILL_NAME_RE.fullmatch(name):
        return _tool_err("invalid skill name")

    skill_dir = resolve_under_allowed_roots(ctx.workspace_root, f"skills/{name}")
    if not skill_dir.exists():
        return _tool_err(f"skill not found: {name}")
    if not skill_dir.is_dir():
        return _tool_err(f"not a skill directory: {name}")

    _op, err = await request_guard(ctx, "write_file", {"path": f"skills/{name}/SKILL.md", "delete": True})
    if err is not None:
        return _tool_err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    shutil.rmtree(skill_dir)
    return _tool_ok(f"Skill deleted: {name}", deleted=True, name=name)


# ---------------------------------------------------------------------------
#  Memory CRUD tools
# ---------------------------------------------------------------------------

_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_BOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _load_book(path: Path) -> dict[str, Any]:
    """Load a memory book yaml. Returns default structure if missing."""
    # Why: memory CRUD moved into the injection plugin with the cache. How: keep
    # the same tolerant YAML load and default book structure. Purpose: avoid
    # changing how malformed or missing memory books are handled.
    if not path.exists():
        return {"book": path.stem, "entries": []}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception:
        return {"book": path.stem, "entries": []}
    if not isinstance(data, dict):
        return {"book": path.stem, "entries": []}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _save_book(path: Path, data: dict[str, Any]) -> None:
    """Write a memory book yaml back to disk."""
    # Why: save_memory and delete_memory still write YAML books directly. How:
    # retain the old safe_dump format and parent creation. Purpose: keep files
    # compatible with existing memory books.
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


def _invalidate_cache(workspace_root: Path, *, memory_book: str = "") -> None:
    """Clear the memory cache so the next prompt build picks up changes.

    [2026-05-28] 增加 memory_book 参数，与 _MemoryCache.invalidate 对齐。
    为什么：namespace 隔离后 cache key 包含 memory_book，invalidate 也需指定。
    目的：精确清除对应 namespace 的缓存。
    """
    try:
        _MemoryCache.invalidate(workspace_root, memory_book=memory_book)
    except Exception:
        pass


async def save_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a memory entry in a book."""
    # Why: memory management tools now live beside memory loading and injection.
    # How: move the old memory-tool implementation here and keep the
    # async tool signature unchanged. Purpose: let PLUGIN_META register the tool
    # without hard-coding it in toolbox.registry.py.
    mid = str(args.get("id") or "").strip()
    if not mid:
        return _tool_err("empty memory id")
    if not _MEMORY_ID_RE.fullmatch(mid):
        return _tool_err("invalid id: only [A-Za-z0-9][A-Za-z0-9_.-]{0,127} allowed")

    # [2026-05-27] memory_book namespace 支持：当节点 yaml 配置了 memory_book 时，
    # 没有显式指定 book 的 save_memory 调用将使用节点配置的默认 book 名称。
    # 为什么：持久节点的记忆应隔离到独立的 namespace，避免互相污染。
    # 怎么改：从 ToolContext._node_extra dict 零 IO 读取 memory_book，
    #   extra 由 Node.load_node 在加载 yaml 时一次性收集，通过 ai_step 传入。
    # 目的：插件层零 IO 拿到业务配置，引擎核心不知道具体字段的存在。
    _user_book = str(args.get("book") or "").strip()
    if _user_book:
        book = _user_book
    else:
        # 从 Node.extra dict 零 IO 读取 memory_book
        _extra = getattr(ctx, "_node_extra", None) or {}
        book = str(_extra.get("memory_book") or "").strip() or "default"
    if not _BOOK_NAME_RE.fullmatch(book):
        return _tool_err("invalid book name")

    content = str(args.get("content") or "").strip()
    if not content:
        return _tool_err("empty content")

    raw_keywords = args.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        keywords = [
            str(k).strip()
            for k in raw_keywords
            if isinstance(k, str) and str(k).strip()
        ]
    elif isinstance(raw_keywords, str) and raw_keywords.strip():
        keywords = [raw_keywords.strip()]

    constant = bool(args.get("constant", False))
    enabled = bool(args.get("enabled", True))

    # Why: existing memory tools accepted node_ids as a list or comma-separated
    # string. How: preserve both forms. Purpose: node-scoped memory entries keep
    # their tool API compatibility.
    raw_node_ids = args.get("node_ids")
    node_ids: list[str] = []
    if isinstance(raw_node_ids, list):
        node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
    elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
        node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

    priority = 0
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass

    scan_depth = 0
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    # [2026-05-28] 从节点 extra 获取 memory_book namespace，用于写入子目录和 invalidate 对应缓存。
    # 为什么：持久化子节点的记忆应写入 data/memory/{memory_book}/。
    # 怎么改：传 memory_book 给 memory_dir 来计算实际路径。
    _ns_extra = getattr(ctx, "_node_extra", None) or {}
    _ns_memory_book = str(_ns_extra.get("memory_book") or "").strip()
    book_path = memory_dir(ctx.workspace_root, _ns_memory_book) / f"{book}.yaml"
    data = _load_book(book_path)
    data.setdefault("book", book)

    # [AutoC 2026-05-31] Why: save_memory 之前不写 created_at 和 source，
    # 导致 dream 的过期/清理逻辑无法判断条目年龄和来源。
    # How: 新建时写入 created_at + source；更新时保留原 created_at，刷新 updated_at。
    # Purpose: dream Phase 4 过期判断和 source 保护逻辑能正常工作。
    from datetime import datetime, timezone
    _now_iso = datetime.now(timezone.utc).isoformat()

    new_entry: dict[str, Any] = {
        "id": mid,
        "content": content,
        "keywords": keywords,
        "constant": constant,
        "enabled": enabled,
        "priority": priority,
        "scan_depth": scan_depth,
    }

    entries = data["entries"]
    found = False
    for i, e in enumerate(entries):
        if isinstance(e, dict) and str(e.get("id") or "").strip() == mid:
            # 更新：保留原 created_at 和 source，刷新 updated_at
            new_entry["created_at"] = str(e.get("created_at") or "").strip() or _now_iso
            new_entry["source"] = str(e.get("source") or "").strip()
            new_entry["updated_at"] = _now_iso
            entries[i] = new_entry
            found = True
            break
    if not found:
        # 新建：写入 created_at，source 由调用方决定（memory_extract 会补 auto）
        new_entry["created_at"] = _now_iso
        new_entry["updated_at"] = _now_iso
        entries.append(new_entry)

    _save_book(book_path, data)
    # invalidate 时也传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_ns_memory_book)
    return _tool_ok(f"Memory {'updated' if found else 'saved'}: {book}/{mid}", book=book, id=mid, updated=found)


async def list_memories(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List memory entries, optionally filtered by book."""
    # Why: list_memories is now plugin-owned like the memory catalog. How: keep
    # the old book scan and preview fields. Purpose: preserve the tool response.
    # [2026-05-28] namespace 隔离：使用节点 memory_book 确定扫描目录。
    book_filter = str(args.get("book") or "").strip() or None
    _ns_extra = getattr(ctx, "_node_extra", None) or {}
    _ns_memory_book = str(_ns_extra.get("memory_book") or "").strip()
    mem_dir = memory_dir(ctx.workspace_root, _ns_memory_book)
    if not mem_dir.exists():
        return _tool_ok("0 memories", entries=[])

    result: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            data = _load_book(yaml_path)
            bname = str(data.get("book") or yaml_path.stem).strip()
            if book_filter and bname != book_filter:
                continue
            for e in data.get("entries", []):
                if not isinstance(e, dict):
                    continue
                result.append({
                    "book": bname,
                    "id": str(e.get("id") or ""),
                    "content": str(e.get("content") or "")[:200],
                    "keywords": e.get("keywords", []),
                    "constant": bool(e.get("constant", False)),
                    "enabled": bool(e.get("enabled", True)),
                    "priority": int(e.get("priority") or 0),
                    "scan_depth": int(e.get("scan_depth") or 0),
                    "node_ids": e.get("node_ids", []),
                })
        except Exception:
            continue

    return _tool_ok(f"{len(result)} memories", entries=result)


async def delete_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a memory entry from a book."""
    # Why: delete_memory moved into the knowledge plugin with save_memory. How:
    # keep the old constant-memory protection and empty-book removal behavior.
    # Purpose: avoid any tool-level behavior change during registration refactor.
    mid = str(args.get("id") or "").strip()
    if not mid:
        return _tool_err("empty memory id")

    # [2026-05-28] namespace 隔离：使用节点 memory_book 确定存储目录。
    _ns_extra = getattr(ctx, "_node_extra", None) or {}
    _ns_memory_book = str(_ns_extra.get("memory_book") or "").strip()
    book = str(args.get("book") or "default").strip()
    book_path = memory_dir(ctx.workspace_root, _ns_memory_book) / f"{book}.yaml"
    if not book_path.exists():
        return _tool_err(f"book not found: {book}")

    data = _load_book(book_path)
    entries = data.get("entries", [])

    # Why: constant memories are treated as protected baseline context. How: keep
    # the old refusal before filtering entries. Purpose: prevent accidental removal
    # of always-injected memory through the tool API.
    for e in entries:
        if isinstance(e, dict) and str(e.get("id") or "").strip() == mid:
            if bool(e.get("constant", False)):
                return _tool_err(f"cannot delete constant memory: {mid}")
            break

    new_entries = [
        e for e in entries
        if not (isinstance(e, dict) and str(e.get("id") or "").strip() == mid)
    ]
    if len(new_entries) == len(entries):
        return _tool_err(f"memory not found: {mid}")

    data["entries"] = new_entries
    if new_entries:
        _save_book(book_path, data)
    else:
        try:
            book_path.unlink()
        except Exception:
            pass

    # [2026-05-28] invalidate 时传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_ns_memory_book)
    return _tool_ok(f"Memory deleted: {book}/{mid}", book=book, id=mid, deleted=True)


# ---------------------------------------------------------------------------
#  PLUGIN_META tool declarations
# ---------------------------------------------------------------------------

# Why: toolbox.registry.py no longer owns these knowledge tool specs. How: attach
# exact copied descriptions and input schemas to PLUGIN_META after the functions
# exist. Purpose: let engine.builtin.loader register plugin-owned builtin tools.
PLUGIN_META["tools"] = [
    {
        "name": "create_or_update_skill",
        "description": "Create or update a skill under skills/<name>/SKILL.md.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "content": {"type": "string", "description": "full SKILL.md content (optional; frontmatter will be normalized)"},
                "enabled": {"type": "boolean"},
                "strategy": {"type": "string", "description": "constant (always injected) or normal (keyword-triggered); default normal", "enum": ["constant", "normal"]},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "activation keywords; supports /regex/flags syntax"},
                "order": {"type": "integer", "description": "injection order within the same block; higher values are placed later (closer to conversation)"},
                "priority": {"type": "integer", "description": "budget priority; higher values are kept first when token budget is exceeded"},
                "scan_depth": {"type": "integer", "description": "number of recent conversation rounds to scan for keyword matching; 0 = current message only"},
            },
            "required": ["name"],
        },
        "func": create_or_update_skill,
    },
    {
        "name": "list_skills",
        "description": "List local skills under skills/*/SKILL.md.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "func": list_skills,
    },
    {
        "name": "delete_skill",
        "description": "Delete a skill directory under skills/<name>/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
        "func": delete_skill,
    },
    {
        "name": "save_memory",
        "description": "Save or update a memory entry in a book. "
        "Use this when you learn something worth remembering across conversations: "
        "user preferences, corrections, project context, external resource pointers, "
        "or character profiles in group chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique entry id (e.g. user_zhangsan, rule_no_mock)."},
                "book": {"type": "string", "description": "Book name (file grouping). Default 'default'. Use e.g. 'people' for character profiles, 'rules' for behavioral rules."},
                "content": {"type": "string", "description": "Memory content text."},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Activation keywords. Supports /regex/flags. When any keyword matches user input, this memory is injected into context.",
                },
                "constant": {"type": "boolean", "description": "If true, always injected regardless of keywords. Default false."},
                "enabled": {"type": "boolean", "description": "Whether this entry is active. Default true."},
                "priority": {"type": "integer", "description": "Budget priority; higher = kept first when budget exceeded."},
                "scan_depth": {"type": "integer", "description": "Number of recent conversation rounds to scan for keywords. 0 = current message only."},
            },
            "required": ["id", "content"],
        },
        "func": save_memory,
    },
    {
        "name": "list_memories",
        "description": "List memory entries, optionally filtered by book name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "book": {"type": "string", "description": "Filter by book name. Omit to list all."},
            },
            "required": [],
        },
        "func": list_memories,
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory entry from a book.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory entry id to delete."},
                "book": {"type": "string", "description": "Book name. Default 'default'."},
            },
            "required": ["id"],
        },
        "func": delete_memory,
    },
]

class KnowledgeInjector:
    """Glue handler for skill and memory prompt injection."""

    name = "knowledge_inject"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Build skill and memory messages, then optionally rebuild the prompt.

        Why: the previous separate skill and memory prompt hooks duplicated glue
        and filtered conversation history separately. How: filter history once, call
        the unified knowledge builder with either independent or global budgets,
        and store the same ctx.extra keys as before. Purpose: unify ownership
        without changing prompt content, order, labels, or downstream contracts.
        """
        if ctx.node is None or ctx.rctx is None:
            return None

        from engine.inference.message_assembly import _conversational_history

        runtime_cfg = ctx.extra.get("runtime_cfg") or {}
        instruction_text = str(ctx.extra.get("instruction_text") or "")
        history = _conversational_history(ctx.extra.get("history") or [])

        # Why: this hook and the inference/preempt paths must share one builder
        # boundary. How: delegate to build_knowledge_context after filtering the
        # same conversation history as before. Purpose: remove duplicated builder
        # calls without changing ctx.extra keys or prompt layout.
        skill_static, skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
            ctx.rctx.workspace_root,
            ctx.node,
            instruction_text,
            history,
            runtime_cfg,
        )

        ctx.extra["skill_static_messages"] = skill_static
        ctx.extra["skill_dynamic_messages"] = skill_dynamic
        ctx.extra["memory_static_messages"] = memory_static
        ctx.extra["memory_dynamic_messages"] = memory_dynamic

        if ctx.extra.get("apply_injection"):
            _rebuild_prompt_messages(ctx)
            return hook_result(modified=True)
        return hook_result(modified=bool(skill_static or skill_dynamic or memory_static or memory_dynamic))


def _rebuild_prompt_messages(ctx: Any) -> None:
    """Rebuild ctx.messages with all prompt injections currently in ctx.extra.

    Why: the previous prompt rebuild helper was shared by both knowledge paths and
    had to survive the merge. How: keep the same assemble_messages_with_injections
    call in the unified module and read the unchanged ctx.extra key names.
    Purpose: preserve the existing prompt layout while removing the old modules.
    """
    from engine.inference.message_assembly import assemble_messages_with_injections

    rebuilt, is_block_mode = assemble_messages_with_injections(
        workspace_root=ctx.rctx.workspace_root,
        system_prompt=list(ctx.extra.get("system_prompt") or []),
        history=list(ctx.extra.get("history") or []),
        instruction=str(ctx.extra.get("instruction_text") or ""),
        attachments=ctx.extra.get("attachments"),
        skill_static=list(ctx.extra.get("skill_static_messages") or []),
        skill_dynamic=list(ctx.extra.get("skill_dynamic_messages") or []),
        memory_static=list(ctx.extra.get("memory_static_messages") or []),
        memory_dynamic=list(ctx.extra.get("memory_dynamic_messages") or []),
    )
    ctx.messages[:] = rebuilt
    ctx.extra["is_block_mode"] = is_block_mode
