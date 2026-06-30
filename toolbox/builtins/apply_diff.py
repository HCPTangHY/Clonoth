"""apply_diff — apply sequential search/replace diffs to a file (policy + approval guarded).

Each diff in the array is applied in order. The search string must match exactly
(including whitespace and indentation). If start_line is omitted and the search
string appears more than once, the diff is rejected.

After diffs[0] is applied, diffs[1] operates on the *resulting* content, and so on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = 1024 * 1024 * 64  # 64 MiB — generous but bounded


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _find_match_lines(content: str, search: str, limit: int = 20) -> list[int]:
    """找出 search 在 content 中所有匹配位置的起始行号(1-based)。

    改进2辅助函数：在多匹配场景下，提供每个匹配的行号候选列表，
    帮助 AI 快速定位正确的 start_line，而不是只返回匹配数量。
    """
    lines: list[int] = []
    start = 0
    while len(lines) < limit:
        idx = content.find(search, start)
        if idx == -1:
            break
        line_no = content[:idx].count('\n') + 1
        lines.append(line_no)
        start = idx + max(1, len(search))
    return lines


def _find_and_replace(
    content: str,
    search: str,
    replace: str,
    start_line: int | None,
) -> tuple[str, str | None, list[int] | None]:
    """Apply a single search/replace on *content*.

    Returns (new_content, error_message | None, candidate_lines | None).
    error_message is None on success.
    candidate_lines is populated when multiple matches exist without a valid start_line.
    """
    if not search:
        return content, "Empty search string is not allowed.", None

    # 改进1 & 2: 先做全局计数，用于唯一匹配优先判断和候选行号收集
    count = content.count(search)

    if start_line is not None:
        # 改进1: 唯一匹配时忽略 start_line —— 即使 AI 给了过期的行号，
        # 只要 search 内容在文件中唯一，就直接替换，不因行号偏移而失败。
        if count == 1:
            new_content = content.replace(search, replace, 1)
            return new_content, None, None

        if count == 0:
            return content, (
                "No exact match found. "
                "Please verify the content matches exactly."
            ), None

        # count > 1: 多匹配场景，使用 start_line 定位（原有逻辑）
        lines = content.split("\n")
        if start_line < 1 or start_line > len(lines):
            return content, (
                f"start_line {start_line} is out of range "
                f"(file has {len(lines)} lines)."
            ), None

        # Build the prefix (lines before start_line) and the search region.
        prefix = "\n".join(lines[: start_line - 1])
        region = "\n".join(lines[start_line - 1:])

        idx = region.find(search)
        if idx == -1:
            return content, (
                "No exact match found starting from line "
                f"{start_line}. Please verify the content matches exactly."
            ), None

        # Replace the *first* occurrence in region.
        new_region = region[:idx] + replace + region[idx + len(search):]
        separator = "\n" if prefix else ""
        new_content = prefix + separator + new_region
        return new_content, None, None

    # Global search: require unique match.
    if count == 0:
        return content, (
            "No exact match found. "
            "Please verify the content matches exactly."
        ), None
    if count > 1:
        # 改进2: 多匹配时返回候选行号，帮助 AI 快速定位正确位置
        candidate_lines = _find_match_lines(content, search)
        return content, (
            f"Multiple matches found ({count}). "
            "Please provide 'start_line' parameter to specify which match to use. "
            f"Candidate match lines: {', '.join(str(l) for l in candidate_lines)}."
        ), candidate_lines

    new_content = content.replace(search, replace, 1)
    return new_content, None, None


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------


def _apply_diff_result_text(path: str, applied: int, failed: int) -> str:
    # [AutoC 2026-05-31] Why: apply_diff now needs a canonical readable transcript
    # in data.result. How: use the same compact path/applied/rejected summary that
    # existing formatter tests expect. Purpose: keep tool history stable while the
    # structured payload moves under data.
    return f"apply_diff on {path}: {applied} applied, {failed} rejected"


def _apply_diff_error_response(message: Any, path: str = "", **data_fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: every apply_diff failure should include ok=false and
    # data.result, including validation and policy failures that occur before counts
    # exist. How: normalize the error and add any known path/count details under
    # data. Purpose: make failed diffs readable and schema-consistent.
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    if path:
        data["file"] = path
    data.update(data_fields)
    return {"ok": False, "success": False, "error": text, "data": data}


async def apply_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path_str = str(args.get("path") or "").strip()
    diffs = args.get("diffs")

    # ------------------------------------------------------------------
    #  Basic validation
    # ------------------------------------------------------------------
    if not path_str:
        return _apply_diff_error_response("'path' is required.")

    if not isinstance(diffs, list) or len(diffs) == 0:
        return _apply_diff_error_response("'diffs' must be a non-empty array.", path_str)

    # ------------------------------------------------------------------
    #  Policy + approval guard (same flow as write_file)
    # ------------------------------------------------------------------
    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path_str,
            "content_preview": f"apply_diff: {len(diffs)} diff(s)",
            "content_len": 0,
            "tool_name": "apply_diff",
        },
    )
    if err is not None:
        cancelled = err.get("cancelled", False)
        if cancelled:
            response = _apply_diff_error_response(
                "Diff was cancelled by user",
                path_str,
                detail=f"Diff for {path_str} was cancelled by user.",
                status="rejected",
                diffCount=len(diffs),
                appliedCount=0,
                failedCount=0,
            )
            response["cancelled"] = True
            response["data"]["cancelled"] = True
            return response
        return _apply_diff_error_response(err.get("error", "denied"), path_str)

    # ------------------------------------------------------------------
    #  Resolve & read file
    # ------------------------------------------------------------------
    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path_str)
    except ValueError as exc:
        return _apply_diff_error_response(str(exc), path_str)

    if not resolved.exists() or not resolved.is_file():
        return _apply_diff_error_response(f"File not found: {path_str}", path_str)

    file_size = resolved.stat().st_size
    if file_size > _MAX_FILE_SIZE:
        return _apply_diff_error_response(
            f"File is too large ({file_size} bytes). Maximum editable size is {_MAX_FILE_SIZE} bytes.",
            path_str,
            fileSize=file_size,
            maxFileSize=_MAX_FILE_SIZE,
        )

    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _apply_diff_error_response("File is not valid UTF-8 text.", path_str)
    except Exception as exc:
        return _apply_diff_error_response(f"Failed to read file: {exc}", path_str)

    # ------------------------------------------------------------------
    #  Apply diffs sequentially
    # ------------------------------------------------------------------
    applied_count = 0
    failed_count = 0
    failed_diffs: list[dict[str, Any]] = []

    # 改进3: lineDelta 行号偏移维护 —— 前面的 diff 增删行后，后续 diff
    # 的 start_line 需要相应调整，避免因行号漂移导致定位失败。
    line_delta = 0

    for i, diff_entry in enumerate(diffs):
        if not isinstance(diff_entry, dict):
            failed_count += 1
            failed_diffs.append({"index": i, "error": "diff entry must be an object."})
            continue

        search = diff_entry.get("search")
        replace = diff_entry.get("replace")

        if search is None:
            failed_count += 1
            failed_diffs.append({"index": i, "error": "'search' is required."})
            continue
        if replace is None:
            failed_count += 1
            failed_diffs.append({"index": i, "error": "'replace' is required."})
            continue

        search = str(search)
        replace = str(replace)

        start_line_raw = diff_entry.get("start_line")
        start_line: int | None = None
        if start_line_raw is not None:
            try:
                start_line = int(start_line_raw)
            except (TypeError, ValueError):
                failed_count += 1
                failed_diffs.append({
                    "index": i,
                    "error": f"'start_line' must be an integer, got {type(start_line_raw).__name__}.",
                })
                continue

        # 改进3: 用累积的 line_delta 调整 start_line，补偿前序 diff 的行数增减
        adjusted_start_line = start_line
        if start_line is not None:
            adjusted_start_line = start_line + line_delta

        new_content, error, candidate_lines = _find_and_replace(
            content, search, replace, adjusted_start_line,
        )
        if error is not None:
            failed_count += 1
            fail_entry: dict[str, Any] = {"index": i, "error": error}
            # 改进2: 在 failed_diffs 条目中附带候选行号，方便 AI 重试时选择正确行
            if candidate_lines is not None:
                fail_entry["candidateLines"] = candidate_lines
            failed_diffs.append(fail_entry)
            # Don't update content — skip this diff and continue with next.
            continue

        # 改进3: 计算本次 diff 的行数变化并累加到 line_delta
        old_line_count = search.count('\n') + 1
        new_line_count = replace.count('\n') + 1
        line_delta += new_line_count - old_line_count

        content = new_content
        applied_count += 1

    diff_count = len(diffs)

    # ------------------------------------------------------------------
    #  All diffs failed → return error, do NOT write
    # ------------------------------------------------------------------
    if applied_count == 0:
        first_error = failed_diffs[0]["error"] if failed_diffs else "unknown"
        return _apply_diff_error_response(
            f"Failed to apply any diffs: {first_error}",
            path_str,
            detail=f"Failed to apply any diffs to {path_str}.",
            failedDiffs=failed_diffs,
            appliedCount=0,
            totalCount=diff_count,
            failedCount=failed_count,
        )

    # ------------------------------------------------------------------
    #  At least one diff succeeded → write result to disk
    # ------------------------------------------------------------------
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except Exception as exc:
        return _apply_diff_error_response(
            f"Failed to write file: {exc}",
            path_str,
            appliedCount=applied_count,
            failedCount=failed_count,
        )

    # ------------------------------------------------------------------
    #  Build response
    # ------------------------------------------------------------------
    if failed_count == 0:
        message = f"Diff applied and saved to {path_str}"
    else:
        message = (
            f"Partially applied diffs to {path_str}: "
            f"{applied_count} succeeded, {failed_count} failed. "
            f"Saved successfully."
        )

    data: dict[str, Any] = {
        "result": _apply_diff_result_text(path_str, applied_count, failed_count),
        "file": path_str,
        "message": message,
        "status": "accepted",
        "diffCount": diff_count,
        "appliedCount": applied_count,
        "failedCount": failed_count,
    }
    if failed_diffs:
        data["failedDiffs"] = failed_diffs

    # [AutoC 2026-05-31] Why: successful apply_diff results should expose ok=true
    # and data.result like all other tools. How: keep the legacy success flag while
    # adding the unified ok field. Purpose: preserve compatibility and schema
    # consistency during migration.
    return {"ok": True, "success": True, "data": data}
