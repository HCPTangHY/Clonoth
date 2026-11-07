from __future__ import annotations

import logging

# [AutoC 2026-08-28] supervisor 端点路由所需。engine 进程同样可导入（与 mcp
# 插件一致），routes face 缺席时不注册任何端点。关键约束：本文件启用
# from __future__ import annotations，全部注解是字符串，FastAPI 在模块
# globals 中解析——Request 与 payload 模型必须 import/定义在模块层。此前
# 它们在 _register_admin_routes 函数体内，注解解析失败，request/payload 被
# 当作必填 query 参数，所有端点 422。
from fastapi import APIRouter, Request
from pydantic import BaseModel


class RawContent(BaseModel):
    content: str


class CreatePayload(BaseModel):
    id: str
    content: str


class SkillPinsPayload(BaseModel):
    session_id: str
    pinned: list[str] = []
import json
import os
import re
import shutil
import threading
import time
import tempfile
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_int
from engine.node import Node
from engine.builtin.memory_hits import (
    hit_key as _memory_hit_key,
    record_hits as _record_hits,
    flush_hits as _flush_hit_cache,
    forget_hit as _forget_memory_hit,
)
from engine.conversation_store import is_dynamic_context
from engine.faces.prompt_sections import content_sha1
from engine.inference.message_assembly import conversational_history, collect_active_injected_hashes
from toolbox._common import request_guard, resolve_under_allowed_roots
from toolbox.builtins import SKILL_NAME_RE
from toolbox.context import ToolContext

logger = logging.getLogger(__name__)


