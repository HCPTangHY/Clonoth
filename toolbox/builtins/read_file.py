"""read_file — read one or more files (text / image / binary).

Supports batch reading via ``files`` array parameter.
Backward-compatible with legacy single-file ``path`` parameter.
"""
from __future__ import annotations

import shutil
import uuid as _uuid
from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Map from sniffed MIME to canonical extension
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _sniff_image_mime(data: bytes) -> str | None:
    """Detect actual image MIME type from magic bytes."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 4 and data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    return None


# ---------------------------------------------------------------------------
#  Image dimension helpers (pure stdlib, no Pillow needed)
# ---------------------------------------------------------------------------

def _make_dimensions(w: int, h: int) -> dict[str, Any]:
    from math import gcd
    g = gcd(w, h) if w > 0 and h > 0 else 1
    return {"width": w, "height": h, "aspectRatio": f"{w // g}:{h // g}"}


def _parse_jpeg_dimensions(data: bytes) -> dict[str, Any] | None:
    import struct
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 9 <= len(data):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return _make_dimensions(w, h)
            break
        if marker in (0xD9, 0xDA):
            break
        if i + 4 <= len(data):
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
        else:
            break
    return None


def _get_image_dimensions(p: Path) -> dict[str, Any] | None:
    try:
        import struct
        data = p.read_bytes()
        ext = p.suffix.lower()
        if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return _make_dimensions(w, h)
        if ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
            return _parse_jpeg_dimensions(data)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
#  Single-file reader
# ---------------------------------------------------------------------------

async def _read_single_file(
    path_str: str,
    start_line: Any,
    end_line: Any,
    ctx: ToolContext,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read one file.  Returns ``(result_entry, multimodal_entry | None)``."""
    if not path_str:
        return {"path": "", "success": False, "error": "empty path"}, None

    _op, err = await request_guard(
        ctx, "read_file",
        {"path": path_str, "start_line": start_line, "end_line": end_line},
    )
    if err is not None:
        return {"path": path_str, "success": False, "error": err.get("error", "denied")}, None

    try:
        p = resolve_under_allowed_roots(ctx.workspace_root, path_str)
    except ValueError as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    if not p.exists() or not p.is_file():
        return {"path": path_str, "success": False, "error": "File not found"}, None

    ext = p.suffix.lower()

    # ---- multimodal (image) ----
    if ext in _IMAGE_EXTENSIONS:
        try:
            raw = p.read_bytes()
            size = len(raw)
            # Sniff actual MIME from magic bytes; fall back to extension
            mime = _sniff_image_mime(raw) or _MIME_MAP.get(ext, "application/octet-stream")
            real_ext = _MIME_TO_EXT.get(mime, ext)
            entry: dict[str, Any] = {
                "path": path_str, "success": True,
                "type": "multimodal", "mimeType": mime, "size": size,
            }
            dims = _get_image_dimensions(p)
            if dims:
                entry["dimensions"] = dims
            # Save a copy under data/attachments/ so the multimodal pipeline
            # can resolve it to a base64 data-URL for the LLM.
            att_dir = ctx.workspace_root / "data" / "attachments" / "read_file"
            att_dir.mkdir(parents=True, exist_ok=True)
            att_name = f"{_uuid.uuid4().hex}{real_ext}"
            att_path = att_dir / att_name
            shutil.copy2(p, att_path)
            att_rel = att_path.relative_to(ctx.workspace_root).as_posix()
            mm: dict[str, Any] = {
                "type": "image",
                "path": att_rel,
                "mime_type": mime,
                "name": p.stem + real_ext,
            }
            return entry, mm
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None

    # ---- text file ----
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            size = p.stat().st_size
            return {"path": path_str, "success": True, "type": "binary", "size": size}, None
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None
    except Exception as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    lines = text.splitlines()
    total_lines = len(lines)

    start_val = start_line if isinstance(start_line, int) and start_line > 0 else 1
    if isinstance(start_line, int) and start_line > total_lines:
        return {
            "path": path_str, "success": False,
            "error": f"startLine {start_line} exceeds total lines",
            "totalLines": total_lines,
        }, None

    s = start_val - 1
    if isinstance(end_line, int) and end_line >= start_val:
        e = min(end_line, total_lines)
    else:
        e = total_lines

    # [AutoC 2026-05-31] Why: 不指定行范围时大文件（如 1700 行的 task_router.py）
    # 全量返回 ~50KB，反复读取会快速耗尽上下文。
    # How: 未指定范围且超过阈值时自动截断并提示。
    # Purpose: 引导模型用 startLine/endLine 精确读取。
    _MAX_LINES_NO_RANGE = 500
    if not isinstance(start_line, int) and not isinstance(end_line, int) and total_lines > _MAX_LINES_NO_RANGE:
        e = _MAX_LINES_NO_RANGE
        _auto_truncated = True
    else:
        _auto_truncated = False

    sliced = lines[s:e]
    width = max(4, len(str(e)))
    numbered = "\n".join([f"{i + s + 1:>{width}} | {ln}" for i, ln in enumerate(sliced)])

    result: dict[str, Any] = {
        "path": path_str, "success": True, "type": "text",
        "content": numbered, "lineCount": len(sliced),
    }
    if isinstance(start_line, int) or isinstance(end_line, int):
        result["totalLines"] = total_lines
        result["startLine"] = s + 1
        result["endLine"] = e
    if _auto_truncated:
        result["truncated"] = True
        result["totalLines"] = total_lines
        result["shownLines"] = _MAX_LINES_NO_RANGE
        result["hint"] = f"File has {total_lines} lines but only first {_MAX_LINES_NO_RANGE} are shown. Use startLine/endLine to read specific sections."
    return result, None


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------


