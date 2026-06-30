"""search_in_files — search (and replace) in workspace files.

Supports:
- Substring and regex search with line/column/context output
- Search-and-replace mode with approval guard
- File glob pattern filtering
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int, load_runtime_config

from ..context import ToolContext
from .._common import resolve_and_classify, guard_external_read, request_guard


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    # Data dirs that can contain huge JSONL / build artifacts
    "conversations", "node_contexts", "artifacts", "logs",
    # Common build output dirs
    "dist", "build", ".next", ".nuxt", ".output", ".turbo",
    ".cache", ".parcel-cache", "coverage",
})
_SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".whl", ".egg",
    ".jsonl", ".log",
})

# Hard limit on total chars returned in search results to prevent context explosion
_MAX_TOTAL_RESULT_CHARS = 50_000
_MAX_CONTEXT_LINE_CHARS = 500


def _match_pattern(rel_path: str, pattern: str) -> bool:
    """Check if *rel_path* matches a glob *pattern*."""
    if pattern in ("**/*", "*"):
        return True
    filename = rel_path.rsplit("/", 1)[-1]
    file_pattern = pattern.rsplit("/", 1)[-1] if "/" in pattern else pattern
    return fnmatch(filename, file_pattern)


def _get_context(lines: list[str], line_idx: int, n: int = 1) -> str:
    """Return ±n lines of context around *line_idx*, with line-number prefixes.

    Each line is capped at _MAX_CONTEXT_LINE_CHARS to prevent single-line
    files (e.g. JSONL) from blowing up the result.
    """
    start = max(0, line_idx - n)
    end = min(len(lines), line_idx + n + 1)
    parts = []
    for i in range(start, end):
        line = lines[i]
        if len(line) > _MAX_CONTEXT_LINE_CHARS:
            line = line[:_MAX_CONTEXT_LINE_CHARS] + "...<truncated>"
        parts.append(f"{i + 1}: {line}")
    return "\n".join(parts)


def _line_col(text: str, pos: int) -> tuple[int, int]:
    """Return 1-based (line, column) for byte offset *pos* in *text*."""
    line = text.count("\n", 0, pos) + 1
    last_nl = text.rfind("\n", 0, pos)
    col = pos - last_nl  # works even when last_nl == -1 → gives pos+1
    return line, col


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: search and replace failures need the same readable
    # data.result field as successful results. How: normalize the error once and
    # copy optional metadata such as path or cancelled into data. Purpose: keep tool
    # history readable while callers migrate to the unified ok/data/error contract.
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    data.update(extra)
    response: dict[str, Any] = {"ok": False, "success": False, "error": text, "data": data}
    response.update(extra)
    return response


def _search_result_text(matches: list[dict[str, Any]], truncated: bool) -> str:
    # [AutoC 2026-05-31] Why: data.result is now the canonical text consumed by the
    # engine. How: render the same location, match, and context transcript that the
    # legacy formatter generated from data.results. Purpose: keep search output
    # readable without relying on formatter fallback.
    lines = [f"{len(matches)} results found{' (truncated)' if truncated else ''}:"]
    for m in matches:
        lines.append(f"\n{m['file']}:{m['line']} | {m['match']}")
        if m.get("context"):
            for cl in str(m["context"]).splitlines():
                lines.append(f"  {cl}")
    return "\n".join(lines)


def _replace_result_text(results: list[dict[str, Any]], files_modified: int, total_replacements: int) -> str:
    # [AutoC 2026-05-31] Why: replace mode writes files and needs a concise readable
    # summary under data.result. How: include aggregate counts and per-file counts.
    # Purpose: make replacement effects visible while preserving structured fields.
    lines = [f"{files_modified} files modified, {total_replacements} replacements"]
    for entry in results:
        file_path = entry.get("file")
        replacements = entry.get("replacements")
        if isinstance(file_path, str) and isinstance(replacements, int):
            lines.append(f"{file_path}: {replacements} replacements")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  File collector
# ---------------------------------------------------------------------------

def _collect_files(root: Path, pattern: str, max_file_size: int, workspace: Path):
    """Yield (Path, rel_posix) for matching files under *root*.

    For paths under *workspace*, rel_posix is workspace-relative.
    For paths under extra_roots (outside workspace), rel_posix is the absolute path.
    """
    def _rel(p: Path) -> str:
        try:
            return p.relative_to(workspace).as_posix()
        except ValueError:
            return p.as_posix()

    if root.is_file():
        rel = _rel(root)
        try:
            if root.stat().st_size <= max_file_size:
                yield root, rel
        except Exception:
            pass
        return

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _SKIP_SUFFIXES:
            continue
        try:
            if p.stat().st_size > max_file_size:
                continue
        except Exception:
            continue
        rel = _rel(p)
        if not _match_pattern(rel, pattern):
            continue
        yield p, rel


# ---------------------------------------------------------------------------
#  Search mode
# ---------------------------------------------------------------------------

async def _do_search(
    root: Path, regex: re.Pattern, pattern: str,
    max_results: int, max_file_size: int, ctx: ToolContext,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    truncated = False
    total_chars = 0

    for p, rel in _collect_files(root, pattern, max_file_size, ctx.workspace_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        lines = text.split("\n")

        for m in regex.finditer(text):
            line, col = _line_col(text, m.start())
            context = _get_context(lines, line - 1, n=1)
            match_text = m.group()
            if len(match_text) > _MAX_CONTEXT_LINE_CHARS:
                match_text = match_text[:_MAX_CONTEXT_LINE_CHARS] + "...<truncated>"

            entry_chars = len(context) + len(match_text) + len(rel) + 50
            total_chars += entry_chars

            matches.append({
                "file": rel,
                "line": line,
                "column": col,
                "match": match_text,
                "context": context,
            })

            if len(matches) >= max_results or total_chars >= _MAX_TOTAL_RESULT_CHARS:
                truncated = True
                break

        if truncated:
            break

    # [AutoC 2026-05-31] Why: callers should no longer need formatter fallback to
    # see readable search output. How: store the rendered transcript in data.result
    # and keep matches/count/truncated as structured fields. Purpose: conform to the
    # unified ok/data/error tool response shape.
    return {
        "ok": True,
        "success": True,
        "data": {
            "result": _search_result_text(matches, truncated),
            "results": matches,
            "count": len(matches),
            "truncated": truncated,
        },
    }


# ---------------------------------------------------------------------------
#  Replace mode
# ---------------------------------------------------------------------------

async def _do_replace(
    root: Path, regex: re.Pattern, query: str, replace_str: str,
    pattern: str, max_files: int, max_file_size: int, ctx: ToolContext,
) -> dict[str, Any]:
    # Approval guard — replace is a write operation
    _op, err = await request_guard(
        ctx, "write_file",
        {"path": "(search_replace)", "reason": f"replace '{query}' in up to {max_files} files"},
    )
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    all_matches: list[dict[str, Any]] = []
    files_modified: list[str] = []
    files_processed = 0

    for p, rel in _collect_files(root, pattern, max_file_size, ctx.workspace_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        file_matches: list[dict[str, Any]] = []
        for m in regex.finditer(text):
            line, col = _line_col(text, m.start())
            file_matches.append({
                "file": rel,
                "line": line,
                "column": col,
                "match": m.group(),
            })

        if not file_matches:
            continue

        new_text = regex.sub(replace_str, text)
        try:
            p.write_text(new_text, encoding="utf-8")
        except Exception:
            continue

        all_matches.extend(file_matches)
        files_modified.append(rel)
        files_processed += 1

        if files_processed >= max_files:
            break

    replace_results = [
        {
            "file": f,
            "replacements": sum(1 for m in all_matches if m["file"] == f),
        }
        for f in files_modified
    ]
    # [AutoC 2026-05-31] Why: replace mode now returns a unified success payload.
    # How: put human-readable replacement counts in data.result and preserve all
    # structured replacement details below data. Purpose: keep both model history
    # and downstream programmatic consumers useful.
    return {
        "ok": True,
        "success": True,
        "cancelled": False,
        "data": {
            "result": _replace_result_text(replace_results, len(files_modified), len(all_matches)),
            "matches": all_matches,
            "results": replace_results,
            "filesModified": len(files_modified),
            "totalReplacements": len(all_matches),
        },
    }


# ---------------------------------------------------------------------------
#  Main entry
# ---------------------------------------------------------------------------

async def search_in_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query", ""))
    mode = str(args.get("mode", "search")).lower()
    rel_path = str(args.get("path", "."))
    pattern = str(args.get("pattern", "**/*"))
    is_regex = bool(args.get("isRegex", False))
    max_results = int(args.get("maxResults") or args.get("max_results") or 100)
    replace_str = str(args.get("replace", ""))
    max_files = int(args.get("maxFiles") or args.get("max_files") or 50)

    if not query:
        return _error_response("empty query")

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    max_file_size_bytes = get_int(
        runtime_cfg,
        "meta.search.max_file_size_bytes",
        2_000_000,
        min_value=100_000,
        max_value=50_000_000,
    )

    try:
        root, is_ext = resolve_and_classify(ctx.workspace_root, rel_path)
    except ValueError as exc:
        return _error_response(str(exc))
    if not root.exists():
        return _error_response("path not found", path=rel_path)

    # ---- External path approval guard ----
    err = await guard_external_read(ctx, is_ext, rel_path, "grep")
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    # Compile regex — search: case-insensitive (gim); replace: case-sensitive (g)
    flags = re.MULTILINE
    if mode != "replace":
        flags |= re.IGNORECASE

    try:
        if is_regex:
            regex = re.compile(query, flags)
        else:
            regex = re.compile(re.escape(query), flags)
    except re.error as e:
        return _error_response(f"invalid regex: {e}")

    if mode == "replace":
        return await _do_replace(
            root, regex, query, replace_str, pattern, max_files, max_file_size_bytes, ctx,
        )
    else:
        return await _do_search(
            root, regex, pattern, max_results, max_file_size_bytes, ctx,
        )
