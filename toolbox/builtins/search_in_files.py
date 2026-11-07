"""search_in_files — search (and replace) in workspace files.

Migrated from the Clonoth production tree (2026-10). Behaviour contract:

- ``output_mode``: ``content`` (line/column/context transcript), ``files_only``
  (matching paths) and ``count`` (matches per file). All three keep the structured
  fields under ``data`` and the rendered transcript under ``data.result``.
- Glob filtering understands ``*``, ``?``, ``[]``, ``**`` and ``**/``; a pattern
  without ``/`` matches the file name at any depth.
- Directory walks skip VCS/build/data noise, binary (NUL-prefixed) files and files
  above ``meta.search.max_file_size_bytes``. A path the caller *explicitly* names is
  never hidden by the directory/suffix filters.
- Replace mode: ``$1``..``$n`` become capture groups in regex mode, everything is
  literal otherwise (no backslash eating). Files are only rewritten when the bytes
  round-trip as strict UTF-8 with their original line endings, so a latin-1 or
  binary file is skipped instead of silently corrupted. A regex replacement
  template is validated with :mod:`re` before any file is read or written, and
  one file's write error is isolated so completed writes stay reported.
- Approvals are per file, never per search root: the walk only *collects* paths,
  then every candidate goes through ``request_guard("read_file", <resolved path>)``
  before a single byte of it is read, and replace adds a ``write_file`` decision
  after matching, before the write. Denied paths are neither read nor reported.
  Policy rules therefore land on the real file — ``.env`` deny, ``data/policy.yaml``
  deny, ``engine/**`` approval, external symlink targets — instead of on ``.``.
  Guards run in batches of ``_GUARD_BATCH`` so a search that already filled its
  result budget stops asking for decisions instead of walking the whole tree.
- Path collection, matching and writing all run in worker threads
  (``asyncio.to_thread``); no blocking filesystem call runs on the event loop.
"""
from __future__ import annotations

import asyncio
import os
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
    "mcp_servers", "temp",
    # Data dirs that can contain huge JSONL / build artifacts
    "conversations", "node_contexts", "artifacts", "logs",
    "attachments", "llm_error_snapshots", "deploy-backups",
    # Common build output dirs
    "dist", "build", ".next", ".nuxt", ".output", ".turbo",
    ".cache", ".parcel-cache", "coverage",
})
_SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".whl", ".egg",
    ".jsonl", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".ogg", ".flac",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".db", ".sqlite", ".sqlite3", ".safetensors", ".pt", ".pth", ".onnx",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".asar", ".map",
})

# Hard limit on total chars returned in search results to prevent context explosion
_MAX_TOTAL_RESULT_CHARS = 50_000
_MAX_CONTEXT_LINE_CHARS = 500
_BINARY_SNIFF_BYTES = 8 * 1024
# How many paths get a policy decision before the next scan step; bounding this
# keeps a result-truncated search from costing one policy round-trip per file.
_GUARD_BATCH = 32


def _as_str(value: Any, default: str = "") -> str:
    """Stringify a model-supplied arg without turning None into ``"None"``."""
    if value is None:
        return default
    return str(value)


def _as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    """Coerce a model-supplied count; garbage falls back to *default*."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _as_bool(value: Any) -> bool:
    """Coerce a model-supplied flag; ``"false"`` must not become True."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _glob_to_regex(pat: str) -> re.Pattern:
    """Compile a glob pattern (supporting **, *, ?, []) into a regex."""
    i, n = 0, len(pat)
    res: list[str] = ["^"]
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                if i + 2 < n and pat[i + 2] == "/":
                    res.append("(?:.+/)?")
                    i += 3
                    continue
                res.append(".*")
                i += 2
                continue
            res.append("[^/]*")
        elif c == "?":
            res.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in ("!", "^"):
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                res.append("\\[")
            else:
                stuff = pat[i + 1:j].replace("\\", "\\\\")
                if stuff[0] in ("!", "^"):
                    stuff = "^" + stuff[1:]
                res.append(f"[{stuff}]")
                i = j
        else:
            res.append(re.escape(c))
        i += 1
    res.append("$")
    return re.compile("".join(res))


