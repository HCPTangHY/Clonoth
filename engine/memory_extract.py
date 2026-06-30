"""Silent background memory extraction.

After the main agent finishes a turn, this module fires a single
independent LLM call to analyze the conversation and decide whether
anything is worth saving to persistent memory.

The main agent has zero awareness of this mechanism — no tools, no
prompt hints, no nodes.  It is a pure engine-level side effect.

Design:
  - Only runs for the entry node (shell-facing), not sub-node dispatches.
  - Only runs on successful finish (not fail/cancel/dispatch).
  - Fire-and-forget via asyncio.create_task — does not block response.
  - Uses structured JSON output; parses and writes memory files directly.
  - Errors are silently swallowed (best-effort).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are a silent memory extraction agent. You analyze conversation transcripts
and decide whether anything is worth saving to persistent memory.

You are NOT part of the conversation. The user and the assistant do not know
you exist. Never output conversational text — only structured JSON.

Memory is for facts that will be useful in FUTURE conversations:
- User corrections ("don't do X", "stop doing Y", "use Z instead")
- User confirmations of non-obvious approaches ("yes exactly", "perfect")
- User identity / preferences / role
- Project context not derivable from code (deadlines, decisions, people)
- External resource pointers (URLs, dashboard names, Linear projects)
- Character profiles in group chat (who is who, their role, preferences)

Do NOT save:
- Anything derivable from code, git history, or file structure
- Ephemeral task details or current conversation state
- Debug solutions (the fix is in the code)
- Content that is too vague to be useful later

Output a JSON object with this schema:

If nothing is worth saving, output exactly:
{"skip": true}

Otherwise output:
{
  "memories": [
    {
      "action": "save",
      "id": "short_snake_case_id",
      "book": "default",
      "content": "concise memory text",
      "keywords": ["keyword1", "keyword2"],
      "constant": false
    }
  ]
}

Rules:
- Most conversations are routine and produce NO memories. Default to {"skip": true}.
  Only save when you see clear, durable, non-obvious information.
- "id" must be unique, descriptive, snake_case, max 128 chars.
- "book" groups related memories: use "people" for character profiles,
  "rules" for behavioral corrections, "project" for project context,
  "default" for everything else.
- "keywords" are activation triggers — when any keyword appears in future
  user messages, this memory gets injected into context. Use names, terms,
  or /regex/i patterns.
- "constant": true means always injected (use sparingly — only for
  universal rules like language preference).
- "content" should be concise but complete. Include WHY when known.
- If a memory with the same id likely exists, use the same id to update it.
- Max 3 memories per extraction. Quality over quantity.
"""

_EXTRACT_USER_TEMPLATE = """\
Analyze the following conversation transcript. Extract any memories worth
saving for future conversations. Output JSON only.

<transcript>
{transcript}
</transcript>"""


# ---------------------------------------------------------------------------
#  Transcript formatting
# ---------------------------------------------------------------------------

def _format_transcript(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 12000,
) -> str:
    """Format recent non-system messages into a readable transcript."""
    parts: list[str] = []
    total = 0
    # Walk backwards to get the most recent messages first
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role == "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = str(content)
        # Truncate individual messages
        if len(content) > 2000:
            content = content[:2000] + "...<truncated>"
        line = f"[{role}]\n{content}"
        total += len(line)
        if total > max_chars:
            break
        parts.append(line)
    parts.reverse()
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
#  Parse extraction result
# ---------------------------------------------------------------------------

def _parse_extraction(text: str) -> list[dict[str, Any]]:
    """Parse LLM output into a list of memory operations."""
    text = text.strip()
    # Try to extract JSON from markdown code block
    m = re.search(r'```(?:json)?\s*\n?(\{[\s\S]*?\})\s*```', text)
    if m:
        text = m.group(1)
    # Try direct JSON parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        m2 = re.search(r'(\{[\s\S]*\})', text)
        if not m2:
            return []
        try:
            data = json.loads(m2.group(1))
        except json.JSONDecodeError:
            return []

    if not isinstance(data, dict):
        return []
    # Explicit skip
    if data.get("skip"):
        return []

    memories = data.get("memories", [])
    if not isinstance(memories, list):
        return []
    return [m for m in memories if isinstance(m, dict) and m.get("id") and m.get("content")]


# ---------------------------------------------------------------------------
#  Write memories to disk
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_BOOK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _memory_dir(workspace_root: Path) -> Path:
    return workspace_root / "data" / "memory"