def _read_file_result_text(results: list[dict[str, Any]]) -> str:
    # [AutoC 2026-05-31] Why: read_file should expose the exact readable transcript
    # in data.result under the unified tool response schema. How: reuse the existing
    # section header format for text, image, binary, and error entries. Purpose:
    # keep model-visible file contents stable without relying on formatter fallback.
    parts: list[str] = []
    for r in results:
        p = r.get("path", "")
        if r.get("success") and r.get("type") == "text":
            c = r.get("content", "")
            if c:
                parts.append(f"── {p} ──\n{c}")
        elif r.get("success") and r.get("type") == "multimodal":
            parts.append(f"── {p} ── [image: {r.get('mimeType', '?')}, {r.get('size', 0)} bytes]")
        elif r.get("success") and r.get("type") == "binary":
            parts.append(f"── {p} ── [binary: {r.get('size', 0)} bytes]")
        elif not r.get("success"):
            parts.append(f"── {p} ── ERROR: {r.get('error', 'unknown')}")
    return "\n".join(parts) if parts else "No files read"


async def read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    files_arg = args.get("files")
    if isinstance(files_arg, list) and files_arg:
        file_entries = [e for e in files_arg if isinstance(e, dict)]
    else:
        # Legacy single-file mode
        path = str(args.get("path", ""))
        if not path:
            return {
                "ok": False, "success": False, "error": "no path specified",
                "data": {"result": "ERROR: no path specified", "results": [], "successCount": 0, "failCount": 0, "totalCount": 0, "multiRoot": False},
            }
        entry_dict: dict[str, Any] = {"path": path}
        if args.get("start_line") is not None:
            entry_dict["startLine"] = args["start_line"]
        if args.get("end_line") is not None:
            entry_dict["endLine"] = args["end_line"]
        file_entries = [entry_dict]

    if not file_entries:
        return {
            "ok": False, "success": False, "error": "empty files list",
            "data": {"result": "ERROR: empty files list", "results": [], "successCount": 0, "failCount": 0, "totalCount": 0, "multiRoot": False},
        }

    results: list[dict[str, Any]] = []
    multimodal_data: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for fe in file_entries:
        path_str = str(fe.get("path", "")).strip()
        sl = fe.get("startLine") if fe.get("startLine") is not None else fe.get("start_line")
        el = fe.get("endLine") if fe.get("endLine") is not None else fe.get("end_line")
        r, mm = await _read_single_file(path_str, sl, el, ctx)
        results.append(r)
        if r.get("success"):
            success_count += 1
        else:
            fail_count += 1
        if mm is not None:
            multimodal_data.append(mm)

    total_count = success_count + fail_count
    response: dict[str, Any] = {
        "ok": fail_count == 0,
        "success": fail_count == 0,
        "data": {
            "result": _read_file_result_text(results),
            "results": results,
            "successCount": success_count,
            "failCount": fail_count,
            "totalCount": total_count,
            "multiRoot": False,
        },
    }
    if fail_count > 0:
        response["error"] = f"{fail_count} file(s) failed to read"
    if multimodal_data:
        # [AutoC 2026-05-31] Why: attachment metadata now lives under data but must
        # be mirrored at the top level during migration. How: write both locations
        # with the same list. Purpose: keep existing attachment collectors working
        # while new readers consume data.attachments.
        response["data"]["attachments"] = multimodal_data
        response["attachments"] = multimodal_data

    # Backward compat: single text file → set top-level path/content
    if total_count == 1 and results:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success") and r0.get("type") == "text":
            response["content"] = r0.get("content", "")
        elif not r0.get("success"):
            response["error"] = r0.get("error", "read failed")

    return response