def _match_pattern(rel_path: str, pattern: str, rel_to_root: str = "") -> bool:
    """Check if *rel_path* (or *rel_to_root*) matches a glob *pattern*."""
    pat = (pattern or "").strip()
    if not pat or pat in ("**/*", "*"):
        return True
    if pat.startswith("./"):
        pat = pat[2:]
    if "/" not in pat:
        filename = rel_path.rsplit("/", 1)[-1]
        return fnmatch(filename, pat)
    rx = _glob_to_regex(pat)
    if rx.match(rel_path):
        return True
    if rel_to_root and rx.match(rel_to_root):
        return True
    return False


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
    """Return 1-based (line, column) for character offset *pos* in *text*."""
    line = text.count("\n", 0, pos) + 1
    last_nl = text.rfind("\n", 0, pos)
    col = pos - last_nl  # works even when last_nl == -1 → gives pos+1
    return line, col


def _load_text(p: Path, *, strict: bool, allow_binary: bool) -> tuple[str | None, str]:
    """Read one file for scanning. Returns ``(text | None, skip_reason)``.

    Only ever called for paths that already passed the per-file read guard.

    ``skip_reason`` is ``"binary"`` (NUL in the 8 KB prefix), ``"undecodable"``
    (strict UTF-8 decode failed) or ``"unreadable"``.

    Replace mode passes ``strict=True`` and ``allow_binary=False``: rewriting a file
    that did not decode cleanly would silently drop bytes (``errors="ignore"``) or
    normalise its line endings. Search mode keeps the historical lenient decode so a
    handful of stray high bytes never hides an otherwise textual file.
    """
    if not allow_binary:
        try:
            with p.open("rb") as bf:
                if b"\x00" in bf.read(_BINARY_SNIFF_BYTES):
                    return None, "binary"
        except Exception:
            return None, "unreadable"
    try:
        # newline="" keeps CRLF intact for replace round-trips; None = universal
        # newlines for search, which only inspects logical lines.
        with p.open("r", encoding="utf-8", errors="strict" if strict else "ignore",
                    newline="" if strict else None) as f:
            return f.read(), ""
    except UnicodeDecodeError:
        return None, "undecodable"
    except Exception:
        return None, "unreadable"


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


def _files_only_result_text(files: list[str], truncated: bool) -> str:
    lines = [f"{len(files)} files found{' (truncated)' if truncated else ''}:"]
    lines.extend(files)
    return "\n".join(lines)


def _count_result_text(counts: list[dict[str, Any]], total_matches: int, truncated: bool) -> str:
    lines = [f"{total_matches} matches across {len(counts)} files{' (truncated)' if truncated else ''}:"]
    for c in counts:
        lines.append(f"{c['file']}: {c['count']}")
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
#  Path collection (read-only, no file contents)
# ---------------------------------------------------------------------------

def _collect_files(root: Path, pattern: str, max_file_size: int, workspace: Path):
    """Yield ``(resolved_path, rel_posix)`` for matching files under *root*.

    Nothing is read here beyond directory metadata. The yielded path is the
    *resolved* one (symlinks followed once, at collection time) so that the same
    value is used for the policy guard, for reading and for writing — a link can
    never be approved under one name and read/written under another.

    For targets under *workspace*, rel_posix is workspace-relative; anything else
    (extra_roots, external symlink targets) keeps its absolute posix path.

    An explicitly named file (``root.is_file()``) is always yielded regardless of
    the skip lists — only its size is checked — so naming a path never makes it
    unsearchable.
    """
    def _rel(p: Path) -> str:
        try:
            return p.relative_to(workspace).as_posix()
        except ValueError:
            return p.as_posix()

    if root.is_file():
        try:
            target = root.resolve()
        except Exception:
            return
        try:
            if target.stat().st_size <= max_file_size:
                yield target, _rel(target)
        except Exception:
            pass
        return

    try:
        _root_resolved = root.resolve()
        _ws_resolved = workspace.resolve()
        _skip_top_data = (_root_resolved == _ws_resolved)
    except Exception:
        _root_resolved = root
        _skip_top_data = False

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dp = Path(dirpath)
        if _skip_top_data and dp == root:
            # workspace-root scans skip the runtime data dir; an explicit
            # path="data/..." scan still works because root is not the workspace.
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _SKIP_DIRS and d != "data"
            )
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)

        for fname in sorted(filenames):
            p = dp / fname
            try:
                target = p.resolve()
            except Exception:
                continue
            if target.suffix in _SKIP_SUFFIXES:
                continue
            try:
                if not target.is_file() or target.stat().st_size > max_file_size:
                    continue
            except Exception:
                continue
            rel = _rel(target)
            try:
                rel_to_root = target.relative_to(_root_resolved).as_posix()
            except ValueError:
                rel_to_root = rel
            if not _match_pattern(rel, pattern, rel_to_root):
                continue
            yield target, rel