def _apply_memories(
    workspace_root: Path,
    operations: list[dict[str, Any]],
) -> int:
    """Write memory operations to disk. Returns count of entries saved."""
    saved = 0
    for op in operations:
        action = str(op.get("action", "save")).strip()
        if action not in ("save", "delete"):
            continue

        mid = str(op.get("id", "")).strip()
        if not mid or not _ID_RE.fullmatch(mid):
            continue

        book = str(op.get("book", "default")).strip() or "default"
        if not _BOOK_RE.fullmatch(book):
            book = "default"

        mem_dir = _memory_dir(workspace_root)
        book_path = mem_dir / f"{book}.yaml"

        # Load existing book
        data: dict[str, Any] = {"book": book, "entries": []}
        if book_path.exists():
            try:
                raw = yaml.safe_load(book_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
                    if not isinstance(data.get("entries"), list):
                        data["entries"] = []
            except Exception:
                pass
        data.setdefault("book", book)

        entries: list[dict[str, Any]] = data["entries"]

        if action == "delete":
            new_entries = [
                e for e in entries
                if not (isinstance(e, dict) and str(e.get("id", "")).strip() == mid)
            ]
            if len(new_entries) < len(entries):
                data["entries"] = new_entries
                _write_book(book_path, data)
                saved += 1
            continue

        # action == save
        content = str(op.get("content", "")).strip()
        if not content:
            continue

        raw_kw = op.get("keywords")
        keywords: list[str] = []
        if isinstance(raw_kw, list):
            keywords = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]

        new_entry: dict[str, Any] = {
            "id": mid,
            "content": content,
            "keywords": keywords,
            "constant": bool(op.get("constant", False)),
            "enabled": True,
            "priority": 0,
            "scan_depth": 0,
            "source": "auto",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_hit_at": "",
        }

        # Upsert
        found = False
        for i, e in enumerate(entries):
            if isinstance(e, dict) and str(e.get("id", "")).strip() == mid:
                entries[i] = new_entry
                found = True
                break
        if not found:
            entries.append(new_entry)

        _write_book(book_path, data)
        saved += 1

    # Invalidate engine cache
    try:
        # Why: the memory runtime cache moved into the knowledge plugin. How:
        # import _MemoryCache from engine.builtin.knowledge_inject instead of the
        # deleted memory runtime module. Purpose: extracted memories are visible to
        # the next prompt build without reviving the old module boundary.
        from engine.builtin.knowledge_inject import _MemoryCache
        _MemoryCache.invalidate(workspace_root)
    except Exception:
        pass

    return saved


def _write_book(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
#  Public entry point
# ---------------------------------------------------------------------------

# 按 workspace_root 记录上次提取时的非 system 消息数，避免全局变量在多会话下污染。
_last_extract_counts: dict[str, int] = {}

# 保存后台提取 task 的强引用，防止被 GC 回收。
_background_tasks: set[asyncio.Task] = set()


async def maybe_extract_memories(
    *,
    workspace_root: Path,
    # [provider-registry 2026-05-03] 记忆提取只依赖 BaseProvider.chat。
    # 原因：这里没有 OpenAI 专用逻辑；做法：类型改为 BaseProvider；目的：避免硬编码具体 provider。
    provider: BaseProvider,
    messages: list[dict[str, Any]],
) -> None:
    """Fire-and-forget memory extraction after a successful main-agent turn.

    Called from runner._run_node_task.  Runs as an asyncio.Task so it
    does not block the response to supervisor.

    Gated by runtime config ``memory.auto_extract.enabled``.
    """
    runtime_cfg = load_runtime_config(workspace_root)
    if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
        return

    _ws_key = str(workspace_root)

    non_system = [m for m in messages if m.get("role") != "system"]
    current_count = len(non_system)

    # Gate 1: absolute minimum
    min_messages = get_int(
        runtime_cfg, "memory.auto_extract.min_messages", 4, min_value=2, max_value=100,
    )
    if current_count < min_messages:
        return

    # Gate 2: enough new messages since last extraction
    min_increment = get_int(
        runtime_cfg, "memory.auto_extract.min_increment", 10, min_value=1, max_value=100,
    )
    if current_count - _last_extract_counts.get(_ws_key, 0) < min_increment:
        return

    transcript = _format_transcript(messages)
    if not transcript.strip():
        return

    # Advance cursor before firing — even if extraction fails, don't retry
    # the same range.  Next accumulation will trigger a fresh attempt.
    _last_extract_counts[_ws_key] = current_count
    task = asyncio.create_task(_run_extraction(workspace_root, provider, transcript))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_extraction(
    workspace_root: Path,
    # [provider-registry 2026-05-03] 后台提取调用只需要通用 chat 接口。
    # 原因：provider 可能来自 registry 的任意实现；做法：类型改为 BaseProvider；目的：保留插件化兼容性。
    provider: BaseProvider,
    transcript: str,
) -> None:
    """The actual extraction LLM call + disk write. Runs in background."""
    try:
        user_content = _EXTRACT_USER_TEMPLATE.format(transcript=transcript)
        resp = await provider.chat(
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            tools=None,
        )
        if not resp.ok or not resp.text:
            return

        operations = _parse_extraction(resp.text)
        if not operations:
            return

        # Cap at 3 per extraction
        operations = operations[:3]
        count = _apply_memories(workspace_root, operations)
        if count > 0:
            logger.info("memory_extract: saved %d entries", count)
    except Exception as e:
        # Best-effort — never crash the engine
        logger.debug("memory_extract: %s", e)