# Why: skill and memory injection are now declarative prompt sections instead
# of a before_prompt_build rebuild handler. How: PLUGIN_META declares no hook
# points; the handler class registers its sections on the prompt_sections face
# at construction. Purpose: the plugin contributes content; the core assembles.
PLUGIN_META = {
    "handler_class": "KnowledgeInjector",
    "hook_points": [
        # [AutoC 2026-08-28] Dream memory router: redirect manage_memory writes
        # from a system node to the original source node directory.
        ("before_tool_call", "_dream_route"),
    ],
    "priority": 50,
    "wants_context": True,
    "description": "知识与记忆插件：skill/memory 声明式 prompt section 注入、manage_skill/manage_memory 统一入口（engine）、skills admin/config 端点与设置面板（supervisor）",
    "author": "core",
    "web": {
        "panels": [
            {
                "id": "settings",
                "slot": "settings",
                "order": 30,
                "title": "技能",
                "icon": "menu_book",
                "entry": "/v1/plugins/knowledge_inject/web/",
            },
            {
                "id": "memory",
                "slot": "settings",
                "order": 40,
                "title": "记忆",
                "icon": "psychology",
                "entry": "/v1/plugins/knowledge_inject/web/memory.html",
            }
        ],
        "slots": [
            {
                # 输入框上方：当前工作区的 skill 挂载 chip，点击展开管理浮层。
                "slot_id": "knowledge.skill_pins",
                "slot": "input_above",
                "priority": 30,
                "mode": "append",
                "script": {"file": "web/skill_pins.js"},
            },
        ],
    },
    # Why: the unified skill and memory entry points belong to the knowledge
    # plugin rather than toolbox.registry.py. How: the concrete declarations are
    # attached after their functions are defined below, and only the unified
    # names are registered. Purpose: keep one source of truth for knowledge
    # behavior and tool registration metadata without reviving retired names.
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


def _scan_content(content: Any) -> str:
    """Extract textual user content, excluding generated attachment bodies."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "") for part in content
        if isinstance(part, dict) and part.get("type") == "text"
        and not str(part.get("text") or "").startswith(
            ("[Attached file:", "[File:", "[Invalid image attachment:")
        )
    )


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
    history = conversational_history(history or [])
    norm_instr = (instruction_text or "").strip()
    parts: list[str] = [(instruction_text or "")]
    if history and scan_depth > 0:
        # [Persistent Dynamic Context] When history already ends with the current
        # instruction (e.g. preempt appends Preempt_User_Msg before rebuild_dynamic_context,
        # or compact_done passes ls.messages containing User_N), exclude that trailing
        # message so scan_depth counts genuine prior rounds rather than double-counting
        # the current instruction.
        scan_end = len(history)
        if norm_instr:
            for j in range(len(history) - 1, -1, -1):
                mj = history[j]
                if not isinstance(mj, dict) or is_dynamic_context(mj):
                    continue
                if mj.get("role") == "user":
                    if _scan_content(mj.get("content")).strip() == norm_instr:
                        scan_end = j
                break
        # Why: the old implementation defined a round as a user message that
        # starts after a non-user message. How: walk backward until enough round
        # starts are found. Purpose: preserve keyword activation scope exactly.
        round_starts: list[int] = []
        for i in range(scan_end - 1, -1, -1):
            msg_i = history[i]
            if msg_i.get("role", "") != "user" or is_dynamic_context(msg_i):
                continue
            prev_is_real_user = (
                i > 0
                and history[i - 1].get("role", "") == "user"
                and not is_dynamic_context(history[i - 1])
            )
            if not prev_is_real_user:
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break

        if round_starts:
            start_idx = round_starts[-1]
            for msg in history[start_idx:scan_end]:
                role = msg.get("role", "")
                if role != "user" or is_dynamic_context(msg):
                    continue
                content = _scan_content(msg.get("content"))
                if content.strip():
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

    @classmethod
    def invalidate(cls, workspace_root: Path) -> None:
        """Drop the cached catalog after a skill CRUD write.

        Why: create/delete endpoints mutated skills/ but called a method that
        did not exist (AttributeError → 500); the catalog then also served stale
        data for the remaining TTL. How: mirror _MemoryCache.invalidate and
        also drop the unused mtime bookkeeping. Purpose: CRUD is visible to the
        next prompt build immediately.
        """
        key = str(workspace_root)
        with cls._lock:
            cls._entries.pop(key, None)
            cls._mtimes.pop(key, None)


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

            # [2026-09-04] Skill visibility: "private" skills are never injected
            # automatically (constant/keyword/INDEX) and only enter the prompt
            # when the user pins them to a workspace. Default "public" keeps the
            # historical behavior.
            visibility = str(meta.get("visibility") or "public").strip().lower()
            if visibility not in ("public", "private"):
                visibility = "public"

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
                "visibility": visibility,
                "body": body.strip(),
                "node_ids": node_ids,
            })
        except Exception:
            continue
    _SkillCache.put(workspace_root, items)
    return items


# ---------------------------------------------------------------------------
#  Workspace skill pins
# ---------------------------------------------------------------------------

# [2026-09-04] Workspace-scoped manual skill mounting.
# Why: constant skills inject for every session of a node and keyword skills
# only fire on matches; users need a middle tier where a skill stays mounted
# for one workspace regardless of keywords. How: pins live in
# data/knowledge_inject/skill_pins.yaml ({workspace_name: [skill_id]}), private to this plugin
# so no core session state is touched. Both engine (injection) and supervisor
# (admin endpoints) read the same file, guarded by an mtime cache. Purpose:
# mounting is a user decision stored beside other plugin data, and a deleted
# session never destroys the mapping.
_skill_pins_cache: dict[str, Any] = {"mtime": -1.0, "data": {}}


def load_skill_pins(workspace_root: Path) -> dict[str, list[str]]:
    """Read data/knowledge_inject/skill_pins.yaml → {workspace_name: [skill_id, ...]}."""
    p = workspace_root / "data" / "knowledge_inject" / "skill_pins.yaml"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _skill_pins_cache["mtime"] = -1.0
        _skill_pins_cache["data"] = {}
        return {}
    if mtime == _skill_pins_cache["mtime"]:
        return _skill_pins_cache["data"]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    data: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for ws, ids in raw.items():
            if isinstance(ids, list):
                data[str(ws)] = [str(i).strip() for i in ids if str(i).strip()]
    _skill_pins_cache["mtime"] = mtime
    _skill_pins_cache["data"] = data
    return data


def save_skill_pins(workspace_root: Path, data: dict[str, list[str]]) -> None:
    """Atomically write data/knowledge_inject/skill_pins.yaml and drop the read cache."""
    p = workspace_root / "data" / "knowledge_inject" / "skill_pins.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    tmp.replace(p)
    _skill_pins_cache["mtime"] = -1.0


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


def _effective_memory_ns(node_extra: dict, node_id: str) -> str:
    """Resolve effective memory namespace: explicit memory_book > node_id."""
    return str(node_extra.get("memory_book") or "").strip() or node_id


def _workspace_memory_dir(workspace_root: Path, base_ns: str, workspace_name: str) -> Path:
    """Return workspace-scoped memory dir: data/memory/{node_id}/@{workspace_name}/."""
    return memory_dir(workspace_root, base_ns) / f"@{workspace_name}"


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
                    "_hit_key": _memory_hit_key(workspace_root, yaml_path, eid),
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


def invalidate_memory_cache(workspace_root: Path, *, memory_book: str = "") -> None:
    """Public cache invalidation for external callers such as memory_extract.

    Why: memory_extract previously reached into the private _MemoryCache class
    across the plugin boundary. How: expose one module-level function.
    Purpose: extracted memories are visible to the next prompt build without
    leaking the cache class.
    """
    _MemoryCache.invalidate(workspace_root, memory_book=memory_book)


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
            "visibility": str(raw.get("visibility") or "public"),
            "book": "",
            "source": "",
            "created_at": "",
            "last_hit_at": "",
        })
    return entries


def normalize_memory_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map memory catalog entries to the unified knowledge-entry dictionary."""
    # Why: memory injection should use the same pipeline as skills. How: map
    # constant memories to constant entries, keyword memories to keyword
    # entries, and keywordless non-constant memories to index entries that stay
    # discoverable without being injected. Purpose: keep every stored memory
    # reachable (index + full read by id) while only relevant ones cost tokens.
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
            strategy = "index"

        entries.append({
            "id": memory_id,
            "_hit_key": str(raw.get("_hit_key") or ""),
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
    # An allowlist without entries grants no access, including an omitted list.
    skill_allow_set = set(skill_allow or [])
    memory_allow_set = set(memory_allow or [])

    for entry in entries:
        node_ids = entry.get("node_ids") or []
        if node_id and node_ids and node_id not in node_ids:
            continue

        if entry.get("kind") == "skill":
            if skill_mode == "none":
                continue
            if skill_mode == "allowlist" and entry.get("id") not in skill_allow_set:
                continue
        elif entry.get("kind") == "memory":
            if memory_mode == "none":
                continue
            # memory_mode=constant injects the read-only constant baseline only.
            if memory_mode == "constant" and entry.get("strategy") != "constant":
                continue
            # Why: only allowlisted books pass in allowlist mode, and an empty
            # allowlist keeps nothing. How: compare against the resolved set.
            # Purpose: book-level authorization cannot be bypassed by omission.
            if memory_mode == "allowlist" and entry.get("book") not in memory_allow_set:
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
        # Keywordless non-constant memories stay discoverable through a compact
        # index; the full body is read back on demand by book+id.
        "memory_index": [],
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

        if kind == "skill":
            buckets["skill_index"].append(entry)

        if strategy == "keyword":
            scan_text = build_scan_text(instruction_text, history, int(entry.get("scan_depth") or 0))
            if match_keywords(list(entry.get("compiled_keywords") or []), scan_text):
                if kind == "skill":
                    buckets["skill_active"].append(entry)
                elif kind == "memory":
                    buckets["memory_active"].append(entry)
                    matched_memories.append(entry)
            continue

        if strategy == "index":
            if kind == "memory":
                buckets["memory_index"].append(entry)

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
    for key in ("memory_constant", "memory_active", "memory_index"):
        buckets[key].sort(key=lambda entry: str(entry.get("id") or ""))


def _memory_dedup_key(entry: dict[str, Any]) -> str:
    """Return book-scoped memory deduplication key ``memory:<book>/<id>``."""
    mem_id = str(entry.get("id") or "")
    book = str(entry.get("book") or "default").strip() or "default"
    return f"memory:{book}/{mem_id}"

def _entry_variant_digests(bucket_name: str, entry: dict[str, Any]) -> tuple[str, str]:
    entry_id = str(entry.get("id") or "")
    content = str(entry.get("content") or "")
    header = f"\n## Skill: {entry_id}" if bucket_name == "skill_active" else f"\n## {entry_id}"
    outline = (_build_skill_outline(entry) if bucket_name == "skill_active"
               else _build_memory_outline(entry))
    return (content_sha1(f"{header}\n\n{content}"),
            content_sha1(f"{header}\n\n{outline}\0{content}"))


def _is_entry_already_active(
    bucket_name: str,
    entry: dict[str, Any],
    active_injected: dict[str, str] | None,
    *,
    skill_inline_max_chars: int = 6000,
    memory_inline_max_chars: int = 2000,
) -> bool:
    """Recognize either surviving variant; budget changes never re-send its source."""
    if not active_injected:
        return False
    entry_id = str(entry.get("id") or "")
    content = str(entry.get("content") or "")
    if not entry_id or not content:
        return False
    digests = _entry_variant_digests(bucket_name, entry)
    keys = ([f"skill:{entry_id}"] if bucket_name == "skill_active"
            else [_memory_dedup_key(entry), f"memory:{entry_id}"])
    return any(active_injected.get(key) in digests for key in keys)

def _apply_active_budget(
    buckets: dict[str, list[dict[str, Any]]],
    bucket_names: tuple[str, ...],
    max_budget_chars: int,
    *,
    active_injected: dict[str, str] | None = None,
    skill_inline_max_chars: int = 6000,
    memory_inline_max_chars: int = 2000,
) -> None:
    """Bound new active bodies per build, including outlines but not static indexes.

    Surviving full/outline entries use no incremental budget and are not rendered
    again. Entries that fit neither form are retried on a future matching turn.
    This is not a total history token limit; normal compaction enforces that.
    """
    if max_budget_chars <= 0:
        return
    ranked = [
        (
            name,
            i,
            entry,
            _is_entry_already_active(
                name,
                entry,
                active_injected,
                skill_inline_max_chars=skill_inline_max_chars,
                memory_inline_max_chars=memory_inline_max_chars,
            ),
        )
        for name in bucket_names
        for i, entry in enumerate(buckets[name])
    ]
    ranked.sort(
        key=lambda item: (1 if item[3] else 0, int(item[2].get("priority") or 0)),
        reverse=True,
    )
    used = 0
    for name, i, entry, already_full in ranked:
        body_len = len(str(entry.get("content") or ""))
        if already_full:
            continue
        inline_max = skill_inline_max_chars if name == "skill_active" else memory_inline_max_chars
        if body_len <= inline_max and used + body_len <= max_budget_chars:
            used += body_len
            continue
        outline = _build_skill_outline(entry) if name == "skill_active" else _build_memory_outline(entry)
        if used + len(outline) <= max_budget_chars:
            used += len(outline)
            buckets[name][i] = {**entry, "_outline_only": True}
        else:
            buckets[name][i] = {**entry, "_budget_dropped": True}
    for name in bucket_names:
        buckets[name] = [entry for entry in buckets[name] if not entry.get("_budget_dropped")]


def _apply_skill_budget(
    buckets: dict[str, list[dict[str, Any]]],
    max_budget_chars: int,
    *,
    active_injected: dict[str, str] | None = None,
    skill_inline_max_chars: int = 6000,
) -> None:
    _apply_active_budget(
        buckets,
        ("skill_active",),
        max_budget_chars,
        active_injected=active_injected,
        skill_inline_max_chars=skill_inline_max_chars,
    )


def _apply_memory_budget(
    buckets: dict[str, list[dict[str, Any]]],
    max_budget_chars: int,
    *,
    active_injected: dict[str, str] | None = None,
    memory_inline_max_chars: int = 2000,
) -> None:
    _apply_active_budget(
        buckets,
        ("memory_active",),
        max_budget_chars,
        active_injected=active_injected,
        memory_inline_max_chars=memory_inline_max_chars,
    )


def _apply_global_budget(
    buckets: dict[str, list[dict[str, Any]]],
    max_budget_chars: int,
    *,
    active_injected: dict[str, str] | None = None,
    skill_inline_max_chars: int = 6000,
    memory_inline_max_chars: int = 2000,
) -> None:
    _apply_active_budget(
        buckets,
        ("skill_active", "memory_active"),
        max_budget_chars,
        active_injected=active_injected,
        skill_inline_max_chars=skill_inline_max_chars,
        memory_inline_max_chars=memory_inline_max_chars,
    )


def _compact_index_desc(text: str, max_chars: int = 60) -> str:
    """One-line, whitespace-normalized description for index blocks."""
    s = " ".join((text or "").split())
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


def _build_skill_outline(entry: dict[str, Any], max_preview_chars: int = 800) -> str:
    """Describe a large skill by headings plus a short intro instead of the body."""
    # Why: one oversized skill must not evict everything after it. How: list the
    # markdown headings and a bounded preview, and point at the file on disk.
    # Purpose: the model keeps a path to the full text without paying for it.
    content = str(entry.get("content") or "")
    headings: list[str] = []
    intro: list[str] = []
    intro_chars = 0
    preview_limit = min(300, max_preview_chars)
    for line_no, line in enumerate(content.splitlines(), 1):
        if re.match(r"^#{1,3}\s+.+", line):
            if len(headings) < 25:
                headings.append(f"- L{line_no}: {line}")
        elif intro_chars < preview_limit and not re.match(r"^#+\s+", line):
            stripped = line.strip()
            if stripped:
                intro.append(stripped)
                intro_chars += len(stripped) + 1
    preview = "\n".join(intro)[:preview_limit]
    skill_path = str(entry.get("path") or "") or f"skills/{entry['id']}/SKILL.md"
    hint = (
        '[Large skill (共 %d 字符) — 仅展示结构大纲与导言。需要具体章节时请调用 '
        'read_file(files=[{"path": "%s", "startLine": ..., "endLine": ...}])]'
        % (len(content), skill_path)
    )
    return hint + f"\nDescription: {entry.get('description') or ''}\nOutline:\n" + "\n".join(headings) + f"\nPreview:\n{preview}"


def _build_memory_outline(entry: dict[str, Any], max_preview_chars: int = 500) -> str:
    """Describe a long memory by preview plus headings and a read-back hint."""
    content = str(entry.get("content") or "")
    headings = [
        f"- L{i}: {line}"
        for i, line in enumerate(content.splitlines(), 1)
        if re.match(r"^#{1,3}\s+.+", line)
    ][:25]
    outline = "\nOutline:\n" + "\n".join(headings) if headings else ""
    hint = (
        "\n[Long memory (共 %d 字符) — 如需完整内容请调用 "
        'manage_memory(action="list", book="%s", id="%s")]'
        % (len(content), entry.get("book") or "default", entry["id"])
    )
    return content[:max_preview_chars] + outline + hint


def _render_skill_messages(
    constant_skills: list[dict[str, Any]],
    dynamic_skills: list[dict[str, Any]],
    index_only_skills: list[dict[str, Any]],
    skill_inline_max_chars: int = 6000,
    active_injected: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Render skill buckets using the legacy SKILLS tags.

    [Persistent Dynamic Context] Why: hashing all active skills as one blob would
    re-inject every skill whenever a single new skill matches. How: after budget
    calculation has marked ``_outline_only``, compute the sha1 of each individual
    skill's rendered block and skip entries whose ``skill:<id>`` hash already
    matches ``active_injected``. Purpose: incremental per-skill deduplication and
    re-injection only when the source changes or its previous injection leaves
    the model context. An existing outline can be read in full via tools.
    """
    static_msgs: list[dict[str, Any]] = []
    dynamic_msgs: list[dict[str, Any]] = []
    active_map = active_injected or {}

    if constant_skills:
        parts: list[str] = ["[SKILLS:CONSTANT]"]
        for entry in constant_skills:
            parts.append(f"\n## Skill: {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/SKILLS:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    skill_hashes: dict[str, str] = {}
    if dynamic_skills:
        kept_blocks: list[tuple[str, str]] = []
        for entry in dynamic_skills:
            skill_id = str(entry.get("id") or "")
            content = str(entry.get("content") or "")
            rendered_body = (
                _build_skill_outline(entry)
                if entry.get("_outline_only") or len(content) > skill_inline_max_chars
                else content
            )
            block_text = f"\n## Skill: {skill_id}\n\n{rendered_body}"
            key = f"skill:{skill_id}"
            digest = (
                content_sha1(f"{block_text}\0{content}")
                if rendered_body != content
                else content_sha1(block_text)
            )
            if _is_entry_already_active("skill_active", entry, active_map):
                continue
            kept_blocks.append((f"\n## Skill: {skill_id}\n", rendered_body))
            skill_hashes[key] = digest
        if kept_blocks:
            dynamic_parts.append("[SKILLS:ACTIVE]")
            for header, body in kept_blocks:
                dynamic_parts.append(header)
                dynamic_parts.append(body)
            dynamic_parts.append("\n[/SKILLS:ACTIVE]")

    if index_only_skills:
        parts = ["[SKILLS:INDEX]",
                 'If a task needs an unactivated skill, read "skills/<name>/SKILL.md" via read_file.']
        for entry in index_only_skills:
            desc = _compact_index_desc(str(entry.get("description") or ""))
            parts.append(f"- {entry['id']}" + (f": {desc}" if desc else ""))
        parts.append("[/SKILLS:INDEX]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    if dynamic_parts:
        dynamic_msgs.append({
            "role": "system",
            "content": "\n".join(dynamic_parts),
            "_injected_hashes": skill_hashes,
        })

    return static_msgs, dynamic_msgs


def _render_memory_messages(
    constant_entries: list[dict[str, Any]],
    dynamic_entries: list[dict[str, Any]],
    index_entries: list[dict[str, Any]] | None = None,
    memory_inline_max_chars: int = 2000,
    active_injected: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Render memory buckets using the legacy MEMORY tags.

    [Persistent Dynamic Context] Why: same as ``_render_skill_messages`` — compare
    each individual memory's rendered block (full text or ``_outline_only``)
    against ``active_injected["memory:<id>"]`` after budgeting. How: skip unchanged
    entries and attach ``_injected_hashes`` for the remaining delta. Purpose:
    per-memory incremental injection across turns and after compaction.
    """
    static_msgs: list[dict[str, Any]] = []
    dynamic_msgs: list[dict[str, Any]] = []
    active_map = active_injected or {}

    if constant_entries:
        parts: list[str] = ["[MEMORY:CONSTANT]"]
        for entry in constant_entries:
            parts.append(f"\n## {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/MEMORY:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    if index_entries:
        parts = ["[MEMORY:INDEX]",
                 'Unindexed memories (use manage_memory(action="list", book="<book>", id="<id>") to read full content when relevant):']
        for entry in index_entries:
            desc = _compact_index_desc(str(entry.get("content") or ""))
            parts.append(f"- {entry.get('book') or 'default'}/{entry['id']}: {desc}")
        parts.append("[/MEMORY:INDEX]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    memory_hashes: dict[str, str] = {}
    if dynamic_entries:
        kept_blocks: list[tuple[str, str]] = []
        for entry in dynamic_entries:
            mem_id = str(entry.get("id") or "")
            content = str(entry.get("content") or "")
            rendered_body = (
                _build_memory_outline(entry)
                if entry.get("_outline_only") or len(content) > memory_inline_max_chars
                else content
            )
            block_text = f"\n## {mem_id}\n\n{rendered_body}"
            key = _memory_dedup_key(entry)
            digest = (
                content_sha1(f"{block_text}\0{content}")
                if rendered_body != content
                else content_sha1(block_text)
            )
            if _is_entry_already_active("memory_active", entry, active_map):
                continue
            kept_blocks.append((f"\n## {mem_id}\n", rendered_body))
            memory_hashes[key] = digest
        if kept_blocks:
            dynamic_parts.append("[MEMORY:ACTIVE]")
            for header, body in kept_blocks:
                dynamic_parts.append(header)
                dynamic_parts.append(body)
            dynamic_parts.append("\n[/MEMORY:ACTIVE]")

    if dynamic_parts:
        dynamic_msgs.append({
            "role": "system",
            "content": "\n".join(dynamic_parts),
            "_injected_hashes": memory_hashes,
        })

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
    skill_inline_max_chars: int = 6000,
    memory_inline_max_chars: int = 2000,
    active_injected: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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

    # [Persistent Dynamic Context] Extract surviving injected_hashes from raw
    # history before stripping dynamic messages for keyword scanning.
    effective_active = (
        active_injected
        if active_injected is not None
        else collect_active_injected_hashes(history)
    )
    scan_history = conversational_history(history) if history else []

    buckets = _classify_and_match_entries(
        workspace_root,
        filtered_entries,
        instruction_text=instruction_text,
        history=scan_history,
    )
    _sort_render_buckets(buckets)

    if knowledge_max_budget_chars > 0:
        _apply_global_budget(
            buckets,
            knowledge_max_budget_chars,
            active_injected=effective_active,
            skill_inline_max_chars=skill_inline_max_chars,
            memory_inline_max_chars=memory_inline_max_chars,
        )
    else:
        _apply_skill_budget(
            buckets,
            skill_max_budget_chars,
            active_injected=effective_active,
            skill_inline_max_chars=skill_inline_max_chars,
        )
        _apply_memory_budget(
            buckets,
            memory_max_budget_chars,
            active_injected=effective_active,
            memory_inline_max_chars=memory_inline_max_chars,
        )

    skill_static, skill_dynamic = _render_skill_messages(
        buckets["skill_constant"],
        buckets["skill_active"],
        buckets["skill_index"],
        skill_inline_max_chars=skill_inline_max_chars,
        active_injected=effective_active,
    )
    memory_static, memory_dynamic = _render_memory_messages(
        buckets["memory_constant"],
        buckets["memory_active"],
        buckets["memory_index"],
        memory_inline_max_chars=memory_inline_max_chars,
        active_injected=effective_active,
    )
    return skill_static, skill_dynamic, memory_static, memory_dynamic


def build_knowledge_context(
    workspace_root: Path,
    node: Node,
    instruction_text: str,
    history: list[dict],
    runtime_cfg: dict,
    task_context: dict | None = None,
    *,
    active_injected: dict[str, str] | None = None,
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
    _mb = _effective_memory_ns(node.extra, node.id)
    _effective_node_id = node.id
    # Why: system maintenance nodes run with memories:constant, which should show
    # the source namespace's constant baseline, not their own empty directory.
    # How: resolve the routed namespace (extractor passes _memory_route_ns, dream
    # passes a route table) and use it for both loading and node_ids filtering.
    # Purpose: the constant block is read-only reference material for a system
    # node while its own writes still get routed by dream_router.
    if (
        node.id in _SYSTEM_MEMORY_NODES
        and not str((node.extra or {}).get("memory_book") or "").strip()
    ):
        from clonoth_runtime import get_entry_node_id

        _routed_ns = str((task_context or {}).get("_memory_route_ns") or "").strip()
        if not _routed_ns:
            _route_tbl = (task_context or {}).get("_memory_route")
            if isinstance(_route_tbl, dict):
                _first_rt = next((v for v in _route_tbl.values() if isinstance(v, dict)), None)
                if _first_rt is not None:
                    _routed_ns = str(_first_rt.get("ns") or "").strip()
        _mb = _routed_ns or get_entry_node_id(safe_runtime_cfg) or _mb
        _effective_node_id = _mb
    # [AutoC 2026-08-10] Workspace-scoped memory: load workspace dir first, then node dir.
    _ws_name = str((task_context or {}).get("workspace_name") or "").strip()
    mem_entries = []
    if _ws_name:
        _ws_dir = _workspace_memory_dir(workspace_root, _mb, _ws_name)
        if _ws_dir.is_dir():
            mem_entries.extend(load_memory_catalog(workspace_root, memory_book=f"{_mb}/@{_ws_name}"))
    # Node-level memories (always loaded, workspace memories are additive)
    _node_mem = load_memory_catalog(workspace_root, memory_book=_mb)
    if _node_mem:
        # Dedup by (book, id) — workspace entries take precedence
        _seen = {(str(e.get("book") or e.get("_book") or ""), str(e.get("id") or "")) for e in mem_entries}
        for e in _node_mem:
            _key = (str(e.get("book") or e.get("_book") or ""), str(e.get("id") or ""))
            if _key not in _seen:
                mem_entries.append(e)
    if not mem_entries:
        # ponytail: fallback to root data/memory/
        mem_entries = load_memory_catalog(workspace_root, memory_book="")
    entries.extend(normalize_memory_entries(mem_entries))

    # [2026-09-04] Apply workspace pins and skill visibility. Why: private
    # skills must not leak through constant/keyword/INDEX paths, and pinned
    # skills must stay mounted for the whole workspace. How: pinned skill ids
    # are promoted to constant; private skills are dropped unless pinned. Pin
    # wins over visibility so a private skill becomes mountable on purpose.
    _pins = set(load_skill_pins(workspace_root).get(_ws_name) or []) if _ws_name else set()
    _next_entries: list[dict[str, Any]] = []
    for e in entries:
        if e.get("kind") == "skill":
            if e.get("id") in _pins:
                if e.get("strategy") != "constant":
                    e = {**e, "strategy": "constant"}
            elif e.get("visibility") == "private":
                continue
        _next_entries.append(e)
    entries = _next_entries

    return build_knowledge_messages(
        workspace_root,
        entries,
        node_id=_effective_node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
        memory_mode=node.memory_access.mode,
        memory_allow=node.memory_access.allow,
        skill_max_budget_chars=get_int(safe_runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
        memory_max_budget_chars=get_int(safe_runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
        knowledge_max_budget_chars=get_int(safe_runtime_cfg, "knowledge.max_budget_chars", 0, min_value=0),
        skill_inline_max_chars=get_int(safe_runtime_cfg, "skills.inline_max_chars", 6000, min_value=0),
        memory_inline_max_chars=get_int(safe_runtime_cfg, "memory.inline_max_chars", 2000, min_value=0),
        active_injected=active_injected,
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


def _action_error(action: str) -> str:
    return f"invalid action: {action!r}, must be 'list', 'upsert', or 'delete'"


def _resolve_action(args: dict[str, Any]) -> str:
    """Resolve the unified tool action: default 'list', mutations must be explicit.

    Why: the same call must mean the same thing for the model, the policy layer,
    the tool-call log, and the handler. How: only an explicit ``action`` selects a
    mutation; anything else is a read. Purpose: no cursor-based guessing about
    whether a call intended to write.
    """
    action = str(args.get("action") or "").strip().lower()
    return action or "list"


async def _guard_mutation(
    ctx: ToolContext,
    path: Path,
) -> dict[str, Any] | None:
    """Ask policy for approval on the real absolute path of one store mutation.

    Why: knowledge tools are auto-approved by name, so the only meaningful write
    gate is the policy decision for the exact file. How: request the write_file
    guard with the resolved absolute path, and while the decision is made present
    the real caller node identity even when dream_router redirected the write to a
    source namespace. Purpose: approvals and audit trail stay bound to the task
    that actually writes, not to the node id the router swapped in.
    """
    caller_id = str(getattr(ctx, "_caller_node_id", "") or "").strip()
    original_id = str(getattr(ctx, "node_id", "") or "")
    restore = bool(caller_id and caller_id != original_id)
    if restore:
        ctx.node_id = caller_id
    try:
        _op, err = await request_guard(ctx, "write_file", {"path": str(path)})
    finally:
        if restore:
            ctx.node_id = original_id
    if err is not None:
        return _tool_err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))
    return None


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

    visibility = str(args.get("visibility", "") or "").strip().lower() or None
    if visibility is not None and visibility not in ("public", "private"):
        return _tool_err("invalid visibility: 'public' or 'private'")

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
        if visibility:
            meta["visibility"] = visibility
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
        if visibility:
            meta["visibility"] = visibility
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"

    try:
        target = resolve_under_allowed_roots(ctx.workspace_root, path, workspace=ctx.workspace)
    except ValueError as exc:
        return _tool_err(exc)
    # Why: the approval decision must name the file that is actually written. How:
    # hand write_file the resolved absolute path so its policy guard evaluates the
    # real location instead of a relative guess. Purpose: one guarded write per
    # skill mutation with an auditable path.
    from toolbox.builtins.write_file import write_file

    res = await write_file({"path": str(target), "content": content}, ctx)
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
                    "visibility": "private" if str(meta.get("visibility") or "public").strip().lower() == "private" else "public",
                    "keywords": kw_list,
                    "order": item_order,
                    "priority": item_priority,
                    "scan_depth": item_scan_depth,
                    "path": rel,
                }
            )
        except Exception as e:
            items.append({"name": skill_md.parent.name, "path": skill_md.relative_to(ctx.workspace_root).as_posix(), "error": str(e)})

    # data.result is the model-visible transcript; sibling fields are host data.
    return _tool_ok(json.dumps(items, ensure_ascii=False, indent=2) if items else "0 skills", skills=items)


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

    skill_dir = resolve_under_allowed_roots(ctx.workspace_root, f"skills/{name}", workspace=ctx.workspace)
    if not skill_dir.exists():
        return _tool_err(f"skill not found: {name}")
    if not skill_dir.is_dir():
        return _tool_err(f"not a skill directory: {name}")

    # Why: deletion must be approved against the directory that really disappears.
    # How: guard the resolved absolute SKILL.md path. Purpose: no relative-path
    # approval can be reused for a different directory.
    denied = await _guard_mutation(ctx, skill_dir / "SKILL.md")
    if denied is not None:
        return denied

    shutil.rmtree(skill_dir)
    return _tool_ok(f"Skill deleted: {name}", deleted=True, name=name)


async def manage_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Unified skill management: list (default), upsert, or delete."""
    # Why: one tool now fronts the skill store instead of three CRUD names. How:
    # resolve the explicit action and delegate to the existing implementation, so
    # the old names stay retired while the behavior stays identical. Purpose: a
    # single, predictable contract that matches schema, policy, and audit logs.
    if not isinstance(args, dict):
        return _tool_err("arguments must be an object")
    action = _resolve_action(args)
    if action == "list":
        return await list_skills(args, ctx)
    if action == "upsert":
        if not str(args.get("name") or "").strip():
            return _tool_err("upsert requires name")
        return await create_or_update_skill(args, ctx)
    if action == "delete":
        if not str(args.get("name") or "").strip():
            return _tool_err("delete requires name")
        return await delete_skill(args, ctx)
    return _tool_err(_action_error(action))


# ---------------------------------------------------------------------------
#  Memory CRUD tools
# ---------------------------------------------------------------------------

_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_BOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

# System maintenance nodes write on behalf of other namespaces. They may add or
# update ordinary entries, but the constant baseline stays read-only for them.
_SYSTEM_MEMORY_NODES = frozenset({"system.dream", "system.memory_extractor"})


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
    """Write a memory book yaml back to disk atomically."""
    # Why: save_memory and delete_memory still write YAML books directly. How:
    # dump next to the target and swap it in with one rename. Purpose: a crash,
    # a denied write, or a full disk must never leave a half-written book behind
    # - readers see either the old book or the new one.
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _memory_tool_scope(ctx: ToolContext) -> tuple[str, str]:
    """Resolve storage routing without replacing the caller's identity/workspace."""
    target = getattr(ctx, "_memory_target", None)
    if isinstance(target, tuple) and len(target) == 2:
        return str(target[0]).strip(), str(target[1]).strip()
    extra = getattr(ctx, "_node_extra", None) or {}
    node_id = ctx.node_id or getattr(ctx, "_node_id", "") or ""
    return _effective_memory_ns(extra, node_id), str(getattr(ctx, "workspace_name", "") or "").strip()


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
    _ns_memory_book, _ws_name = _memory_tool_scope(ctx)
    # scope: "workspace" writes to @{workspace_name}/ subdir.
    _scope = str(args.get("scope") or "workspace").strip().lower()
    if _scope == "workspace" and _ws_name:
        _target_ns = f"{_ns_memory_book}/@{_ws_name}"
    else:
        _target_ns = _ns_memory_book
    # Why: system nodes see the source node's constant entries read-only. How:
    # identify the real caller (dream_router rewrote node_id to the target ns)
    # and refuse constant writes plus any shadowing of an existing constant id.
    # Purpose: automated maintenance cannot rewrite always-injected context, and
    # cannot hide it behind a same-id entry in another namespace.
    _caller_nid = str(
        getattr(ctx, "_caller_node_id", "") or ctx.node_id or getattr(ctx, "_node_id", "") or ""
    ).strip()
    _is_sys_mem_node = _caller_nid in _SYSTEM_MEMORY_NODES
    if _is_sys_mem_node:
        if constant:
            return _tool_err(f"system node ({_caller_nid}) cannot set constant=true")
        # Background extraction/cleanup must not create historical-scan triggers.
        scan_depth = 0
        for _check_ns in dict.fromkeys([_target_ns, _ns_memory_book, ""]):
            for _cat_item in load_memory_catalog(ctx.workspace_root, memory_book=_check_ns):
                if _cat_item.get("id") == mid and bool(_cat_item.get("constant", False)):
                    return _tool_err(
                        f"system node ({_caller_nid}) cannot modify constant memory: "
                        f"{_cat_item.get('book')}/{mid}"
                    )
    book_path = memory_dir(ctx.workspace_root, _target_ns) / f"{book}.yaml"
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
    # node_ids is the per-entry visibility list; omitting it means "every node in
    # the namespace", so only write the key when the caller asked for a subset.
    if node_ids:
        new_entry["node_ids"] = node_ids

    entries = data["entries"]
    found = False
    for i, e in enumerate(entries):
        if isinstance(e, dict) and str(e.get("id") or "").strip() == mid:
            # 更新：保留原 created_at 和 source，刷新 updated_at；
            # 未显式传 node_ids 时保留原可见性，避免更新把条目 broadcast 给所有节点。
            if raw_node_ids is None and e.get("node_ids"):
                new_entry["node_ids"] = e.get("node_ids")
            new_entry["created_at"] = str(e.get("created_at") or "").strip() or _now_iso
            new_entry["source"] = str(e.get("source") or "").strip()
            new_entry["updated_at"] = _now_iso
            entries[i] = new_entry
            found = True
            break
    if not found:
        # 新建：写入 created_at；系统提取/整理节点产出的条目标记 source=auto，
        # dream 的过期清理与提升逻辑只依赖这个来源标记。
        if _is_sys_mem_node and not new_entry.get("source"):
            new_entry["source"] = "auto"
        new_entry["created_at"] = _now_iso
        new_entry["updated_at"] = _now_iso
        entries.append(new_entry)

    denied = await _guard_mutation(ctx, book_path)
    if denied is not None:
        return denied

    _save_book(book_path, data)
    # invalidate 时也传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_target_ns)
    return _tool_ok(f"Memory {'updated' if found else 'saved'}: {book}/{mid}", book=book, id=mid, updated=found,
                    scope=_scope, workspace=_ws_name if _scope == 'workspace' and _ws_name else None)


async def list_memories(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List memory entries, optionally filtered by book."""
    # Why: list_memories is now plugin-owned like the memory catalog. How: keep
    # the old book scan and preview fields. Purpose: preserve the tool response.
    # [AutoC 2026-08-31] 与 save_memory 的 scope 语义对齐：workspace 作用域的
    # 记忆存放在 data/memory/{ns}/@{ws}/ 子目录，旧实现只扫节点级目录，节点
    # 看不到自己刚保存的 workspace 记忆。How: 扫描节点级目录与全部 @ 子目录，
    # 每条目标记 scope/workspace 来源；ns 目录不存在时保留 root 兜底。
    book_filter = str(args.get("book") or "").strip() or None
    # Why: dream needs the full body of one entry without a full scan. How: an
    # explicit id filter returns the untruncated content of exactly that entry.
    # Purpose: index/outline entries stay readable on demand.
    id_filter = str(args.get("id") or "").strip() or None
    _ns_memory_book, _caller_ws = _memory_tool_scope(ctx)

    sources: list[tuple[Path, str, str]] = []  # (dir, scope, workspace)
    # Workspace isolation: a scoped caller only sees node-level entries plus its
    # own @workspace directory. Cross-workspace listing is reserved for unscoped
    # maintenance nodes (dream / extractor), which carry no workspace_name.
    base_dir = memory_dir(ctx.workspace_root, _ns_memory_book)
    if base_dir.is_dir():
        sources.append((base_dir, "node", ""))
        for sub in sorted(base_dir.iterdir()):
            if not sub.is_dir() or not sub.name.startswith("@"):
                continue
            if _caller_ws and sub.name[1:] != _caller_ws:
                continue
            sources.append((sub, "workspace", sub.name[1:]))
    if not sources:
        # ponytail: fallback to root
        root_dir = memory_dir(ctx.workspace_root, "")
        if root_dir.is_dir():
            sources.append((root_dir, "node", ""))
    if not sources:
        return _tool_ok("0 memories", entries=[])

    result: list[dict[str, Any]] = []
    for mem_dir, scope, ws_name in sources:
        for yaml_path in sorted(mem_dir.glob("*.yaml")):
            try:
                data = _load_book(yaml_path)
                bname = str(data.get("book") or yaml_path.stem).strip()
                if book_filter and bname != book_filter:
                    continue
                for e in data.get("entries", []):
                    if not isinstance(e, dict):
                        continue
                    if id_filter and str(e.get("id") or "").strip() != id_filter:
                        continue
                    result.append({
                        "book": bname,
                        "id": str(e.get("id") or ""),
                        "content": str(e.get("content") or "") if id_filter else str(e.get("content") or "")[:200],
                        "keywords": e.get("keywords", []),
                        "constant": bool(e.get("constant", False)),
                        "enabled": bool(e.get("enabled", True)),
                        "priority": int(e.get("priority") or 0),
                        "scan_depth": int(e.get("scan_depth") or 0),
                        "node_ids": e.get("node_ids", []),
                        "scope": scope,
                        "workspace": ws_name or None,
                    })
            except Exception:
                continue

    if id_filter:
        return _tool_ok(
            json.dumps(result, ensure_ascii=False, indent=2) if result else f"memory not found: {id_filter}",
            entries=result,
        )
    return _tool_ok(json.dumps(result, ensure_ascii=False, indent=2) if result else "0 memories", entries=result)


async def delete_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a memory entry from a book."""
    # Why: delete_memory moved into the knowledge plugin with save_memory. How:
    # keep the old constant-memory protection and empty-book removal behavior.
    # Purpose: avoid any tool-level behavior change during registration refactor.
    mid = str(args.get("id") or "").strip()
    if not mid:
        return _tool_err("empty memory id")

    if not _MEMORY_ID_RE.fullmatch(mid):
        return _tool_err("invalid memory id")
    # Validate before composing paths: book names cannot cross namespaces.
    _ns_memory_book, _ws_name = _memory_tool_scope(ctx)
    _explicit_book = str(args.get("book") or "").strip()
    if _explicit_book and not _BOOK_NAME_RE.fullmatch(_explicit_book):
        return _tool_err("invalid book name")
    book = _explicit_book or "default"
    # [AutoC 2026-08-31] 查找顺序与 save_memory 的默认落点（scope=workspace）
    # 对齐：当前 workspace 子目录优先，其次节点级，最后 root 兜底。How: 第一个
    # 包含目标 id 的书文件胜出；书文件存在但不含该 id 时继续向下找。Purpose:
    # 节点能删掉自己刚保存的 workspace 作用域记忆，无需显式传 scope。
    candidates: list[tuple[Path, str]] = []
    if _ws_name:
        _ws_ns = f"{_ns_memory_book}/@{_ws_name}"
        candidates.append((memory_dir(ctx.workspace_root, _ws_ns) / f"{book}.yaml", _ws_ns))
    candidates.append((memory_dir(ctx.workspace_root, _ns_memory_book) / f"{book}.yaml", _ns_memory_book))
    candidates.append((memory_dir(ctx.workspace_root, "") / f"{book}.yaml", ""))

    if not _explicit_book and _ns_memory_book and _ns_memory_book != book:
        # 兼容旧版 save_memory：未显式传 book 时曾以 namespace 作为书名。
        if _ws_name:
            _ws_ns = f"{_ns_memory_book}/@{_ws_name}"
            candidates.append((memory_dir(ctx.workspace_root, _ws_ns) / f"{_ns_memory_book}.yaml", _ws_ns))
        candidates.append((memory_dir(ctx.workspace_root, _ns_memory_book) / f"{_ns_memory_book}.yaml", _ns_memory_book))
        candidates.append((memory_dir(ctx.workspace_root, "") / f"{_ns_memory_book}.yaml", ""))

    book_path: Path | None = None
    for _cand_path, _cand_ns in candidates:
        if not _cand_path.exists():
            continue
        if book_path is None:
            book_path, _ns_memory_book = _cand_path, _cand_ns
        _cand_data = _load_book(_cand_path)
        if any(
            isinstance(e, dict) and str(e.get("id") or "").strip() == mid
            for e in _cand_data.get("entries", [])
        ):
            book_path, _ns_memory_book = _cand_path, _cand_ns
            break
    if book_path is None:
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

    denied = await _guard_mutation(ctx, book_path)
    if denied is not None:
        return denied

    data["entries"] = new_entries
    if new_entries:
        _save_book(book_path, data)
    else:
        try:
            book_path.unlink()
        except OSError:
            return _tool_err(f"cannot delete memory book: {book}")
        if book_path.parent.name.startswith("@"):
            try:
                book_path.parent.rmdir()  # Remove only this empty workspace directory.
            except OSError:
                pass  # Other books or concurrent writes keep the directory alive.

    # [2026-05-28] invalidate 时传 memory_book，精确清除对应 namespace 的缓存
    _invalidate_cache(ctx.workspace_root, memory_book=_ns_memory_book)
    _forget_memory_hit(ctx.workspace_root, book_path, mid)
    return _tool_ok(f"Memory deleted: {book}/{mid}", book=book, id=mid, deleted=True)


async def manage_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Unified memory management: list (default), upsert, or delete."""
    # Why: one tool fronts the memory store instead of three CRUD names. How:
    # resolve the explicit action, validate the arguments that action needs, and
    # delegate to the existing implementation. Purpose: schema, policy and audit
    # agree because nothing is inferred from which fields happen to be present.
    if not isinstance(args, dict):
        return _tool_err("arguments must be an object")
    action = _resolve_action(args)
    if action == "list":
        return await list_memories(args, ctx)
    if action == "upsert":
        if not str(args.get("id") or "").strip():
            return _tool_err("upsert requires id")
        if not str(args.get("content") or "").strip():
            return _tool_err("upsert requires content")
        return await save_memory(args, ctx)
    if action == "delete":
        if not str(args.get("id") or "").strip():
            return _tool_err("delete requires id")
        return await delete_memory(args, ctx)
    return _tool_err(_action_error(action))


# ---------------------------------------------------------------------------
#  PLUGIN_META tool declarations
# ---------------------------------------------------------------------------

# Why: toolbox.registry.py no longer owns these knowledge tool specs. How: attach
# exact copied descriptions and input schemas to PLUGIN_META after the functions
# exist. Purpose: let engine.builtin.loader register plugin-owned builtin tools.
PLUGIN_META["tools"] = [
    {
        "name": "manage_skill",
        "description": (
            "Manage local skills under skills/<name>/SKILL.md: "
            "action='list' lists all local skills (default); "
            "action='upsert' creates or updates a skill (frontmatter normalized); "
            "action='delete' deletes a skill directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "upsert", "delete"],
                    "description": "Operation: 'list' (default), 'upsert' (create/update), or 'delete'.",
                },
                "name": {"type": "string", "description": "[upsert/delete] Skill name (directory name under skills/)."},
                "description": {"type": "string", "description": "[upsert] Short skill description."},
                "content": {"type": "string", "description": "[upsert] Full SKILL.md content (optional; frontmatter will be normalized)."},
                "enabled": {"type": "boolean", "description": "[upsert] Whether the skill is enabled."},
                "strategy": {"type": "string", "description": "[upsert] constant (always injected) or normal (keyword-triggered); default normal", "enum": ["constant", "normal"]},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "[upsert] Activation keywords; supports /regex/flags syntax."},
                "order": {"type": "integer", "description": "[upsert] Injection order within the same block; higher values are placed later (closer to conversation)."},
                "priority": {"type": "integer", "description": "[upsert] Budget priority; higher values are kept first when token budget is exceeded."},
                "scan_depth": {"type": "integer", "description": "[upsert] Number of recent conversation rounds to scan for keyword matching; 0 = current message only."},
                "visibility": {"type": "string", "description": "[upsert] public (constant/keyword activation allowed) or private (injected only when pinned to a workspace); default public", "enum": ["public", "private"]},
            },
            "required": ["action"],
        },
        "func": manage_skill,
    },
    {
        "name": "manage_memory",
        "description": (
            "Manage persistent memory entries in books: "
            "action='list' lists memory entries (default; passing id returns that entry's full untruncated content); "
            "action='upsert' saves or updates a memory entry (memory content MUST be concise, high-density, and to the point / 写记忆文字需简明扼要，不啰嗦不水字数); "
            "action='delete' deletes a memory entry by id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "upsert", "delete"],
                    "description": "Operation: 'list' (default), 'upsert' (create/update), or 'delete'. Changes are never inferred from other fields.",
                },
                "id": {"type": "string", "description": "[upsert/delete/list] Unique entry id (e.g. user_zhangsan, rule_no_mock). In 'list' mode, specifying id returns full untruncated content."},
                "book": {"type": "string", "description": "Book name (file grouping). Default 'default' (or all books in 'list' mode when omitted). Use e.g. 'people', 'rules'."},
                "content": {"type": "string", "description": "[upsert] Memory content text. Must be concise, high-density, and free of filler/padding (文字需简明扼要，不啰嗦不水字数)."},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "[upsert] Activation keywords. Supports /regex/flags. When any keyword matches user input, this memory is injected into context.",
                },
                "constant": {"type": "boolean", "description": "[upsert] If true, always injected regardless of keywords. Default false."},
                "enabled": {"type": "boolean", "description": "[upsert] Whether this entry is active. Default true."},
                "priority": {"type": "integer", "description": "[upsert] Budget priority; higher = kept first when budget exceeded."},
                "scan_depth": {"type": "integer", "description": "[upsert] Number of recent conversation rounds to scan for keywords. 0 = current message only."},
                "scope": {"type": "string", "enum": ["workspace", "node"], "description": "[upsert] Storage scope. 'workspace' (default): saved under the active workspace. 'node': shared across all workspaces."},
                "node_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "[upsert] Optional list of node IDs allowed to see this memory entry (omit for all nodes in the namespace).",
                },
            },
            "required": ["action"],
        },
        "func": manage_memory,
    },
]


# ---------------------------------------------------------------------------
#  supervisor 侧 skills admin 端点与设置面板静态资源
# ---------------------------------------------------------------------------

def _ws_root(request: Any) -> Path:
    return Path(request.app.state.state.workspace_root)


def _skill_md_path(workspace_root: Path, name: str) -> Path:
    """Resolve skills/{name}/SKILL.md with a name whitelist and containment check."""
    if not SKILL_NAME_RE.match(name or ""):
        raise _http_exc(400, "Invalid skill name")
    base = (workspace_root / "skills").resolve()
    p = (base / name / "SKILL.md").resolve()
    if not str(p).startswith(str(base)):
        raise _http_exc(400, "Invalid path")
    return p


def _http_exc(status: int, detail: str) -> Any:
    from fastapi import HTTPException
    return HTTPException(status_code=status, detail=detail)


def _list_skills_endpoint(request: Any) -> list[dict[str, Any]]:
    # Why: admin listing shares the injection-side catalog parser. How: call
    # load_skill_catalog with the cache disabled so writes are immediately
    # visible. Purpose: one frontmatter parser instead of an admin_api copy.
    ws = _ws_root(request)
    res: list[dict[str, Any]] = []
    for item in load_skill_catalog(ws, _use_cache=False):
        res.append({
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "enabled": bool(item.get("enabled", True)),
            "strategy": str(item.get("strategy") or "normal"),
            "visibility": str(item.get("visibility") or "public"),
            "keywords": [str(k) for k in (item.get("keywords") or []) if k],
            "body_preview": str(item.get("body") or "")[:200],
        })
    return res


def _get_skill_raw_endpoint(name: str, request: Any) -> dict[str, str]:
    p = _skill_md_path(_ws_root(request), name)
    if not p.exists():
        raise _http_exc(404, "File not found")
    return {"content": p.read_text(encoding="utf-8")}


def _update_skill_raw_endpoint(name: str, request: Any, payload: Any) -> dict[str, Any]:
    ws = _ws_root(request)
    p = _skill_md_path(ws, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(payload.content), encoding="utf-8")
    _SkillCache.invalidate(ws)
    return {"ok": True}


def _create_skill_endpoint(request: Any, payload: Any) -> dict[str, Any]:
    from toolbox.builtins import SKILL_NAME_RE as _re
    ident = str(getattr(payload, "id", "")).strip()
    if not _re.match(ident):
        raise _http_exc(400, "Invalid skill name")
    ws = _ws_root(request)
    p = _skill_md_path(ws, ident)
    if p.exists():
        raise _http_exc(409, "Skill already exists")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(getattr(payload, "content", "")), encoding="utf-8")
    _SkillCache.invalidate(ws)
    return {"ok": True}


def _delete_skill_endpoint(name: str, request: Any) -> dict[str, Any]:
    import shutil as _shutil
    ws = _ws_root(request)
    p = _skill_md_path(ws, name)
    if p.parent.exists() and p.parent.is_dir() and p.parent != (ws / "skills").resolve():
        _shutil.rmtree(p.parent)
    _SkillCache.invalidate(ws)
    return {"ok": True}


# [2026-09-04] Workspace skill pin endpoints.
# Why: the input-bar chip needs to read and edit the current workspace's pin
# list without knowing workspace internals. How: the caller passes only
# session_id; the supervisor resolves the workspace name from session state
# and maps it onto data/knowledge_inject/skill_pins.yaml. Purpose: the frontend stays
# workspace-agnostic and the mapping cannot be spoofed by the client.
def _session_workspace_name(request: Any, session_id: str) -> str:
    """Resolve a session's workspace name from supervisor state."""
    st = getattr(request.app.state, "state", None)
    sessions = getattr(st, "sessions", None)
    si = sessions.get(session_id) if isinstance(sessions, dict) else None
    if si is None:
        return ""
    ws = getattr(si, "workspace", None)
    if isinstance(ws, dict):
        return str(ws.get("name") or "").strip()
    return ""


def _get_skill_pins_endpoint(session_id: str, request: Any) -> dict[str, Any]:
    ws_root = _ws_root(request)
    ws_name = _session_workspace_name(request, session_id)
    pinned = list(load_skill_pins(ws_root).get(ws_name) or []) if ws_name else []
    pinned_set = set(pinned)
    skills: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in load_skill_catalog(ws_root, _use_cache=False):
        name = str(item.get("name") or "")
        if not name:
            continue
        names.add(name)
        skills.append({
            "name": name,
            "description": str(item.get("description") or ""),
            "visibility": str(item.get("visibility") or "public"),
            "pinned": name in pinned_set,
        })
    # 过滤掉已不存在于 catalog 的 pin，保持响应与实际生效一致。
    return {
        "workspace": ws_name,
        "pinned": [n for n in pinned if n in names],
        "skills": skills,
    }


def _put_skill_pins_endpoint(request: Any, payload: Any) -> dict[str, Any]:
    ws_root = _ws_root(request)
    ws_name = _session_workspace_name(request, str(getattr(payload, "session_id", "") or ""))
    if not ws_name:
        raise _http_exc(400, "Session has no workspace")
    catalog_names = {
        str(item.get("name") or "")
        for item in load_skill_catalog(ws_root, _use_cache=False)
        if str(item.get("name") or "")
    }
    pinned: list[str] = []
    for raw in (getattr(payload, "pinned", None) or []):
        name = str(raw).strip()
        if name and name in catalog_names and name not in pinned:
            pinned.append(name)
    data = {k: list(v) for k, v in load_skill_pins(ws_root).items()}
    if pinned:
        data[ws_name] = pinned
    else:
        data.pop(ws_name, None)
    save_skill_pins(ws_root, data)
    return {"ok": True, "workspace": ws_name, "pinned": pinned}


def _skill_pins_get_route(request: Request, session_id: str = "") -> dict[str, Any]:
    return _get_skill_pins_endpoint(session_id, request)


def _skill_pins_put_route(request: Request, payload: SkillPinsPayload) -> dict[str, Any]:
    return _put_skill_pins_endpoint(request, payload)


def _memory_book_path(workspace_root: Path, ns: str, book: str) -> Path:
    """Resolve data/memory/{ns}/{book}.yaml with containment checks.

    ns 是相对 data/memory 的子目录（可含一级 @workspace 段）。为什么单独
    校验：ns 来自客户端查询参数，节点 id 与 workspace 名允许的点划线之外
    还可能有 @ 前缀，白名单过窄会挡住磁盘上真实存在的目录；resolve +
    前缀包含 + 深度上限即可挡住穿越。
    """
    if not _BOOK_NAME_RE.fullmatch(book or ""):
        raise _http_exc(400, "Invalid book name")
    ns = (ns or "").strip().strip("/")
    base = (workspace_root / "data" / "memory").resolve()
    if not ns:
        p = base / f"{book}.yaml"
    else:
        parts = ns.split("/")
        if len(parts) > 2 or any(seg in ("", ".", "..") for seg in parts):
            raise _http_exc(400, "Invalid namespace")
        p = base.joinpath(*parts) / f"{book}.yaml"
    rp = p.resolve()
    if rp != base and not str(rp).startswith(str(base) + "/"):
        raise _http_exc(400, "Invalid path")
    return rp


def _entry_summary(e: dict[str, Any]) -> dict[str, Any]:
    """条目管理摘要（不含正文全文）。"""
    return {
        "id": str(e.get("id") or ""),
        "content_preview": str(e.get("content") or "")[:120],
        "keywords": [str(k) for k in (e.get("keywords") or []) if isinstance(k, str)][:12],
        "constant": bool(e.get("constant", False)),
        "enabled": bool(e.get("enabled", True)),
        "priority": int(e.get("priority") or 0),
        "scan_depth": int(e.get("scan_depth") or 0),
        "node_ids": [str(n) for n in (e.get("node_ids") or []) if isinstance(n, str)],
        "source": str(e.get("source") or ""),
        "created_at": str(e.get("created_at") or ""),
        "updated_at": str(e.get("updated_at") or ""),
    }


# [AutoC 2026-08-28] 管理列表的按文件缓存：path -> (mtime, size, listing)。
# Why: 生产 260 个 yaml / 2.8MB，每次请求全量解析 ~3.3s，lazy 只裁剪了
# 响应体积没减少解析量，点击 book 也触发全树扫描。How: stat 命中
# (mtime, size) 直接复用解析结果；文件写入（含 PUT/DELETE 端点）必然
# 改变 mtime，无需显式失效。Purpose: 重复扫描毫秒级返回。
_book_scan_cache: dict[Path, tuple[float, int, dict[str, Any]]] = {}

# [2026-09-04] 搜索用的整本缓存：列表缓存只存摘要（120 字预览），内容搜索
# 需要完整正文。同 (mtime, size) 失效策略。
_book_data_cache: dict[Path, tuple[float, int, dict[str, Any]]] = {}


def _book_full_data(yf: Path) -> dict[str, Any]:
    """整本 book 数据（含完整正文），带 mtime+size 缓存，供全库搜索使用。"""
    try:
        st = yf.stat()
    except OSError:
        return {"book": yf.stem, "entries": []}
    hit = _book_data_cache.get(yf)
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    data = _load_book(yf)
    _book_data_cache[yf] = (st.st_mtime, st.st_size, data)
    return data


def _memory_search(base: Path, needle: str) -> dict[str, Any]:
    """全库搜索：id / 内容子串 + 关键词命中。

    返回与列表接口相同的树形结构，只含命中条目；每条附加
    matches（命中类型列表）与 kw_hits（命中的关键词原文），
    前端据此做关键词聚合分组。
    """

    def _scan_matches(ns_dir: Path) -> list[dict[str, Any]]:
        books: list[dict[str, Any]] = []
        for yf in sorted(ns_dir.glob("*.yaml")):
            data = _book_full_data(yf)
            hits: list[dict[str, Any]] = []
            for e in data.get("entries", []):
                if not isinstance(e, dict):
                    continue
                eid = str(e.get("id") or "")
                content = str(e.get("content") or "")
                kws = [str(k) for k in (e.get("keywords") or []) if isinstance(k, str)]
                matched: list[str] = []
                if needle in eid.lower():
                    matched.append("id")
                if needle in content.lower():
                    matched.append("content")
                kw_hits = [k for k in kws if needle in k.lower()]
                if kw_hits:
                    matched.append("keyword")
                if not matched:
                    continue
                s = _entry_summary(e)
                s["matches"] = matched
                s["kw_hits"] = kw_hits
                hits.append(s)
            if hits:
                books.append({
                    "book": str(data.get("book") or yf.stem),
                    "file": yf.name,
                    "count": len(hits),
                    "enabled_count": sum(1 for h in hits if h["enabled"]),
                    "entries": hits,
                })
        return books

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    root = _scan_matches(base)
    if root:
        groups.append(("", root))
    for d in sorted(x for x in base.iterdir() if x.is_dir() and not x.name.startswith(".")):
        nb = _scan_matches(d)
        if nb:
            groups.append((d.name, nb))
        for sub in sorted(x for x in d.iterdir() if x.is_dir()):
            sb = _scan_matches(sub)
            if sb:
                groups.append((f"{d.name}/{sub.name}", sb))
    total = sum(b["count"] for _, books in groups for b in books)
    return {
        "total": total,
        "query": needle,
        "namespaces": [
            {"ns": ns, "total": sum(b["count"] for b in books), "books": books}
            for ns, books in groups
        ],
    }



def _book_listing(yf: Path) -> dict[str, Any]:
    """单个 book 的列表数据（名称/计数/条目摘要），带 mtime+size 缓存。"""
    try:
        st = yf.stat()
    except OSError:
        return {"book": yf.stem, "file": yf.name, "count": 0, "enabled_count": 0, "entries": []}
    hit = _book_scan_cache.get(yf)
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    data = _load_book(yf)
    entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
    listing = {
        "book": str(data.get("book") or yf.stem),
        "file": yf.name,
        "count": len(entries),
        "enabled_count": sum(1 for e in entries if bool(e.get("enabled", True))),
        "entries": [_entry_summary(e) for e in entries],
    }
    _book_scan_cache[yf] = (st.st_mtime, st.st_size, listing)
    return listing


def _memory_list_endpoint(request: Any) -> list[dict[str, Any]]:
    """列出 namespace/book 及条目摘要，供管理面板拉取。

    [AutoC 2026-08-28] 两级性能路径：
    - 解析层：_book_listing 按 (mtime, size) 缓存每个 yaml 的解析结果，
      重复扫描不再重复解析（此前 260 文件全量解析每次 ~3.3s）。
    - 请求层：?lazy=1 只返回计数；ns+book 同给时走直达路径——只扫描
      目标 ns 目录，且响应里只有该 ns/book（前端只取匹配项，整树数据
      无意义）。无参数形态与旧版完全一致。
    """
    q = getattr(request, "query_params", None) or {}

    def _q(name: str) -> str:
        try:
            v = q.get(name)
        except Exception:
            return ""
        return str(v) if v is not None else ""

    lazy = _q("lazy") == "1"
    want_ns = _q("ns")
    want_book = _q("book")
    ws = _ws_root(request)
    base = ws / "data" / "memory"
    if not base.is_dir():
        return []

    # [2026-09-04] 搜索分支：?q= 触发全库扫描（解析层有按文件缓存），
    # 与 lazy/直达路径互斥，优先级最高。
    needle = _q("q").strip().lower()
    if needle:
        return _memory_search(base, needle)

    base_resolved = base.resolve()

    def _scan(ns_dir: Path, ns: str) -> list[dict[str, Any]]:
        books = []
        for yf in sorted(ns_dir.glob("*.yaml")):
            b = _book_listing(yf)
            include = (not lazy) or (ns == want_ns and b["book"] == want_book)
            books.append(b if include else {**b, "entries": []})
        return books

    # 直达路径：ns+book 命中时只扫目标 ns 目录，响应只含该 ns
    if lazy and want_ns and want_book:
        ns_dir = (base / want_ns) if want_ns else base
        try:
            resolved = ns_dir.resolve()
        except OSError:
            resolved = ns_dir
        if resolved != base_resolved and base_resolved not in resolved.parents:
            return {"total": 0, "namespaces": []}
        books = _scan(ns_dir, want_ns) if ns_dir.is_dir() else []
        for b in books:
            if b["book"] == want_book:
                return {
                    "total": b["count"],
                    "namespaces": [{"ns": want_ns, "total": b["count"], "books": [b]}],
                }
        return {"total": 0, "namespaces": []}

    # 根级 books（ns=""）
    root_books = _scan(base, "")
    # 一级目录：节点 ns 与（历史遗留的）根级 @ws 目录，一律作为 ns 展开
    level1 = sorted(d for d in base.iterdir() if d.is_dir() and not d.name.startswith("."))
    # 二级目录：仅 @workspace 形态
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    if root_books:
        groups.append(("", root_books))
    for d in level1:
        ns_books = _scan(d, d.name)
        if ns_books:
            groups.append((d.name, ns_books))
        for sub in sorted(x for x in d.iterdir() if x.is_dir()):
            sub_books = _scan(sub, f"{d.name}/{sub.name}")
            if sub_books:
                groups.append((f"{d.name}/{sub.name}", sub_books))

    total = sum(b["count"] for _, books in groups for b in books)
    return {
        "total": total,
        "namespaces": [
            {
                "ns": ns,
                "total": sum(b["count"] for b in books),
                "books": books,
            }
            for ns, books in groups
        ],
    }


def _memory_raw_get_endpoint(ns: str, book: str, request: Any) -> dict[str, str]:
    p = _memory_book_path(_ws_root(request), ns, book)
    if not p.exists():
        raise _http_exc(404, "Book not found")
    return {"content": p.read_text(encoding="utf-8")}


def _memory_raw_put_endpoint(ns: str, book: str, payload: Any, request: Any) -> dict[str, Any]:
    ws = _ws_root(request)
    p = _memory_book_path(ws, ns, book)
    try:
        data = yaml.safe_load(str(payload.content))
    except Exception as exc:
        raise _http_exc(400, f"YAML parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise _http_exc(400, "Book yaml must be a mapping")
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    data.setdefault("book", book)
    p.parent.mkdir(parents=True, exist_ok=True)
    _save_book(p, data)
    _invalidate_cache(ws, memory_book=(ns or "").strip("/"))
    return {"ok": True}


def _memory_delete_entry_endpoint(ns: str, book: str, id: str, request: Any) -> dict[str, Any]:
    ws = _ws_root(request)
    p = _memory_book_path(ws, ns, book)
    if not p.exists():
        raise _http_exc(404, "Book not found")
    data = _load_book(p)
    entries = data.get("entries", [])
    kept = [e for e in entries if not (isinstance(e, dict) and str(e.get("id") or "").strip() == id)]
    if len(kept) == len(entries):
        raise _http_exc(404, f"Memory not found: {id}")
    if kept:
        data["entries"] = kept
        _save_book(p, data)
    else:
        # 空书随最后一条删除一并移除，与工具侧行为一致
        try:
            p.unlink()
        except Exception:
            pass
    _invalidate_cache(ws, memory_book=(ns or "").strip("/"))
    return {"ok": True, "deleted": id}


# [AutoC 2026-08-28] 路由函数定义在模块层：PEP 563 字符串注解需要在模块
# globals 解析 Request / RawContent / CreatePayload。旧形态在函数体内定义
# 包装，注解解析失败导致 422。
def _skills_list_route(request: Request) -> list[dict[str, Any]]:
    return _list_skills_endpoint(request)


def _skills_raw_route(name: str, request: Request) -> dict[str, str]:
    return _get_skill_raw_endpoint(name, request)


def _skills_update_route(name: str, request: Request, payload: RawContent) -> dict[str, Any]:
    return _update_skill_raw_endpoint(name, request, payload)


def _skills_create_route(request: Request, payload: CreatePayload) -> dict[str, Any]:
    return _create_skill_endpoint(request, payload)


def _skills_delete_route(name: str, request: Request) -> dict[str, Any]:
    return _delete_skill_endpoint(name, request)


def _memory_list_route(request: Request) -> Any:
    return _memory_list_endpoint(request)


def _memory_raw_route(ns: str = "", book: str = "", request: Request = None) -> dict[str, str]:  # noqa: RUF013
    return _memory_raw_get_endpoint(ns, book, request)


def _memory_raw_put_route(ns: str = "", book: str = "", request: Request = None, payload: RawContent = None) -> dict[str, Any]:  # noqa: RUF013
    return _memory_raw_put_endpoint(ns, book, payload, request)


def _memory_delete_route(ns: str = "", book: str = "", id: str = "", request: Request = None) -> dict[str, Any]:  # noqa: RUF013
    return _memory_delete_entry_endpoint(ns, book, id, request)


def _register_admin_routes(routes: Any) -> None:
    """组装 admin/config 端点与设置面板静态资源（纯注册，无注解）。"""
    from engine.faces.routes import static_router

    router = APIRouter()
    router.add_api_route("/skills", _skills_list_route, methods=["GET"])
    router.add_api_route("/skills/{name}/raw", _skills_raw_route, methods=["GET"])
    router.add_api_route("/skills/{name}/raw", _skills_update_route, methods=["PUT"])
    router.add_api_route("/skills", _skills_create_route, methods=["POST"])
    router.add_api_route("/skills/{name}", _skills_delete_route, methods=["DELETE"])
    router.add_api_route("/skill_pins", _skill_pins_get_route, methods=["GET"])
    router.add_api_route("/skill_pins", _skill_pins_put_route, methods=["PUT"])
    routes.register(router, mount="admin/config", description="skills 配置端点")

    mem_router = APIRouter()
    mem_router.add_api_route("/memory", _memory_list_route, methods=["GET"])
    mem_router.add_api_route("/memory/raw", _memory_raw_route, methods=["GET"])
    mem_router.add_api_route("/memory/raw", _memory_raw_put_route, methods=["PUT"])
    mem_router.add_api_route("/memory/entry", _memory_delete_route, methods=["DELETE"])
    routes.register(mem_router, mount="admin/config", description="memory 配置端点")

    # 设置面板静态资源。public：iframe 无法携带 Authorization 头，页面本身
    # 不含秘密，数据全部经鉴权 XHR 获取。
    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "web"),
        prefix="/web",
    )
    routes.register(client, description="knowledge_inject 设置面板静态资源（短期面板认证）")


class KnowledgeInjector:
    """Declarative skill and memory prompt sections.

    Why: knowledge injection no longer rebuilds the whole prompt inside a
    before_prompt_build hook; it declares content and the assembler places it.
    How: __init__ receives the EngineContext and registers one static and one
    dynamic section on the prompt_sections face; both renders share one cached
    pipeline run per turn. Purpose: the plugin declares what to contribute, the
    core owns where it lands.
    """

    name = "knowledge_inject"
    priority = 50

    def __init__(self, ctx: Any = None) -> None:
        sections = ctx.contributions.get("prompt_sections") if ctx is not None else None
        if sections is not None:
            # Why: order 50 keeps knowledge content ahead of later sections. How:
            # static scope lands before history (cache-friendly), dynamic before
            # the instruction (per-turn). Purpose: preserve the historical prompt
            # layout after moving off the rebuild hook.
            sections.register_section("knowledge_static", self.render_static, order=50, scope="static")
            sections.register_section("knowledge_dynamic", self.render_dynamic, order=50, scope="dynamic")
        # supervisor 进程：挂 skills admin 端点与设置面板静态资源。
        routes = ctx.contributions.get("routes") if ctx is not None else None
        if routes is not None:
            _register_admin_routes(routes)
        if sections is None and routes is None:
            logger.warning("knowledge_inject: neither prompt_sections nor routes face mounted; plugin inactive")

    def _compute(self, sctx: Any) -> tuple | None:
        """Share one pipeline result between renders of the same prompt context."""
        if sctx.node is None:
            return None
        active_injected = collect_active_injected_hashes(sctx.history)
        instruction = sctx.instruction
        inbound = sctx.extra.get("inbound_message")
        if isinstance(inbound, dict) and not conversational_history([inbound]):
            instruction = ""
        key = (instruction, len(sctx.history or ()), tuple(sorted(active_injected.items())))
        cached = sctx.extra.get("knowledge_result")
        if cached is not None and cached[0] == key:
            return cached[1]
        result = build_knowledge_context(
            sctx.workspace_root,
            sctx.node,
            instruction,
            conversational_history(sctx.history),
            sctx.runtime_cfg,
            task_context={**(sctx.task_context or {}), "workspace_name": sctx.workspace_name},
            active_injected=active_injected,
        )
        sctx.extra["knowledge_result"] = (key, result)
        return result

    def render_static(self, sctx: Any) -> list[dict[str, Any]] | None:
        """Contribute skill/memory constant blocks as separate system messages."""
        result = self._compute(sctx)
        if result is None:
            return None
        skill_static, _skill_dynamic, memory_static, _memory_dynamic = result
        msgs = [m for m in (*skill_static, *memory_static) if m.get("content")]
        return msgs or None

    def render_dynamic(self, sctx: Any) -> list[dict[str, Any]] | None:
        """Contribute skill/memory active blocks with their per-entry ``_injected_hashes``."""
        result = self._compute(sctx)
        if result is None:
            return None
        _skill_static, skill_dynamic, _memory_static, memory_dynamic = result
        items = [dict(m) for m in (*skill_dynamic, *memory_dynamic) if m.get("content")]
        return items or None

    # [AutoC 2026-08-28] Dream memory router hook delegate.
    # Registered as ("before_tool_call", "_dream_route") in PLUGIN_META.
    # Delegates to the standalone DreamMemoryRouter handler.
    _dream_router_instance: Any = None

    async def _dream_route(self, ctx: Any) -> Any:
        """Redirect dream node save_memory/delete_memory to source namespace."""
        if self._dream_router_instance is None:
            from engine.builtin.knowledge_inject.dream_router import DreamMemoryRouter
            self._dream_router_instance = DreamMemoryRouter()
        return await self._dream_router_instance.handle(ctx)