def _collect_paths(root: Path, pattern: str, max_file_size: int, workspace: Path) -> list[tuple[Path, str]]:
    """Materialise the candidate list. Runs in a worker thread; reads no content."""
    return list(_collect_files(root, pattern, max_file_size, workspace))


# ---------------------------------------------------------------------------
#  Per-file policy gate
# ---------------------------------------------------------------------------

async def _guard_paths(
    ctx: ToolContext,
    paths: list[tuple[Path, str]],
    *,
    reason: str,
) -> tuple[list[tuple[Path, str]], list[dict[str, Any]], bool]:
    """Ask for a ``read_file`` decision per candidate path.

    Returns ``(approved, blocked, cancelled)``. Only *approved* paths may be read;
    a path is never opened before its own decision comes back.
    """
    approved: list[tuple[Path, str]] = []
    blocked: list[dict[str, Any]] = []
    for target, rel in paths:
        _op, err = await request_guard(
            ctx, "read_file",
            {"path": str(target), "reason": f"{reason}: {rel}"},
        )
        if err is not None:
            if err.get("cancelled"):
                return approved, blocked, True
            blocked.append({"file": rel, "error": str(err.get("error", "denied"))})
            continue
        approved.append((target, rel))
    return approved, blocked, False


def _blocked_summary(blocked: list[dict[str, Any]]) -> str:
    shown = ", ".join(f"{b.get('file')} ({b.get('error')})" for b in blocked[:10])
    return f"{len(blocked)} file(s) skipped by policy: {shown}"


# ---------------------------------------------------------------------------
#  Search mode
# ---------------------------------------------------------------------------

def _new_search_state(output_mode: str) -> dict[str, Any]:
    return {
        "mode": output_mode,
        "matches": [],
        "files": [],
        "counts": [],
        "total_matches": 0,
        "total_chars": 0,
        "truncated": False,
    }


def _scan_batch(
    state: dict[str, Any], files: list[tuple[Path, str]], regex: re.Pattern,
    max_results: int, allow_binary: bool,
) -> None:
    """Scan one batch of already-approved files into *state* (worker thread)."""
    mode = state["mode"]
    for target, rel in files:
        if state["truncated"]:
            return
        text, _skip = _load_text(target, strict=False, allow_binary=allow_binary)
        if text is None:
            continue

        if mode == "files_only":
            if regex.search(text):
                state["files"].append(rel)
                if len(state["files"]) >= max_results:
                    state["truncated"] = True
            continue

        if mode == "count":
            c = sum(1 for _ in regex.finditer(text))
            if c:
                state["counts"].append({"file": rel, "count": c})
                state["total_matches"] += c
                if len(state["counts"]) >= max_results:
                    state["truncated"] = True
            continue

        lines = text.split("\n")
        for m in regex.finditer(text):
            line, col = _line_col(text, m.start())
            context = _get_context(lines, line - 1, n=1)
            match_text = m.group()
            if len(match_text) > _MAX_CONTEXT_LINE_CHARS:
                match_text = match_text[:_MAX_CONTEXT_LINE_CHARS] + "...<truncated>"

            state["total_chars"] += len(context) + len(match_text) + len(rel) + 50
            state["matches"].append({
                "file": rel,
                "line": line,
                "column": col,
                "match": match_text,
                "context": context,
            })

            if (len(state["matches"]) >= max_results
                    or state["total_chars"] >= _MAX_TOTAL_RESULT_CHARS):
                state["truncated"] = True
                break


