from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .tool_result_formatters import (
    ToolResultFormatContext,
    format_tool_result_by_structure,
    json_fallback,
)



def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "x").strip() or "x")[:80]


_DEFAULT_INLINE_LIMITS: dict[str, int] = {
    "default": 32_000,
    "read_file": 80_000,
    "grep": 50_000,
    "list_dir": 50_000,
    "execute_command": 24_000,
    "media": 8_000,
}
_MEDIA_TOOL_HINTS = ("image", "video", "audio", "base64", "read_video", "read_image")
_SEARCH_TOOL_HINTS = ("search", "web", "exa", "mcp_", "gelbooru", "danbooru")
_HEAD_TAIL_TOOLS = {"execute_command"}


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return parsed if parsed > 0 else int(default)


def _tool_trace_cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    engine = config.get("engine")
    if not isinstance(engine, Mapping):
        return {}
    tool_trace = engine.get("tool_trace")
    return tool_trace if isinstance(tool_trace, Mapping) else {}


def _tool_limit_key(tool_name: str) -> str:
    name = (tool_name or "").strip()
    lowered = name.lower()
    if lowered in _DEFAULT_INLINE_LIMITS:
        return lowered
    if any(hint in lowered for hint in _MEDIA_TOOL_HINTS):
        return "media"
    if lowered.startswith(("mcp_", "web_")) or any(hint in lowered for hint in _SEARCH_TOOL_HINTS):
        return lowered if lowered in {"grep"} else "default"
    return lowered


def get_tool_inline_limit(tool_name: str, config: Mapping[str, Any] | None) -> int:
    """Return the inline character budget for one tool result."""
    # [AutoC 2026-06-08] Why: runtime.yaml historically allowed one integer
    # max_inline_chars value, while large-output recovery now needs per-tool limits.
    # How: accept either an int-like scalar or a dict with tool-name overrides and
    # category defaults. Purpose: old deployments keep working and new deployments can
    # tune read_file, shell, search, and media output independently.
    raw_limits = _tool_trace_cfg(config).get("max_inline_chars")
    if isinstance(raw_limits, Mapping):
        default = _coerce_positive_int(raw_limits.get("default"), _DEFAULT_INLINE_LIMITS["default"])
        lowered = (tool_name or "").strip().lower()
        key = _tool_limit_key(tool_name)
        if lowered in raw_limits:
            return _coerce_positive_int(raw_limits.get(lowered), default)
        if key in raw_limits:
            return _coerce_positive_int(raw_limits.get(key), default)
        if key == "media" and "media" in raw_limits:
            return _coerce_positive_int(raw_limits.get("media"), default)
        return default
    if raw_limits is not None:
        return _coerce_positive_int(raw_limits, _DEFAULT_INLINE_LIMITS["default"])
    key = _tool_limit_key(tool_name)
    return _DEFAULT_INLINE_LIMITS.get(key, _DEFAULT_INLINE_LIMITS["default"])


def get_tool_step_inline_budget(config: Mapping[str, Any] | None) -> int:
    """Return the per-step total inline character budget for tool results."""
    # [AutoC 2026-06-08] Why: several individually valid tool outputs can still exceed
    # the model context when combined in one round. How: read a step-level budget with
    # a safe 120k default. Purpose: later tool results in the same round are forced
    # smaller once earlier results have consumed the shared inline budget.
    return _coerce_positive_int(_tool_trace_cfg(config).get("max_inline_chars_per_step"), 120_000)


