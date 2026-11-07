"""Attachment handling utilities for multimodal message support.

All image data in the system is stored as files under data/attachments/.
Messages reference images via file:// URLs (e.g. file://data/attachments/xxx/yyy.png).
Before sending to the LLM provider, file:// refs are resolved to base64 data URLs.
"""
from __future__ import annotations

import base64
import io
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any


_logger = logging.getLogger(__name__)

_FILE_SCHEME = "file://"
# Attachment reads are restricted to the resolved workspace data/ directory.
_RASTER_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MIME_TO_EXT = {mime: ext for ext, mime in _RASTER_MIME_MAP.items()}
_MIME_TO_EXT["image/jpeg"] = ".jpg"
_MIME_MAP = {**_RASTER_MIME_MAP, ".svg": "image/svg+xml"}

# Restrict Pillow inspection to the raster formats used by the image channel.
_ALLOWED_PIL_DECODERS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")

# Bound source reads as well as the compressed image sent to the provider.
_MAX_SOURCE_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_TEXT_FILE_MAX_BYTES = 100 * 1024

# LLM 图片压缩参数
_MAX_LONG_EDGE = 1024
_JPEG_QUALITY = 85

# Discord CDN 等来源经常返回无意义的 MIME，需要清洗掉以便 fallback 到扩展名猜测
_USELESS_MIMES = frozenset({
    "application/octet-stream",
    "binary/octet-stream",
})


def _sanitize_mime(mime_type: str) -> str:
    """清洗无意义的 MIME 类型，返回空字符串以触发扩展名猜测。"""
    if not mime_type or mime_type.strip() in _USELESS_MIMES:
        return ""
    return mime_type.strip()


def _guess_mime(ext: str) -> str:
    """根据扩展名猜测 MIME。

    已知图片扩展名走 _MIME_MAP；其余走 stdlib mimetypes；
    都猜不出的兜底为 application/octet-stream。
    """
    lower = ext.lower()
    if lower in _MIME_MAP:
        return _MIME_MAP[lower]
    guessed = mimetypes.guess_type(f"file{lower}")[0]
    return guessed or "application/octet-stream"


def _detect_magic_raster_mime(data: bytes) -> str | None:
    """仅根据文件头 Magic Bytes 识别五大主流栅格位图 MIME，不匹配一律返回 None。"""
    if len(data) < 4:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 14 and data[:2] == b"BM":
        return "image/bmp"
    return None


def inspect_raster_image_bytes(data: bytes) -> tuple[str | None, str]:
    """校验字节流是否为合法且可被 Pillow 解码的栅格位图。

    返回 (real_mime_type, error_reason)：
    - 校验通过：返回 (real_mime_type, "")
    - 校验失败：返回 (None, 具体错误原因)
    """
    if not data:
        return None, "empty file (0 bytes)"
    magic_mime = _detect_magic_raster_mime(data)
    if not magic_mime:
        head = data[:64].lstrip()
        if head.startswith((b"<svg", b"<?xml")) or b"<svg" in data[:512].lower():
            return None, "SVG/XML vector text is not a raster bitmap image"
        if head.startswith((b"<!doctype html", b"<html", b"<!DOCTYPE html", b"<HTML")):
            return None, "file content is an HTML page, not a valid image"
        if head.startswith((b"{", b"[")):
            return None, "file content is JSON text, not a valid image"
        return None, "magic bytes do not match any supported raster image (PNG/JPEG/GIF/WEBP/BMP)"

    try:
        from PIL import Image as _PILImage

        with _PILImage.open(io.BytesIO(data), formats=list(_ALLOWED_PIL_DECODERS)) as img:
            w, h = img.size
            if w <= 0 or h <= 0:
                return None, f"invalid image dimensions ({w}x{h})"
            img.verify()
        return magic_mime, ""
    except Exception as exc:
        return None, f"corrupted or undecodable image ({exc})"


