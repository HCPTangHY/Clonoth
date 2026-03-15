"""Attachment handling utilities for multimodal message support.

All image data in the system is stored as files under data/attachments/.
Messages reference images via file:// URLs (e.g. file://data/attachments/xxx/yyy.png).
Before sending to the LLM provider, file:// refs are resolved to base64 data URLs.
"""

from __future__ import annotations

import base64
import functools
import mimetypes
import uuid
from pathlib import Path
from typing import Any


_FILE_SCHEME = "file://"
_ALLOWED_PREFIX = "data/attachments/"

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# 单图最大 10 MB
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _guess_mime(ext: str) -> str:
    return _MIME_MAP.get(ext.lower(), "application/octet-stream")


def _guess_mime_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    return _MIME_MAP.get(ext, "image/png")


def _is_allowed_attachment_path(rel_path: str) -> bool:
    """Only allow paths under data/attachments/."""
    normalized = rel_path.replace("\\", "/").lstrip("/")
    return normalized.startswith(_ALLOWED_PREFIX)


def save_attachment(
    workspace_root: Path,
    session_id: str,
    data_bytes: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
) -> dict[str, Any]:
    """Save attachment bytes to data/attachments/{session_id}/. Returns attachment dict."""
    sid = (session_id or "unknown").strip() or "unknown"
    d = workspace_root / "data" / "attachments" / sid
    d.mkdir(parents=True, exist_ok=True)

    ext = ""
    if filename:
        ext = Path(filename).suffix.lower()
    if not ext and mime_type:
        ext = mimetypes.guess_extension(mime_type) or ""
    if not ext:
        ext = ".bin"

    name = f"{uuid.uuid4().hex}{ext}"
    p = d / name
    p.write_bytes(data_bytes)

    rel = p.relative_to(workspace_root).as_posix()
    detected_mime = mime_type or _guess_mime(ext)
    return {
        "type": "image" if detected_mime.startswith("image/") else "file",
        "path": rel,
        "mime_type": detected_mime,
        "name": filename or name,
    }


def attachments_to_content_parts(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert attachment dicts to OpenAI multimodal content parts (with file:// references).

    Only paths under data/attachments/ are accepted.
    """
    parts: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "").strip()
        if not path:
            continue
        if not _is_allowed_attachment_path(path):
            continue
        url = f"{_FILE_SCHEME}{path}"
        parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })
    return parts


def build_multimodal_content(
    text: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]] | str:
    """Build multimodal content for a message. Returns plain str if no image attachments."""
    image_parts = attachments_to_content_parts(attachments)
    if not image_parts:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(image_parts)
    return parts


def prepare_messages_for_llm(
    messages: list[dict[str, Any]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    """Return a copy of messages with file:// image refs resolved to base64 data URLs.

    Uses a per-call cache keyed by relative path to avoid re-encoding the same
    image file multiple times within a single invocation.
    """
    cache: dict[str, str | None] = {}
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        needs_resolve = any(
            isinstance(part, dict)
            and part.get("type") == "image_url"
            and isinstance(part.get("image_url"), dict)
            and str(part["image_url"].get("url", "")).startswith(_FILE_SCHEME)
            for part in content
        )

        if not needs_resolve:
            result.append(msg)
            continue

        new_msg = dict(msg)
        new_content: list[dict[str, Any]] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
            ):
                url = str(part["image_url"].get("url", ""))
                if url.startswith(_FILE_SCHEME):
                    rel_path = url[len(_FILE_SCHEME):]
                    if rel_path in cache:
                        resolved = cache[rel_path]
                    else:
                        resolved = _resolve_file_url(url, workspace_root)
                        cache[rel_path] = resolved
                    if resolved:
                        new_content.append({
                            "type": "image_url",
                            "image_url": {"url": resolved},
                        })
                        continue
            new_content.append(part)
        new_msg["content"] = new_content
        result.append(new_msg)

    return result


def _resolve_file_url(url: str, workspace_root: Path) -> str | None:
    """Resolve a file:// URL to a data: base64 URL."""
    rel_path = url[len(_FILE_SCHEME):]

    # Only allow paths under data/attachments/
    if not _is_allowed_attachment_path(rel_path):
        return None

    p = (workspace_root / rel_path).resolve()

    # Security: must be under workspace_root
    try:
        p.relative_to(workspace_root.resolve())
    except ValueError:
        return None

    if not p.exists() or not p.is_file():
        return None

    try:
        size = p.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            return None
        data = p.read_bytes()
    except Exception:
        return None

    mime = _guess_mime_from_path(str(p))
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"
