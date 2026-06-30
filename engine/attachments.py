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
# [2026-04-22] 放宽为 data/ 前缀，使 data/ 下的非 attachments 文件（如 data/news_raw_*.md、
# data/chat_messages_*.json）也能作为附件传递。安全风险可控——仍限制在 workspace 的 data/ 内，
# 不会暴露源码或配置文件。
_ALLOWED_PREFIX = "data/"

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


def _guess_mime_from_path(path: str) -> str:
    """根据路径扩展名猜测 MIME。

    仅用于 LLM 图片 base64 编码场景，未知扩展名仍兜底为 image/png。
    """
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
    # 清洗无意义的 MIME（如 Discord CDN 返回的 application/octet-stream）
    mime_type = _sanitize_mime(mime_type)

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


def attachments_to_content_parts(
    attachments: list[dict[str, Any]],
    workspace_root: Path | None = None,  # [2026-04-22] 用于解析文本文件的完整路径并读取内容
) -> list[dict[str, Any]]:
    """Convert attachment dicts to OpenAI multimodal content parts.

    Images use file:// references (resolved to base64 later by prepare_messages_for_llm).
    Text files (<=100KB) are read and injected inline; larger files get metadata-only reference.
    Only paths under data/ are accepted.
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

        att_type = str(att.get("type") or "").strip()

        # [2026-04-22] Text files: read content inline if <=100KB, otherwise metadata-only.
        # 之前只注入一行元数据引用，LLM 无法直接看到文件内容，需要额外调用 read_file。
        # 改为：小文件直接注入完整内容到上下文，减少不必要的工具调用轮次。
        # 大文件（>100KB）保持 metadata-only，避免撑爆上下文窗口。
        _TEXT_FILE_MAX_BYTES = 102400  # 100KB
        if att_type == "file":
            name = att.get("name") or Path(path).name
            mime = att.get("mime_type") or "text/plain"
            metadata = f"[Attached file: {name} | type: {mime} | path: {path}]"

            # 尝试读取文件内容（需要 workspace_root 来解析相对路径）
            if workspace_root is not None:
                full_path = workspace_root / path
                try:
                    file_size = full_path.stat().st_size
                    if file_size <= _TEXT_FILE_MAX_BYTES:
                        # 小文件：注入元数据 + 完整内容
                        file_content = full_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        parts.append({
                            "type": "text",
                            "text": f"{metadata}\n---\n{file_content}\n---",
                        })
                    else:
                        # 大文件：metadata-only + 大小提示
                        size_kb = file_size / 1024
                        parts.append({
                            "type": "text",
                            "text": (
                                f"{metadata}\n"
                                f"(File too large to inject: {size_kb:.1f}KB > 100KB limit. "
                                f"Use read_file to inspect content.)"
                            ),
                        })
                except Exception as exc:
                    # 读取失败（文件不存在、权限等）：metadata-only + 错误提示
                    _logger.warning("failed to read attached file %s: %s", path, exc)
                    parts.append({
                        "type": "text",
                        "text": (
                            f"{metadata}\n"
                            f"(Failed to read file content: {exc})"
                        ),
                    })
            else:
                # 没有 workspace_root，退回 metadata-only 行为
                parts.append({
                    "type": "text",
                    "text": metadata,
                })
            continue

        # Images: create file:// reference for later resolution
        url = f"{_FILE_SCHEME}{path}"
        parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })
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
    # Collect image paths from attachments for inline annotation
    _img_paths = [
        str(a.get("path", ""))
        for a in attachments
        if str(a.get("type", "")).strip() != "file" and a.get("path")
    ]
    _annotated = text
    if _img_paths:
        _annotated = text + "\n[Attached images: " + ", ".join(_img_paths) + "]"
    parts: list[dict[str, Any]] = [{"type": "text", "text": _annotated}]
    parts.extend(att_parts)
    return parts


def _strip_image_parts_for_assistant(content: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
    """从 assistant 消息的 content 中剥掉所有 image_url 部分。

    Claude API 不允许 assistant turn 包含 image block，会报：
    'image' blocks are not permitted within assistant turns.
    """
    text_parts = [p for p in content if isinstance(p, dict) and p.get("type") != "image_url"]
    if not text_parts:
        # 全是图片，返回占位文本
        return "[image attachment]"
    if len(text_parts) == 1 and text_parts[0].get("type") == "text":
        # 只剩一个 text part，展平为纯字符串
        return text_parts[0].get("text", "")
    return text_parts


def prepare_messages_for_llm(
    messages: list[dict[str, Any]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    """Return a copy of messages with file:// image refs resolved to base64 data URLs.

    Also strips image blocks from assistant messages (Claude API restriction).

    Uses a per-call cache keyed by relative path to avoid re-encoding the same
    image file multiple times within a single invocation.
    """
    cache: dict[str, str | None] = {}
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
        role = msg.get("role", "")

        if not isinstance(content, list):
            result.append(msg)
            continue

        # --- assistant 消息：剥掉所有 image_url 部分 ---
        if role == "assistant":
            has_image = any(
                isinstance(p, dict) and p.get("type") == "image_url"
                for p in content
            )
            if has_image:
                new_msg = dict(msg)
                new_msg["content"] = _strip_image_parts_for_assistant(content)
                result.append(new_msg)
                _logger.debug("stripped image blocks from assistant message")
                continue
            # assistant 消息没有 image_url，正常处理
            result.append(msg)
            continue

        # --- user / system 等消息：正常解析 file:// 引用 ---
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
                    else:
                        # [缺陷修复] 解析失败时注入占位文本，让 LLM 知道用户发过图片。
                        # 之前是完全静默跳过，LLM 完全不知道用户发了图片，导致回复不连贯。
                        # 原样传递 file:// URL 会导致 API 报 octet-stream 错误，所以不能保留原 URL。
                        _logger.warning("attachment resolve failed, injecting placeholder: %s", rel_path)
                        new_content.append({
                            "type": "text",
                            "text": "[Image unavailable]",
                        })
                    continue  # 无论成功失败都 continue，不 fallback 到 append(part)
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

        img = _PILImage.open(io.BytesIO(data))

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
            img = img.resize((int(w * scale), int(h * scale)), _PILImage.LANCZOS)

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


def _resolve_file_url(url: str, workspace_root: Path) -> str | None:
    """Resolve a file:// URL to a data: base64 URL.

    Images are automatically compressed / resized for LLM consumption.
    """
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
        data = p.read_bytes()
    except Exception:
        return None

    mime = _guess_mime_from_path(rel_path)

    # Compress / resize images (skip SVG)
    if mime.startswith("image/") and mime != "image/svg+xml":
        data, mime = _compress_image_for_llm(data, mime)

    if not data:
        _logger.warning("image compression returned empty data, skipping: %s", rel_path)
        return None

    if len(data) > _MAX_IMAGE_BYTES:
        _logger.warning(
            "image too large after compression (%d bytes), skipping: %s",
            len(data), rel_path,
        )
        return None

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"