def _is_claimed_raster_image(ext: str, declared_mime: str, declared_type: str) -> bool:
    """Return whether attachment metadata claims a raster image, excluding SVG."""
    ext = ext.lower()
    mime = declared_mime.lower().strip()
    if ext == ".svg" or mime == "image/svg+xml":
        return False
    return (
        ext in _RASTER_MIME_MAP
        or ext in {".apng", ".avif", ".heic", ".heif"}
        or mime.startswith("image/")
        or declared_type.lower().strip() == "image"
    )


def _is_allowed_attachment_path(rel_path: str) -> bool:
    """Accept relative data/ paths; resolved containment is checked before I/O."""
    path = Path(rel_path)
    return (
        not path.is_absolute()
        and path.parts[:1] == ("data",)
        and ".." not in path.parts
        and "\\" not in rel_path
    )


def _resolve_attachment_path(rel_path: str, workspace_root: Path) -> Path:
    """Reject traversal and symlinks that leave the workspace data/ directory."""
    if not _is_allowed_attachment_path(rel_path):
        raise ValueError("path is outside allowed data/ directory")
    root = workspace_root.resolve()
    path = (root / rel_path).resolve()
    if not path.is_relative_to(root / "data"):
        raise ValueError("path escapes workspace data/ directory")
    return path


def _read_image_bytes(path: Path) -> bytes:
    """Bound both the initial size and the read if a source grows concurrently."""
    if path.stat().st_size > _MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("file size exceeds 25MiB limit")
    with path.open("rb") as source:
        data = source.read(_MAX_SOURCE_IMAGE_BYTES + 1)
    if len(data) > _MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("file size exceeds 25MiB limit")
    return data


def save_attachment(
    workspace_root: Path,
    session_id: str,
    data_bytes: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
) -> dict[str, Any]:
    """Save bytes under data/attachments/ and classify images by their real bytes."""
    mime_type = _sanitize_mime(mime_type)
    ext = Path(filename).suffix.lower() if filename else ""
    if len(data_bytes) > _MAX_SOURCE_IMAGE_BYTES:
        real_mime, image_error = None, "file size exceeds 25MiB limit"
    else:
        real_mime, image_error = inspect_raster_image_bytes(data_bytes)
    if real_mime and (not ext or ext == ".bin"):
        ext = _MIME_TO_EXT[real_mime]
    if not ext:
        ext = mimetypes.guess_extension(mime_type) if mime_type else ""
    ext = ext or ".bin"

    sid = (session_id or "unknown").strip() or "unknown"
    name = f"{uuid.uuid4().hex}{ext}"
    rel = f"data/attachments/{sid}/{name}"
    path = _resolve_attachment_path(rel, workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data_bytes)

    detected_mime = real_mime or mime_type or _guess_mime(ext)
    is_svg = ext == ".svg" or detected_mime.lower() == "image/svg+xml"
    attachment: dict[str, Any] = {
        "type": "image" if real_mime else "file",
        "path": rel,
        "mime_type": detected_mime if real_mime or is_svg or not detected_mime.startswith("image/") else "application/octet-stream",
        "name": filename or name,
    }
    if not real_mime and _is_claimed_raster_image(ext, detected_mime, ""):
        attachment["invalid_image"] = True
        attachment["invalid_image_reason"] = image_error
    return attachment


