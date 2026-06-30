from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolResultFormatContext:
    """Context supplied to structure-based tool-result formatters."""

    tool_name: str = ""
    tool_spec: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolResultFormatter:
    """One structure-based formatter entry.

    The engine keeps the formatter API small so callers can add routing rules
    without editing result_to_raw().
    """

    id: str
    priority: int
    predicate: Callable[[Any, ToolResultFormatContext], bool]
    render: Callable[[Any, ToolResultFormatContext], tuple[str, str] | None]


_FORMATTERS: list[ToolResultFormatter] = []


def register_result_formatter(formatter: ToolResultFormatter) -> None:
    """Register or replace a tool-result formatter."""
    # [AutoC 2026-05-31] Why: result_to_raw() should no longer grow hard-coded
    # tool-name branches. How: keep formatter registration in one ordered table
    # keyed by formatter id, replacing existing entries when tests or plugins
    # intentionally re-register an id. Purpose: make result rendering extensible
    # while preserving deterministic priority order.
    if not isinstance(formatter.id, str) or not formatter.id.strip():
        raise ValueError("formatter id is required")
    existing_index = next((i for i, item in enumerate(_FORMATTERS) if item.id == formatter.id), None)
    if existing_index is None:
        _FORMATTERS.append(formatter)
    else:
        _FORMATTERS[existing_index] = formatter
    _FORMATTERS.sort(key=lambda item: (item.priority, item.id))


def format_tool_result_by_structure(
    result: Any,
    ctx: ToolResultFormatContext | None = None,
) -> tuple[str, str] | None:
    """Format a tool result by structural features instead of tool name."""
    # [AutoC 2026-05-31] Why: tools from builtins, MCP, and external scripts can
    # share useful result shapes even when their names differ. How: walk the
    # registered formatters by priority and let strict predicates choose a match.
    # Purpose: route output automatically and fall back safely when no structure
    # is recognized.
    safe_ctx = ctx or ToolResultFormatContext()
    for formatter in list(_FORMATTERS):
        try:
            if not formatter.predicate(result, safe_ctx):
                continue
            rendered = formatter.render(result, safe_ctx)
        except Exception:
            logger.warning(
                "tool result formatter %s failed for tool %s",
                formatter.id,
                safe_ctx.tool_name or "<unknown>",
                exc_info=True,
            )
            continue
        if rendered is not None:
            return rendered
    return None


def json_fallback(result: Any) -> tuple[str, str]:
    """Return a JSON text representation when no formatter matches."""
    # [AutoC 2026-05-31] Why: unknown tool result structures must remain visible
    # after removing name-based branches. How: preserve the old json.dumps path
    # and fall back to str() only when serialization fails. Purpose: keep backward
    # compatibility for arbitrary tool payloads.
    try:
        return "json", json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return "json", str(result)