def artifact_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return whether tool-result artifact writing is enabled."""
    raw = _tool_trace_cfg(config).get("artifact_enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _tail_ratio(config: Mapping[str, Any] | None) -> float:
    raw = _tool_trace_cfg(config).get("tail_ratio", 0.25)
    try:
        ratio = float(raw)
    except Exception:
        ratio = 0.25
    return min(0.9, max(0.05, ratio))


def _head_tail_tools(config: Mapping[str, Any] | None) -> set[str]:
    raw = _tool_trace_cfg(config).get("head_tail_tools")
    if not isinstance(raw, list):
        return set(_HEAD_TAIL_TOOLS)
    values = {str(item).strip().lower() for item in raw if str(item).strip()}
    return values or set(_HEAD_TAIL_TOOLS)


def write_artifact(
    workspace_root: Path,
    task_id: str,
    step: int,
    index: int,
    tool_name: str,
    tool_call_id: str,
    raw_text: str,
) -> str:
    """Write a full tool result to a task-scoped artifact and return a relative path."""
    # [AutoC 2026-06-08] Why: context truncation must not discard the full tool output
    # while a task is still running. How: write the raw text under
    # data/artifacts/tool_results/{task_id}/ with sanitized components. Purpose: the
    # truncated model-visible content can point to a local file that read_file or grep
    # can inspect during the task.
    root = Path(workspace_root)
    safe_task_id = _sanitize(task_id or "task")
    safe_tool = _sanitize(tool_name or "tool")
    safe_call = _sanitize(tool_call_id or "call")
    filename = f"{int(step):03d}_{int(index):02d}_{safe_tool}_{safe_call}.txt"
    path = root / "data" / "artifacts" / "tool_results" / safe_task_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(raw_text or ""), encoding="utf-8", errors="ignore")
    return path.relative_to(root).as_posix()


def cleanup_tool_result_artifacts(workspace_root: Path, task_id: str) -> bool:
    """Remove the task-scoped tool-result artifact directory."""
    # [AutoC 2026-06-08] Why: full raw tool outputs may contain sensitive project data
    # and are needed only while the task can read them back. How: delete the whole
    # task directory when a terminal task is observed. Purpose: normal task completion
    # does not leave recoverable raw output on disk.
    if not str(task_id or "").strip():
        # [AutoC 2026-06-08] Why: _sanitize() maps an empty value to "x", which is
        # appropriate for filenames but unsafe for deletion. How: reject empty task IDs
        # before sanitizing. Purpose: a malformed terminal task cannot delete an
        # unrelated tool_results/x directory.
        return False
    safe_task_id = _sanitize(task_id or "")
    target = Path(workspace_root) / "data" / "artifacts" / "tool_results" / safe_task_id
    try:
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
            return True
    except Exception:
        return False
    return False


def truncate_tool_result(
    tool_name: str,
    raw_text: str,
    max_chars: int,
    ref_path: str,
    head_tail: bool = False,
    tail_ratio: float = 0.25,
    *,
    config: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    """Return an inline-safe tool result plus whether truncation occurred."""
    # [AutoC 2026-06-08] Why: large tool outputs should keep useful inline context and
    # a direct local recovery path, not a wrapped block that changes formatter shape.
    # How: keep head-only for most tools, head+tail for configured tools such as
    # execute_command, and append a single guidance line containing the artifact path.
    # Purpose: native, fake-native, and JSON tool modes all receive plain content with
    # the same readable truncation hint.
    text = str(raw_text or "")
    limit = _coerce_positive_int(max_chars, _DEFAULT_INLINE_LIMITS["default"])
    if len(text) <= limit:
        return text, False

    original_len = len(text)
    ref = str(ref_path or "").strip() or "data/artifacts/tool_results/<missing>.txt"
    lowered = (tool_name or "").strip().lower()
    if config is not None:
        # [AutoC 2026-06-08] Allow runtime.yaml to decide head+tail behavior while
        # preserving the explicit head_tail/tail_ratio parameters requested for the
        # helper. Why: synchronous and async callers already carry runtime config.
        # How: config values override the default False flag only for listed tools.
        # Purpose: execute_command keeps tail context without every caller duplicating
        # config parsing logic.
        head_tail = head_tail or lowered in _head_tail_tools(config)
        tail_ratio = _tail_ratio(config)
    else:
        # [AutoC 2026-06-08] Keep execute_command head+tail by default when tests or
        # legacy callers invoke the helper without runtime config. Why: the requested
        # design names execute_command as a head_tail tool. How: consult the built-in
        # default set when no config is supplied. Purpose: direct helper behavior
        # matches production defaults.
        head_tail = head_tail or lowered in _HEAD_TAIL_TOOLS
    if head_tail:
        tail_chars = max(1, int(limit * min(0.9, max(0.05, float(tail_ratio)))))
        head_chars = max(1, limit - tail_chars)
        if head_chars + tail_chars > limit:
            head_chars = max(1, limit - tail_chars)
        guidance = (
            f"...[middle omitted, showing first {head_chars:,} and last {tail_chars:,} "
            f"of {original_len:,} chars. Full output: {ref} — use read_file or grep to inspect.]"
        )
        return text[:head_chars] + "\n" + guidance + "\n" + text[-tail_chars:], True

    guidance = (
        f"...[truncated, showing {limit:,} of {original_len:,} chars. "
        f"Full output: {ref} — use read_file or grep to inspect.]"
    )
    return text[:limit] + "\n" + guidance, True


def result_to_raw(
    tool_name: str,
    result: Any,
    *,
    tool_spec: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """把工具结果转为 (format, raw_text)。"""
    # [AutoC 2026-05-31] Why: tools are migrating to a unified ok/data/error
    # response shape, where data.result is the canonical human-readable transcript.
    # How: prefer data.result before the legacy structure-based registry. Purpose:
    # keep model-visible tool history stable while structured fields move under data.
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return "text", data["result"]

    # [AutoC 2026-05-31] Why: result formatting previously depended on hard-coded
    # tool-name branches, so compatible tools with the same return structure could
    # not reuse readable renderers. How: create a formatting context, route through
    # the structure-based formatter registry, and use the preserved JSON fallback
    # only when no formatter matches. Purpose: keep the old public signature while
    # allowing tool_spec to opt into explicit result_format routing.
    ctx = ToolResultFormatContext(tool_name=tool_name, tool_spec=tool_spec)
    formatted = format_tool_result_by_structure(result, ctx)
    if formatted is not None:
        return formatted
    return json_fallback(result)


def format_tool_trace(entries: list[dict[str, Any]]) -> str:
    """把一批工具调用结果格式化为 CLONOTH_TOOL_TRACE 块。

    v2: 简化字段名，减少冗余前缀。
    """
    lines = ["[CLONOTH_TOOL_TRACE v2]"]
    for e in entries:
        lines.append(f"TOOL: {e['name']} {json.dumps(e.get('args', {}), ensure_ascii=False)}")
        lines.append(f"RESULT_FORMAT: {e.get('format', 'json')}")
        if e.get("truncated"):
            lines.append("RESULT_TRUNCATED: true")
        if e.get("ref"):
            lines.append(f"RESULT_REF: {e['ref']}")
        raw = e.get("raw_inline", "")
        if raw:
            lines.append("RESULT:")
            for ln in raw.splitlines():
                lines.append("  " + ln)
        else:
            lines.append("RESULT: <empty>")
        atts = e.get("attachments")
        if isinstance(atts, list) and atts:
            att_paths = [str(a.get('path', '')) for a in atts if isinstance(a, dict) and a.get('path')]
            if att_paths:
                lines.append(f"ATTACHMENTS: {', '.join(att_paths)}")
        lines.append("---")
    lines.append("[/CLONOTH_TOOL_TRACE]")
    return "\n".join(lines)