def attachments_to_content_parts(
    attachments: list[dict[str, Any]],
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Build image references or inline text from attachments under data/.

    Raster images are checked before use; SVG is text. Without an explicit
    workspace, files stay metadata-only and image validation is deferred to
    prepare_messages_for_llm rather than reading the process working directory.
    """
    parts: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "").strip()
        if not path or not _is_allowed_attachment_path(path):
            continue

        name = att.get("name") or Path(path).name
        ext = Path(path).suffix.lower()
        declared_type = str(att.get("type") or "").strip()
        declared_mime = str(att.get("mime_type") or "").strip()
        invalid_flag = bool(att.get("invalid_image"))
        invalid_reason = str(att.get("invalid_image_reason") or "").strip()
        full_path = None
        if workspace_root is not None:
            try:
                full_path = _resolve_attachment_path(path, workspace_root)
            except (OSError, ValueError, RuntimeError) as exc:
                parts.append({"type": "text", "text": f"[Attachment unavailable | path: {path} | reason: {exc}]"})
                continue

        is_svg = ext == ".svg" or declared_mime.lower() == "image/svg+xml"
        claimed_image = _is_claimed_raster_image(ext, declared_mime, declared_type)
        att_type = "file" if is_svg or invalid_flag or (declared_type == "file" and not claimed_image) else "image"
        if is_svg:
            declared_mime = "image/svg+xml"
        elif att_type == "image" and full_path is not None:
            try:
                if not full_path.is_file():
                    raise FileNotFoundError("file does not exist on disk")
                real_mime, reason = inspect_raster_image_bytes(_read_image_bytes(full_path))
                if real_mime:
                    declared_mime = real_mime
                else:
                    att_type = "file"
                    invalid_flag = claimed_image
                    invalid_reason = reason
            except (OSError, ValueError) as exc:
                att_type = "file"
                invalid_flag = True
                invalid_reason = str(exc)

        if invalid_flag:
            reason = invalid_reason or "magic bytes or Pillow decode check failed"
            _logger.warning("invalid image attachment rejected: path=%s reason=%s", path, reason)
            parts.append({
                "type": "text",
                "text": (
                    f"[Invalid image attachment: {name} | path: {path} | reason: {reason}. "
                    f"This file was not sent to vision input; you can inspect or fix it at '{path}' using tools.]"
                ),
            })
            continue

        if att_type == "file":
            mime = declared_mime or _guess_mime(ext)
            metadata = f"[Attached file: {name} | type: {mime} | path: {path}]"
            if full_path is not None:
                try:
                    file_size = full_path.stat().st_size
                    if not full_path.is_file():
                        raise FileNotFoundError("file does not exist on disk")
                    if file_size > _TEXT_FILE_MAX_BYTES:
                        metadata += (
                            f"\n(File too large to inject: {file_size / 1024:.1f}KB > 100KB limit. "
                            "Use read_file to inspect content.)"
                        )
                    else:
                        with full_path.open("rb") as source:
                            data = source.read(_TEXT_FILE_MAX_BYTES + 1)
                        if len(data) > _TEXT_FILE_MAX_BYTES:
                            raise ValueError("file grew beyond the 100KB inline limit")
                        metadata += f"\n---\n{data.decode('utf-8', errors='replace')}\n---"
                except (OSError, ValueError) as exc:
                    _logger.warning("failed to read attached file %s: %s", path, exc)
                    metadata += f"\n(Failed to read file content: {exc})"
            parts.append({"type": "text", "text": metadata})
            continue

        parts.append({"type": "image_url", "image_url": {"url": f"{_FILE_SCHEME}{path}"}})
    return parts


def build_multimodal_content(
    text: str,
    attachments: list[dict[str, Any]],
    workspace_root: Path | None = None,
) -> list[dict[str, Any]] | str:
    """Build multimodal content for a message. Returns plain str if no attachment parts."""
    att_parts = attachments_to_content_parts(attachments, workspace_root=workspace_root)
    if not att_parts:
        return text
    # Annotate only paths that actually produced image references.
    _img_paths = [
        str(part["image_url"]["url"])[len(_FILE_SCHEME):]
        for part in att_parts
        if part.get("type") == "image_url"
        and str(part["image_url"]["url"]).startswith(_FILE_SCHEME)
    ]
    _annotated = text
    if _img_paths:
        _annotated = text + "\n[Attached images: " + ", ".join(_img_paths) + "]"
    parts: list[dict[str, Any]] = [{"type": "text", "text": _annotated}]
    parts.extend(att_parts)
    return parts


def prepare_messages_for_llm(
    messages: list[dict[str, Any]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    """Return a copy of messages with file:// image refs resolved to base64 data URLs.

    Media restrictions belong to each provider; retain images in every role.

    Uses a per-call cache keyed by relative path to avoid re-encoding the same
    image file multiple times within a single invocation.
    """
    cache: dict[str, tuple[str | None, str]] = {}
    result: list[dict[str, Any]] = []
    for msg in messages:
        # [2026-05-01] Keep _meta until the provider layer runs. Native
        # providers, especially OpenAI Responses, need provider metadata to
        # round-trip raw output/reasoning items and rebuild native tool history.
        # Other transient internal flags are still removed here; providers are
        # responsible for never copying _meta into the final API payload.
        if any(k.startswith("_") and k != "_meta" for k in msg):
            msg = {k: v for k, v in msg.items() if not k.startswith("_") or k == "_meta"}

        content = msg.get("content")

        if not isinstance(content, list):
            result.append(msg)
            continue

        # Resolve local image references without changing the message role.
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
                    if rel_path not in cache:
                        cache[rel_path] = _resolve_file_url(url, workspace_root)
                    resolved, reason = cache[rel_path]
                    if resolved:
                        new_content.append({
                            **part,
                            "image_url": {**part["image_url"], "url": resolved},
                        })
                    else:
                        _logger.warning("attachment resolve failed: path=%s reason=%s", rel_path, reason)
                        new_content.append({
                            "type": "text",
                            "text": (
                                f"[Invalid or unavailable image attachment | path: {rel_path} | "
                                f"reason: {reason}. You can inspect or fix this file at '{rel_path}' using tools.]"
                            ),
                        })
                    continue
            new_content.append(part)  # 非 file:// 的 part 原样保留
        new_msg["content"] = new_content
        result.append(new_msg)

    return result


def _compress_image_for_llm(data: bytes, mime: str) -> tuple[bytes, str]:
    """Compress / resize an image for LLM consumption.

    - GIF: extract first frame
    - Long edge > _MAX_LONG_EDGE: resize proportionally
    - Convert to JPEG
    Returns (compressed_bytes, mime_type).
    """
    try:
        from PIL import Image as _PILImage

        img = _PILImage.open(io.BytesIO(data), formats=list(_ALLOWED_PIL_DECODERS))

        # GIF / animated: take first frame only
        if getattr(img, "is_animated", False) or img.format == "GIF":
            img.seek(0)

        # Convert to RGB (handle RGBA / palette transparency / etc.)
        if img.mode != "RGB":
            if "A" in img.mode or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            else:
                img = img.convert("RGB")

        # Resize if long edge exceeds limit
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > _MAX_LONG_EDGE:
            scale = _MAX_LONG_EDGE / long_edge
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), _PILImage.LANCZOS)

        # Encode as JPEG
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        result = buf.getvalue()

        # If still huge, progressively lower quality
        if len(result) > _MAX_IMAGE_BYTES:
            for q in (70, 50, 30):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=q, optimize=True)
                result = buf.getvalue()
                if len(result) <= _MAX_IMAGE_BYTES:
                    break

        _logger.debug(
            "image compressed: %d -> %d bytes, %dx%d",
            len(data), len(result), img.size[0], img.size[1],
        )
        return result, "image/jpeg"
    except Exception as e:
        _logger.warning("image compression failed, skipping image: %s", e)
        return b"", mime


def _resolve_file_url(url: str, workspace_root: Path) -> tuple[str | None, str]:
    """Resolve a local raster image to (data URL, failure reason)."""
    if not url.startswith(_FILE_SCHEME):
        return None, "expected a file:// image reference"
    rel_path = url[len(_FILE_SCHEME):]
    try:
        path = _resolve_attachment_path(rel_path, workspace_root)
        if not path.is_file():
            return None, "file does not exist on disk"
        data = _read_image_bytes(path)
    except (OSError, ValueError, RuntimeError) as exc:
        return None, str(exc)

    real_mime, reason = inspect_raster_image_bytes(data)
    if not real_mime:
        return None, reason
    data, mime = _compress_image_for_llm(data, real_mime)
    if not data:
        return None, "Pillow image compression/re-encoding failed"
    if len(data) > _MAX_IMAGE_BYTES:
        return None, "compressed image size exceeds 10MiB limit"

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", ""