def _data_dict(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else None


def _is_read_file_entry(entry: Any) -> bool:
    # [AutoC 2026-05-31] Why: read_file batch output has a broad data.results
    # wrapper that can overlap with search and list outputs. How: require every
    # non-failed entry to carry one of the documented file result types. Purpose:
    # avoid misrouting other batched tools that also return data.results.
    if not isinstance(entry, dict):
        return False
    if entry.get("success") is False:
        return "path" in entry and "error" in entry
    return entry.get("type") in {"text", "multimodal", "binary"} and "path" in entry


def _is_search_match_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return isinstance(entry.get("file"), str) and "line" in entry and isinstance(entry.get("match"), str)


def _is_list_dir_section(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    entries = section.get("entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("name"), str):
            return False
        if entry.get("type") not in {"directory", "file"}:
            return False
    return True


def _display_directive_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    return isinstance(result, dict) and ("_display" in result or "_format" in result)


def _display_directive_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    if not isinstance(result, dict):
        return None

    # [AutoC 2026-05-31] Why: some tools need to explicitly provide a compact
    # display string while still returning structured metadata. How: prefer a
    # string _display value, then support a small dict form with format/raw-like
    # fields, and finally let _format name an existing formatter. Purpose: give
    # tools a stable escape hatch without reintroducing tool-name branches.
    display = result.get("_display")
    if isinstance(display, str):
        return "text", display
    if isinstance(display, dict):
        raw_value = display.get("raw")
        if raw_value is None:
            raw_value = display.get("text")
        if raw_value is None:
            raw_value = display.get("message")
        if isinstance(raw_value, str):
            format_value = display.get("format")
            return str(format_value) if isinstance(format_value, str) and format_value else "text", raw_value

    format_id = result.get("_format")
    if not isinstance(format_id, str) or not format_id.strip():
        return None
    format_id = format_id.strip()
    if format_id == "json":
        return json_fallback(result)
    if format_id == "text":
        for key in ("text", "description", "message", "output", "result"):
            value = result.get(key)
            if isinstance(value, str):
                return "text", value
        return None
    return _render_formatter_by_id(format_id, result, ctx, skip_ids={"display_directive"})


def _spec_result_format_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    spec = ctx.tool_spec
    if not isinstance(spec, Mapping):
        return False
    format_id = spec.get("result_format")
    return isinstance(format_id, str) and bool(format_id.strip())


def _spec_result_format_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    spec = ctx.tool_spec
    if not isinstance(spec, Mapping):
        return None
    format_id = spec.get("result_format")
    if not isinstance(format_id, str) or not format_id.strip():
        return None

    # [AutoC 2026-05-31] Why: external tools may declare the formatter they want
    # in SPEC.result_format. How: support simple text/json directives directly,
    # then resolve other ids through the same formatter registry while skipping
    # this dispatcher to prevent recursion. Purpose: preserve external tool
    # metadata while keeping result_to_raw() free of tool-specific conditions.
    format_id = format_id.strip()
    if format_id == "json":
        return json_fallback(result)
    if format_id == "text":
        for key in ("text", "description", "message", "output", "result"):
            value = result.get(key) if isinstance(result, dict) else None
            if isinstance(value, str):
                return "text", value
        return "text", str(result)
    return _render_formatter_by_id(format_id, result, ctx, skip_ids={"spec_result_format"})


def _render_formatter_by_id(
    formatter_id: str,
    result: Any,
    ctx: ToolResultFormatContext,
    *,
    skip_ids: set[str] | None = None,
) -> tuple[str, str] | None:
    skip = skip_ids or set()
    for formatter in list(_FORMATTERS):
        if formatter.id != formatter_id or formatter.id in skip:
            continue
        try:
            if not formatter.predicate(result, ctx):
                return None
            return formatter.render(result, ctx)
        except Exception:
            logger.warning(
                "tool result formatter %s failed for tool %s",
                formatter.id,
                ctx.tool_name or "<unknown>",
                exc_info=True,
            )
            return None
    return None


def _read_file_batch_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    data = _data_dict(result)
    if data is None:
        return False
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return False
    return all(_is_read_file_entry(entry) for entry in results)


def _read_file_batch_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    data = _data_dict(result)
    if data is None:
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None

    # [AutoC 2026-05-31] Why: the previous read_file formatter was embedded in
    # result_to_raw() and only worked when the tool name was read_file. How: render
    # any result with the documented data.results file-entry structure. Purpose:
    # let compatible external tools reuse the same readable file transcript.
    parts: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if entry.get("success") and entry.get("type") == "text":
            content = entry.get("content")
            if isinstance(content, str) and content:
                parts.append(f"── {path} ──\n{content}")
        elif entry.get("success") and entry.get("type") == "multimodal":
            parts.append(f"── {path} ── [image: {entry.get('mimeType', '?')}, {entry.get('size', 0)} bytes]")
        elif entry.get("success") and entry.get("type") == "binary":
            parts.append(f"── {path} ── [binary: {entry.get('size', 0)} bytes]")
        elif entry.get("success") is False:
            parts.append(f"── {path} ── ERROR: {entry.get('error', 'unknown')}")
    if not parts:
        return None
    return "text", "\n".join(parts)


def _search_matches_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    data = _data_dict(result)
    if data is None:
        return False
    results = data.get("results")
    if not isinstance(results, list):
        return False
    if not isinstance(data.get("count"), int) or not isinstance(data.get("truncated"), bool):
        return False
    return all(_is_search_match_entry(entry) for entry in results)


def _search_matches_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    data = _data_dict(result)
    if data is None:
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None

    # [AutoC 2026-05-31] Why: search-style results should remain easy to scan
    # after routing by shape instead of the grep (formerly search_in_files) name. How: preserve
    # the existing multiline location, match, and context rendering. Purpose:
    # keep tool history readable for any tool that returns the same structure.
    count = data.get("count", sum(1 for item in results if isinstance(item, dict)))
    truncated = bool(data.get("truncated", False))
    parts = [f"{count} results found{' (truncated)' if truncated else ''}:"]
    for item in results:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file") or "?")
        line_no = item.get("line", "?")
        match_text = str(item.get("match") or "").replace("\n", "\\n")
        parts.append("")
        parts.append(f"{file_path}:{line_no} | {match_text}")
        context = item.get("context")
        if context is not None:
            for context_line in str(context).splitlines():
                parts.append(f"  {context_line}")
    return "text", "\n".join(parts)


def _search_replace_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    data = _data_dict(result)
    if data is None:
        return False
    return isinstance(data.get("filesModified"), int) and isinstance(data.get("totalReplacements"), int)


def _search_replace_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    data = _data_dict(result)
    if data is None:
        return None

    # [AutoC 2026-05-31] Why: replace-mode search results describe write effects,
    # not match context. How: render aggregate replacement counts and per-file
    # counts from the documented data.results structure. Purpose: make replace
    # operations concise without hiding which files changed.
    files_modified = data.get("filesModified")
    total_replacements = data.get("totalReplacements")
    parts = [f"{files_modified} files modified, {total_replacements} replacements total"]
    results = data.get("results")
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("file")
            replacements = entry.get("replacements")
            if isinstance(file_path, str) and isinstance(replacements, int):
                parts.append(f"{file_path}: {replacements} replacements")
    return "text", "\n".join(parts)


def _list_dir_tree_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    data = _data_dict(result)
    if data is None:
        return False
    if not isinstance(data.get("totalFiles"), int) or not isinstance(data.get("totalDirs"), int):
        return False
    results = data.get("results")
    if not isinstance(results, list):
        return False
    return all(_is_list_dir_section(section) for section in results)


def _list_dir_tree_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    data = _data_dict(result)
    if data is None:
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None

    # [AutoC 2026-05-31] Why: directory listings are tree-like and difficult to
    # read as JSON. How: carry forward the old section-header and file/folder
    # marker rendering, but trigger it from totals plus entries shape. Purpose:
    # keep list output compact for builtins and compatible third-party tools.
    parts: list[str] = []
    for section in results:
        if not isinstance(section, dict):
            continue
        path = str(section.get("path") or "?")
        file_count = section.get("fileCount", 0)
        dir_count = section.get("dirCount", 0)
        if section.get("success") is False:
            parts.append(f"── {path} ── ERROR: {section.get('error', 'unknown')}")
            parts.append("")
            continue

        parts.append(f"── {path} ── ({file_count} files, {dir_count} dirs)")
        entries = section.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                entry_type = entry.get("type")
                if entry_type == "directory":
                    if not name.endswith("/"):
                        name += "/"
                    parts.append(f"  📁 {name}")
                elif entry_type == "file":
                    parts.append(f"  📄 {name}")
                else:
                    parts.append(f"  • {name}")
        parts.append("")

    while parts and parts[-1] == "":
        parts.pop()
    total_files = data.get("totalFiles", 0)
    total_dirs = data.get("totalDirs", 0)
    if parts:
        parts.append("")
    parts.append(f"Total: {total_files} files, {total_dirs} dirs")
    return "text", "\n".join(parts)


def _command_output_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    return isinstance(result, dict) and "returncode" in result and isinstance(result.get("output"), str)


def _command_output_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    if not isinstance(result, dict) or not isinstance(result.get("output"), str):
        return None
    # [AutoC 2026-05-31] Why: command-like tools share returncode/output even
    # when they are not named execute_command. How: use the documented top-level
    # fields as the route key and keep the old text layout. Purpose: preserve
    # command transcripts while removing name checks.
    return "text", f"returncode={result.get('returncode')}\n{result.get('output', '')}"


def _apply_diff_summary_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    data = _data_dict(result)
    if data is None:
        return False
    has_path = isinstance(data.get("file"), str) or isinstance(data.get("path"), str)
    # [AutoC 2026-05-31] Why: current apply_diff returns diffCount,
    # appliedCount, and failedCount, while older tests and documented migrations
    # also cover applied/rejected names. How: accept either count vocabulary after
    # a strict data.file/data.path check. Purpose: preserve compatibility without
    # reintroducing a tool-name condition.
    has_counts = any(
        key in data
        for key in ("diffCount", "appliedCount", "failedCount", "applied", "rejected", "rejectedCount")
    )
    return has_path and has_counts


def _apply_diff_summary_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    data = _data_dict(result)
    if data is None:
        return None
    path = data.get("path") or data.get("file")
    if not isinstance(path, str) or not path:
        return None

    # [AutoC 2026-05-31] Why: apply_diff output is consumed as a short action
    # summary, not as a full JSON object. How: accept both legacy applied/rejected
    # names and current appliedCount/failedCount names. Purpose: keep existing
    # concise output while allowing response-schema drift.
    applied = data.get("applied", data.get("appliedCount", "?"))
    rejected = data.get("rejected", data.get("rejectedCount", data.get("failedCount", "?")))
    return "text", f"apply_diff on {path}: {applied} applied, {rejected} rejected"


def _write_file_summary_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return False
    if isinstance(result.get("path"), str) and isinstance(result.get("bytes"), int):
        return True
    data = result.get("data")
    # [AutoC 2026-05-31] Why: the current builtin returns top-level path/bytes,
    # but existing formatter tests also cover the older data.path success wrapper.
    # How: allow only the minimal legacy wrapper with no extra data keys, after an
    # explicit non-failure result check. Purpose: keep backward compatibility
    # without broadly misclassifying unrelated data.path payloads as writes.
    return isinstance(data, dict) and set(data.keys()) == {"path"} and isinstance(data.get("path"), str)


def _write_file_summary_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    path = result.get("path")
    if not isinstance(path, str) and isinstance(data, dict):
        path = data.get("path")
    if not isinstance(path, str) or not path:
        return None
    # [AutoC 2026-05-31] Why: write_file has a small success payload whose useful
    # part is the path. How: render either the current top-level path/bytes shape
    # or the legacy data.path wrapper. Purpose: preserve existing output while
    # avoiding hard-coded tool-name routing.
    return "text", f"File written: {path}"


def _mcp_content_parts_from_result(result: Any) -> list[Any] | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if isinstance(content, list):
        return content
    nested = result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("content"), list):
        return nested.get("content")
    return None


def _is_mcp_content_part(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    return part.get("type") in {"text", "image", "resource"}


def _mcp_content_parts_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    content = _mcp_content_parts_from_result(result)
    if not isinstance(content, list):
        return False
    return bool(content) and all(_is_mcp_content_part(part) for part in content)


def _mcp_content_parts_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    content = _mcp_content_parts_from_result(result)
    if not isinstance(content, list):
        return None

    # [AutoC 2026-05-31] Why: MCP call_tool wraps provider-specific content parts
    # that should be readable without exposing raw SDK JSON. How: concatenate text
    # parts and summarize image/resource parts with stable metadata. Purpose: keep
    # MCP results compact while still showing non-text outputs.
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif part_type == "image":
            path = part.get("path")
            mime = part.get("mimeType") or part.get("mime_type") or part.get("mime") or "image"
            if isinstance(path, str) and path:
                parts.append(f"[image: {path}]")
            else:
                data = part.get("data")
                size_text = f", {len(data)} base64 chars" if isinstance(data, str) else ""
                parts.append(f"[image: {mime}{size_text}]")
        elif part_type == "resource":
            resource = part.get("resource")
            uri = resource.get("uri") if isinstance(resource, dict) else part.get("uri")
            if isinstance(uri, str) and uri:
                parts.append(f"[resource: {uri}]")
            else:
                try:
                    parts.append(json.dumps(part, ensure_ascii=False, indent=2))
                except Exception:
                    parts.append(str(part))
    if not parts:
        return None
    return "text", "\n".join(parts)


def _attachment_entries(result: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    # [AutoC 2026-05-31] Why: generated media tools are migrating attachment
    # metadata from top-level attachments/path fields into data.attachments and
    # data.path. How: scan the nested data dict first and then the legacy top-level
    # fields, de-duplicating by path. Purpose: keep formatter fallback useful for
    # partially migrated attachment results that do not yet include data.result.
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    attachment_sources = [data.get("attachments"), result.get("attachments")]
    for attachments in attachment_sources:
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    path = attachment.get("path")
                    if isinstance(path, str) and path and path not in paths:
                        paths.append(path)
                elif isinstance(attachment, str) and attachment and attachment not in paths:
                    paths.append(attachment)
    for source in (data, result):
        for key in ("path", "image_path", "audio_path", "video_path"):
            path = source.get(key) if isinstance(source, dict) else None
            if isinstance(path, str) and path.startswith("data/") and path not in paths:
                paths.append(path)
    return paths


def _attachment_result_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    if not isinstance(result, dict):
        return False
    # [AutoC 2026-05-31] Why: attachment fields may now be nested under data.
    # How: reuse _attachment_entries instead of checking only top-level fields.
    # Purpose: keep attachment-like payloads routed correctly during migration.
    return bool(_attachment_entries(result))


def _attachment_result_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    if not isinstance(result, dict):
        return None
    paths = _attachment_entries(result)
    if not paths:
        return None

    # [AutoC 2026-05-31] Why: generated files and media usually arrive through
    # attachments rather than primary text. How: list attachment paths and include
    # an optional top-level text/description/message first. Purpose: make produced
    # files visible in raw tool history without depending on each tool name.
    parts: list[str] = []
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for source in (data, result):
        for key in ("text", "description", "message"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
        if parts:
            break
    parts.append("Attachments:")
    parts.extend(f"- {path}" for path in paths)
    return "text", "\n".join(parts)


def _primary_text_result_predicate(result: Any, ctx: ToolResultFormatContext) -> bool:
    if not isinstance(result, dict):
        return False
    for key in ("text", "description", "message", "output", "result"):
        if isinstance(result.get(key), str):
            return True
    return False


def _primary_text_result_render(result: Any, ctx: ToolResultFormatContext) -> tuple[str, str] | None:
    if not isinstance(result, dict):
        return None

    # [AutoC 2026-05-31] Why: many external tools return a single human-readable
    # field without any richer schema. How: choose the first documented primary
    # string field in stable order. Purpose: avoid JSON noise for simple text
    # responses while leaving unknown structures to json_fallback().
    for key in ("text", "description", "message", "output", "result"):
        value = result.get(key)
        if isinstance(value, str):
            return "text", value
    return None


_DEFAULT_FORMATTERS = [
    ToolResultFormatter("display_directive", 10, _display_directive_predicate, _display_directive_render),
    ToolResultFormatter("spec_result_format", 20, _spec_result_format_predicate, _spec_result_format_render),
    ToolResultFormatter("read_file_batch", 100, _read_file_batch_predicate, _read_file_batch_render),
    ToolResultFormatter("search_matches", 110, _search_matches_predicate, _search_matches_render),
    ToolResultFormatter("search_replace", 111, _search_replace_predicate, _search_replace_render),
    ToolResultFormatter("list_dir_tree", 120, _list_dir_tree_predicate, _list_dir_tree_render),
    ToolResultFormatter("command_output", 130, _command_output_predicate, _command_output_render),
    ToolResultFormatter("apply_diff_summary", 140, _apply_diff_summary_predicate, _apply_diff_summary_render),
    ToolResultFormatter("write_file_summary", 150, _write_file_summary_predicate, _write_file_summary_render),
    ToolResultFormatter("mcp_content_parts", 200, _mcp_content_parts_predicate, _mcp_content_parts_render),
    ToolResultFormatter("attachment_result", 210, _attachment_result_predicate, _attachment_result_render),
    ToolResultFormatter("primary_text_result", 300, _primary_text_result_predicate, _primary_text_result_render),
]

for _formatter in _DEFAULT_FORMATTERS:
    register_result_formatter(_formatter)
