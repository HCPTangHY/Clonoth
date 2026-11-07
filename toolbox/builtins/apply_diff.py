"""apply_diff — apply sequential search/replace diffs to a file (policy + approval guarded).

Each diff in the array is applied in order, each one operating on the content
produced by the previous one. The search string must match exactly (including
whitespace and indentation).

``start_line`` is always a line number of the *original* file, i.e. the file as it
was before the first diff of this batch ran. That is the only coordinate system a
caller can reason about: once an edit adds or removes lines, a post-edit line number
means something different from what was written. Each applied edit is recorded in
the coordinate frame it ran in and replayed in order, so a later start_line keeps
pointing at the same text across any number of insertions, deletions, replacements,
re-edits of previously inserted text, or bottom-to-top edit order.

If the search text is unique it is located by content alone, unless that only
occurrence ends before the mapped position — then the requested region was already
replaced or deleted by an earlier diff of this batch and must not fall back to a
different copy. When the text occurs more than once, start_line picks the first
occurrence at or after the mapped position; without start_line the diff is rejected
with the candidate original line numbers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = 1024 * 1024 * 64  # 64 MiB — generous but bounded
_MAX_CANDIDATES = 200  # 只限制错误信息里回显的候选行号数量，不限制实际定位


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _match_offsets(content: str, search: str, limit: int) -> list[int]:
    """search 在 content 中前 *limit* 个（不重叠）匹配的字符偏移。"""
    offsets: list[int] = []
    pos = 0
    while len(offsets) < limit:
        idx = content.find(search, pos)
        if idx == -1:
            break
        offsets.append(idx)
        pos = idx + max(1, len(search))
    return offsets


def _line_of(content: str, offset: int) -> int:
    """字符偏移所在的 1-based 行号。"""
    return content.count("\n", 0, offset) + 1


def _line_start_offsets(text: str) -> list[int]:
    """每一行起始字符偏移；index 0 即第 1 行。"""
    starts = [0]
    for idx, char in enumerate(text):
        if char == "\n":
            starts.append(idx + 1)
    return starts


# 已应用的一次编辑记录为 (cur_start, cur_end, new_len)：命中区间是**该次编辑执行时**
# 那份文本里的坐标，new_len 是替换文本长度。行号重放时从原始坐标出发，按应用顺序
# 逐个变换 —— 每次编辑都用自己的坐标系，所以「对上一编辑插入的文本再替换/删除」
# 不会把已计入的位移重复累加（那正是用原始坐标记录再反推起点时的经典错误）。
Edit = tuple[int, int, int]


def _current_offset(edits: list[Edit], offset: int) -> int:
    """原始文件字符偏移 → 当前（本批修改后）文本字符偏移。"""
    for start, end, new_len in edits:
        if offset >= end:
            offset += new_len - (end - start)
        elif offset >= start:
            # 目标文本已被这次编辑替换或删除：退到该编辑起点，不猜其内部对应关系
            # （也保证映射单调不减，可反查用于候选行号回显）。
            offset = start
    return offset


def _original_offset(edits: list[Edit], offset: int, original_len: int) -> int:
    """_current_offset 的反函数（映射单调不减）；仅用于候选行号的原始坐标回显。"""
    lo, hi = 0, original_len
    while lo < hi:
        mid = (lo + hi) // 2
        if _current_offset(edits, mid) < offset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _candidate_lines(content: str, search: str, orig_line_of: Callable[[int], int]) -> list[int]:
    """候选匹配的原始行号（仅回显，截断到 _MAX_CANDIDATES，不影响实际定位）。"""
    return [orig_line_of(offset) for offset in _match_offsets(content, search, _MAX_CANDIDATES)]


def _find_and_replace(
    content: str,
    search: str,
    replace: str,
    start_line: int | None,
    search_from: int | None,
    orig_line_of: Callable[[int], int],
) -> tuple[str, str | None, int | None, list[int] | None]:
    """Apply a single search/replace on *content*.

    Returns (new_content, error_message | None, match_offset | None, candidate_lines | None).
    error_message is None on success. match_offset is the character offset of the
    occurrence that was replaced, so the caller can record where the edit happened.
    candidate_lines holds original-file line numbers and is populated when the search
    text is ambiguous.

    *search_from* is the earliest character offset (in the current content) that the
    caller's original-file start_line maps to; None means "no usable line hint".
    """
    if not search:
        return content, "Empty search string is not allowed.", None, None

    # 改进1 & 2: 先做唯一性判断，并保留首处匹配位置
    first = content.find(search)
    if first == -1:
        return content, (
            "No exact match found. "
            "Please verify the content matches exactly."
        ), None, None

    if content.find(search, first + max(1, len(search))) == -1:
        # 改进1: 唯一匹配时忽略 start_line —— 即使调用方给了过期/越界的行号，
        # 只要 search 内容在文件中唯一，就直接替换，不因行号漂移而失败。
        if search_from is not None and first + len(search) <= search_from:
            # 例外：这唯一一处整体位于映射位置之前 —— 原始目标段落已被本批前序
            # diff 删除/替换，剩下的是「另一段」，不能拿它顶替。
            candidate_lines = _candidate_lines(content, search, orig_line_of)
            return content, (
                f"No exact match found at or after line {start_line} "
                "(start_line is resolved against the original file); the only "
                "remaining occurrence ends before it. "
                f"Candidate match lines: {', '.join(str(l) for l in candidate_lines)}."
            ), None, candidate_lines
        idx = first
    elif search_from is None:
        # 改进2: 多匹配时返回候选行号，帮助 AI 快速定位正确位置
        candidate_lines = _candidate_lines(content, search, orig_line_of)
        return content, (
            f"Multiple matches found ({content.count(search)}). "
            "Please provide 'start_line' parameter to specify which match to use. "
            f"Candidate match lines: {', '.join(str(l) for l in candidate_lines)}."
        ), None, candidate_lines
    else:
        # 只接受映射位置及其之后的匹配：更早出现的同文本属于另一个位置，
        # 这正是生产版「先命中 start_line 当前坐标」误改的根源。定位本身不设
        # 匹配数上限——第 201 处也可能是真实目标。
        idx = content.find(search, search_from)
        if idx == -1:
            candidate_lines = _candidate_lines(content, search, orig_line_of)
            return content, (
                f"No exact match found at or after line {start_line} "
                "(start_line is resolved against the original file). "
                f"Candidate match lines: {', '.join(str(l) for l in candidate_lines)}."
            ), None, candidate_lines

    new_content = content[:idx] + replace + content[idx + len(search):]
    return new_content, None, idx, None


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
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path_str, workspace=ctx.workspace)
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

    # 改进3: start_line 一律解析为「本批修改开始前的原始文件行号」。每次编辑按
    # 「它执行时」的字符区间记录，行号经 _current_offset 逐条重放后映射到当前文本；
    # 插入/删除/替换/再编辑插入文本/先改下方再改上方都能正确累加，不存在坐标混用。
    edits: list[Edit] = []
    original_text = content
    original_line_starts = _line_start_offsets(original_text)

    def _orig_line_of(offset: int) -> int:
        return _line_of(
            original_text,
            _original_offset(edits, offset, len(original_text)),
        )

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

        # 改进3: 原始行号 → 当前文本的字符偏移。唯一 search 不依赖行号，
        # 因此行号越界只在「多匹配又必须靠行号定位」时才是硬错误。
        search_from: int | None = None
        if start_line is not None:
            if 1 <= start_line <= len(original_line_starts):
                search_from = _current_offset(edits, original_line_starts[start_line - 1])
            elif content.count(search) > 1:
                failed_count += 1
                failed_diffs.append({
                    "index": i,
                    "error": (
                        f"start_line {start_line} is out of range "
                        f"(original file has {len(original_line_starts)} lines)."
                    ),
                })
                continue

        new_content, error, match_offset, candidate_lines = _find_and_replace(
            content, search, replace, start_line, search_from, _orig_line_of,
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

        # 改进3: 记录本次命中的当前文本字符区间（即这次编辑自己的坐标系），
        # 后续行号重放即可正确累加，包括对上一编辑插入文本的再修改。
        if match_offset is not None:
            edits.append((match_offset, match_offset + len(search), len(replace)))

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