def _format_search(state: dict[str, Any]) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: callers should no longer need formatter fallback to
    # see readable search output. How: store the rendered transcript in data.result
    # and keep matches/count/truncated as structured fields. Purpose: conform to the
    # unified ok/data/error tool response shape.
    truncated = bool(state["truncated"])
    if state["mode"] == "files_only":
        files = state["files"]
        return {
            "result": _files_only_result_text(files, truncated),
            "files": files,
            "results": files,
            "count": len(files),
            "truncated": truncated,
        }
    if state["mode"] == "count":
        counts = state["counts"]
        return {
            "result": _count_result_text(counts, state["total_matches"], truncated),
            "counts": counts,
            "results": counts,
            "count": state["total_matches"],
            "filesCount": len(counts),
            "truncated": truncated,
        }
    matches = state["matches"]
    return {
        "result": _search_result_text(matches, truncated),
        "results": matches,
        "count": len(matches),
        "truncated": truncated,
    }


async def _do_search(
    root: Path, regex: re.Pattern, pattern: str,
    max_results: int, max_file_size: int, ctx: ToolContext,
    output_mode: str = "content",
) -> dict[str, Any]:
    allow_binary = root.is_file()

    # 1) collect paths only — no file content is read before approval
    paths = await asyncio.to_thread(
        _collect_paths, root, pattern, max_file_size, ctx.effective_workspace,
    )

    # 2) per-file read decisions, batched so a filled result budget stops early
    state = _new_search_state(output_mode)
    blocked: list[dict[str, Any]] = []
    for start in range(0, len(paths), _GUARD_BATCH):
        approved, blocked_batch, cancelled = await _guard_paths(
            ctx, paths[start:start + _GUARD_BATCH], reason="grep",
        )
        blocked.extend(blocked_batch)
        if cancelled:
            return _error_response("task cancelled", cancelled=True, blocked=blocked)
        # 3) scan only what was approved
        await asyncio.to_thread(_scan_batch, state, approved, regex, max_results, allow_binary)
        if state["truncated"]:
            break

    data = _format_search(state)
    if blocked:
        data["blocked"] = blocked
        data["result"] = f"{data['result']}\n[{_blocked_summary(blocked)}]"
    return {"ok": True, "success": True, "data": data}


# ---------------------------------------------------------------------------
#  Replace mode
# ---------------------------------------------------------------------------

def _match_approved(
    files: list[tuple[Path, str]], regex: re.Pattern, max_files: int,
) -> tuple[list[tuple[Path, str]], dict[str, int]]:
    """Read approved files and keep the ones that both match and round-trip."""
    matched: list[tuple[Path, str]] = []
    skipped = {"binary": 0, "undecodable": 0, "unreadable": 0, "no_match": 0}
    for target, rel in files:
        text, skip_reason = _load_text(target, strict=True, allow_binary=False)
        if text is None:
            if skip_reason in skipped:
                skipped[skip_reason] += 1
            continue
        if not regex.search(text):
            skipped["no_match"] += 1
            continue
        matched.append((target, rel))
        if len(matched) >= max_files:
            break
    return matched, skipped


def _validate_replace_template(regex: re.Pattern, repl: str) -> str:
    """Return an error message when *repl* is not a usable regex template.

    [Audit B 2026-10] Why: ``re`` parses a replacement template only when it is
    applied, so an invalid group reference (``$9`` with two groups,
    ``\\g<missing>``) used to raise from a worker thread — after every write
    approval had already been requested. How: substitute into an empty string;
    CPython compiles the template eagerly even when nothing matches. Purpose: one
    stdlib check rejects the whole operation before any file is touched, instead
    of failing per file (or mid-batch).
    """
    try:
        regex.sub(repl, "")
    except (re.error, IndexError) as exc:
        # re raises IndexError (not re.error) for an unknown \\g<name> group.
        return f"invalid replacement {repl!r}: {exc}"
    return ""


def _apply_replace_file(
    p: Path, rel: str, regex: re.Pattern, repl: str, is_regex: bool,
) -> dict[str, Any]:
    """Rewrite one already-approved file.

    Returns ``{"matches": [...]}`` on success, ``{"skip": reason}`` when the file
    must not be touched, or ``{"error": msg}`` when the write failed.
    ``errors="ignore"`` is never used here: a file that does not round-trip as
    UTF-8 stays untouched instead of being silently corrupted, and the original
    line endings are preserved.
    """
    text, skip_reason = _load_text(p, strict=True, allow_binary=False)
    if text is None:
        return {"matches": [], "skip": skip_reason}

    matches: list[dict[str, Any]] = []
    for m in regex.finditer(text):
        line, col = _line_col(text, m.start())
        matches.append({"file": rel, "line": line, "column": col, "match": m.group()})
    if not matches:
        return {"matches": [], "skip": "no_match"}

    if is_regex:
        new_text = regex.sub(repl, text)
    else:
        new_text = regex.sub(lambda _: repl, text)
    if new_text == text:
        # Matched but the replacement is identical — do not churn the file.
        return {"matches": matches, "skip": "unchanged"}
    try:
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    except Exception as exc:
        return {"matches": [], "error": str(exc)}
    return {"matches": matches}


def _apply_approved_writes(
    files: list[tuple[Path, str]], regex: re.Pattern, repl: str, is_regex: bool,
) -> list[tuple[str, dict[str, Any]]]:
    """Apply the replacement to each already-approved file (worker thread).

    [Audit B 2026-10] Why: the per-file call used to sit in a list comprehension,
    so the first exception escaped the worker thread and discarded the report of
    every file already rewritten. How: isolate each file and collect its outcome.
    Purpose: a mid-batch I/O failure still reports the writes that did happen.
    """
    outcomes: list[tuple[str, dict[str, Any]]] = []
    for target, rel in files:
        try:
            outcome = _apply_replace_file(target, rel, regex, repl, is_regex)
        except Exception as exc:  # noqa: BLE001 - one file must not sink the batch
            outcome = {"matches": [], "error": f"{type(exc).__name__}: {exc}"}
        outcomes.append((rel, outcome))
    return outcomes


async def _do_replace(
    root: Path, regex: re.Pattern, query: str, replace_str: str,
    pattern: str, max_files: int, max_file_size: int, ctx: ToolContext,
    is_regex: bool = False,
) -> dict[str, Any]:
    # 兼容 $1/$2 捕获组语法（仅 is_regex=True 时转换未转义的 $1~$99 为 \g<N>；
    # is_regex=False 时按字面量替换，反斜杠和 $n 原样保留）。
    norm_repl = (
        re.sub(r"(?<!\\)\$(\d+)", r"\\g<\1>", replace_str) if is_regex else replace_str
    )
    # [Audit B] Reject an unusable template before collecting paths, asking for
    # approvals or writing anything: an invalid group fails on every file anyway,
    # only later and as a bare exception out of the worker thread.
    if is_regex:
        template_error = _validate_replace_template(regex, norm_repl)
        if template_error:
            return _error_response(template_error, mode="replace")

    # 1) collect paths only — a file that will be denied is never read
    paths = await asyncio.to_thread(
        _collect_paths, root, pattern, max_file_size, ctx.effective_workspace,
    )

    # 2) per-file read decisions, then match on approved files only
    matched: list[tuple[Path, str]] = []
    skipped = {"binary": 0, "undecodable": 0, "unreadable": 0, "no_match": 0}
    blocked: list[dict[str, Any]] = []
    for start in range(0, len(paths), _GUARD_BATCH):
        approved, blocked_batch, cancelled = await _guard_paths(
            ctx, paths[start:start + _GUARD_BATCH], reason="search_replace scan",
        )
        blocked.extend(blocked_batch)
        if cancelled:
            return _error_response("task cancelled", cancelled=True, blocked=blocked)
        newly, skipped_batch = await asyncio.to_thread(
            _match_approved, approved, regex, max_files - len(matched),
        )
        matched.extend(newly)
        for reason, count in skipped_batch.items():
            skipped[reason] += count
        if len(matched) >= max_files:
            break

    # 3) one write decision per matched file, keyed by its resolved path
    write_approved: list[tuple[Path, str]] = []
    for target, rel in matched:
        _op, err = await request_guard(
            ctx, "write_file", {"path": str(target), "reason": f"search_replace in {rel}"},
        )
        if err is not None:
            if err.get("cancelled"):
                return _error_response("task cancelled", cancelled=True, blocked=blocked)
            blocked.append({"file": rel, "error": str(err.get("error", "denied"))})
            continue
        write_approved.append((target, rel))

    outcomes = await asyncio.to_thread(
        _apply_approved_writes, write_approved, regex, norm_repl, is_regex,
    )

    all_matches: list[dict[str, Any]] = []
    files_modified: list[str] = []
    failures: list[dict[str, Any]] = []
    for rel, outcome in outcomes:
        skip_reason = outcome.get("skip")
        if skip_reason:
            if skip_reason in skipped:
                skipped[skip_reason] += 1
            # "no_match" / "unchanged" -> nothing written, nothing counted
            continue
        if "error" in outcome:
            failures.append({"file": rel, "error": outcome["error"]})
            continue
        all_matches.extend(outcome.get("matches") or [])
        files_modified.append(rel)

    replace_results = [
        {
            "file": f,
            "replacements": sum(1 for m in all_matches if m["file"] == f),
        }
        for f in files_modified
    ]
    text = _replace_result_text(replace_results, len(files_modified), len(all_matches))
    if blocked:
        text += "\n" + "\n".join(f"blocked: {b['file']} ({b['error']})" for b in blocked)
    if failures:
        text += "\n" + "\n".join(f"failed: {f['file']} ({f['error']})" for f in failures)

    # [AutoC 2026-05-31] Why: replace mode now returns a unified success payload.
    # How: put human-readable replacement counts in data.result and preserve all
    # structured replacement details below data. Purpose: keep both model history
    # and downstream programmatic consumers useful.
    data: dict[str, Any] = {
        "result": text,
        "matches": all_matches,
        "results": replace_results,
        "filesModified": len(files_modified),
        "totalReplacements": len(all_matches),
    }
    if blocked:
        data["blocked"] = blocked
    if failures:
        data["failures"] = failures
    for reason in ("binary", "undecodable", "unreadable"):
        if skipped.get(reason):
            data[f"skipped{reason.capitalize()}"] = skipped[reason]
    if blocked and not files_modified:
        return _error_response(
            f"all {len(blocked)} candidate file(s) blocked by policy",
            cancelled=False, blocked=blocked,
        )
    return {
        "ok": True,
        "success": True,
        "cancelled": False,
        "data": data,
    }


# ---------------------------------------------------------------------------
#  Main entry
# ---------------------------------------------------------------------------

async def search_in_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = _as_str(args.get("query"))
    mode = _as_str(args.get("mode"), "search").lower()
    rel_path = _as_str(args.get("path"), ".") or "."
    pattern = _as_str(args.get("pattern"), "**/*")
    is_regex = _as_bool(args.get("isRegex", False))
    max_results = _as_int(args.get("maxResults") or args.get("max_results") or 100, 100)
    replace_str = _as_str(args.get("replace"))
    max_files = _as_int(args.get("maxFiles") or args.get("max_files") or 50, 50)
    output_mode = _as_str(args.get("output_mode"), "content").strip().lower()
    if output_mode not in ("content", "files_only", "count"):
        output_mode = "content"

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
        root, _trust = resolve_and_classify(ctx.workspace_root, rel_path, workspace=ctx.workspace)
        is_ext = _trust == "external"
    except ValueError as exc:
        return _error_response(str(exc))
    if not root.exists():
        return _error_response("path not found", path=rel_path)

    # ---- External search-root approval (per-file decisions follow below) ----
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
            root, regex, query, replace_str, pattern, max_files, max_file_size_bytes, ctx, is_regex,
        )
    else:
        return await _do_search(
            root, regex, pattern, max_results, max_file_size_bytes, ctx, output_mode,
        )
