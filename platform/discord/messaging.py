"""Discord attachment, sending, progress, and restart helpers.

[2026-05-14 refactor note] This module was split out of ereuna_main.py so
message output and file handling share DiscordRuntime explicitly. The functions
avoid importing context.py to keep the requested one-way import structure.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import discord  # type: ignore[import-untyped]


_CST = datetime.timezone(datetime.timedelta(hours=8))
logger = logging.getLogger("ereuna_v2")


_IMAGE_EXT_MAP: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".jfif": "image/jpeg", ".tiff": "image/tiff",
    ".tif": "image/tiff", ".ico": "image/x-icon", ".avif": "image/avif",
}

_TEXT_EXT_MAP: dict[str, str] = {
    ".txt": "text/plain", ".md": "text/markdown", ".py": "text/x-python",
    ".js": "text/javascript", ".ts": "text/typescript", ".json": "application/json",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".xml": "text/xml",
    ".html": "text/html", ".css": "text/css", ".csv": "text/csv",
    ".log": "text/plain", ".sh": "text/x-shellscript", ".toml": "text/toml",
    ".ini": "text/plain", ".cfg": "text/plain", ".conf": "text/plain",
    ".c": "text/x-c", ".h": "text/x-c", ".cpp": "text/x-c++",
    ".java": "text/x-java", ".go": "text/x-go", ".rs": "text/x-rust",
    ".rb": "text/x-ruby", ".sql": "text/x-sql",
}


def _extract_reactions(rt: Any, text: str) -> tuple[str, list[str]]:
    """从文本中提取 [REACT:emoji] 标记，返回 (剥离后文本, emoji列表)。"""
    reactions = rt.react_pattern.findall(text)
    cleaned = rt.react_pattern.sub('', text).strip()
    return cleaned, reactions


def _resolve_image_mime(att_content_type: str | None, filename: str) -> str | None:
    if att_content_type and att_content_type.startswith("image/"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _IMAGE_EXT_MAP.get(ext)


def _resolve_text_mime(att_content_type: str | None, filename: str) -> str | None:
    if att_content_type and (att_content_type.startswith("text/") or att_content_type == "application/json"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _TEXT_EXT_MAP.get(ext)


async def _collect_attachments(
    rt: Any,
    message: discord.Message,
    conversation_key: str,
) -> list[dict[str, Any]]:
    """将消息及其引用中的附件保存到 data/attachments/，返回 Clonoth 附件列表。"""
    result: list[dict[str, Any]] = []
    all_atts = list(message.attachments)
    if message.reference and message.reference.resolved and hasattr(message.reference.resolved, "attachments"):
        all_atts.extend(message.reference.resolved.attachments)

    # --- 转发消息 (message_snapshots) 附件收集 ---
    # Discord 转发消息的图片附件存储在 snapshot 内部，普通 message.attachments
    # 为空，需要从 snapshot 额外提取，否则 agent 看不到转发消息里的图片。
    for src in [message, (getattr(message.reference, "resolved", None) if message.reference else None)]:
        if src is None:
            continue
        snaps = getattr(src, "message_snapshots", None)
        if snaps and len(snaps) > 0:
            snap = snaps[0]
            snap_msg = getattr(snap, "message", snap)
            snap_atts = getattr(snap_msg, "attachments", None) or getattr(snap, "attachments", None) or []
            all_atts.extend(snap_atts)

    logger.info("_collect_attachments: conv_key=%s, message.attachments=%d, all_atts=%d",
                conversation_key, len(message.attachments), len(all_atts))
    for i, att in enumerate(all_atts):
        logger.info("  att[%d]: filename=%s content_type=%s size=%s",
                    i, getattr(att, 'filename', '?'), getattr(att, 'content_type', '?'), getattr(att, 'size', '?'))
        mime = _resolve_image_mime(att.content_type, att.filename or "")
        att_type = "image"
        if not mime:
            mime = _resolve_text_mime(att.content_type, att.filename or "")
            att_type = "file"
        if not mime:
            continue
        try:
            file_bytes = await att.read()
            filename = att.filename or f"{os.urandom(16).hex()}{'.png' if att_type == 'image' else '.txt'}"
            if rt.clonoth_client:
                # Upload via Supervisor API (Docker-safe)
                uploaded = await rt.clonoth_client.upload_attachment(
                    file_bytes, filename,
                    conversation_key=conversation_key,
                    content_type=mime,
                )
                att_dict = {
                    "type": uploaded.get("type", att_type),
                    "path": uploaded["path"],
                    "mime_type": uploaded.get("mime_type", mime),
                    "name": uploaded.get("name", filename),
                }
            else:
                # Fallback: direct write (SDK not yet initialized)
                att_dir = rt.workspace / "data" / "attachments" / conversation_key.replace(":", "_")
                att_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(filename).suffix.lower() or (".png" if att_type == "image" else ".txt")
                fname = f"{os.urandom(16).hex()}{ext}"
                fpath = att_dir / fname
                fpath.write_bytes(file_bytes)
                rel_path = fpath.relative_to(rt.workspace).as_posix()
                att_dict = {
                    "type": att_type, "path": rel_path,
                    "mime_type": mime, "name": filename,
                }
            result.append(att_dict)
            logger.info("  att[%d]: SAVED -> %s (%d bytes)", i, att_dict["path"], len(file_bytes))
        except Exception as e:
            logger.warning("  att[%d]: FAILED to read/save %s: %s", i, getattr(att, 'filename', '?'), e)
    logger.info("_collect_attachments: returning %d items", len(result))
    return result


def _current_message_media_text(message: discord.Message) -> str:
    """Return a compact text marker for non-text Discord messages that are still valid input."""
    parts: list[str] = []
    if getattr(message, "stickers", None):
        names = ", ".join(s.name for s in message.stickers)
        parts.append(f"[贴纸: {names}]" if names else f"[贴纸×{len(message.stickers)}]")
    atts = list(getattr(message, "attachments", []) or [])
    if atts:
        img_count = sum(
            1 for a in atts
            if _resolve_image_mime(getattr(a, "content_type", None), getattr(a, "filename", "") or "")
        )
        other_count = max(0, len(atts) - img_count)
        if img_count:
            parts.append(f"[图片×{img_count}]")
        if other_count:
            parts.append(f"[附件×{other_count}]")
    if getattr(message, "embeds", None):
        parts.append(f"[嵌入内容×{len(message.embeds)}]")
    return " ".join(parts).strip()


def _truncate(text: str, limit: int = 2000) -> str:
    if len(text) > limit:
        return text[:limit - 4] + "..."
    return text


async def _send_split_text(
    rt: Any,
    channel: Any,
    text: str,
    *,
    reply_to: discord.Message | None = None,
    files: list[discord.File] | None = None,
) -> list[discord.Message]:
    """发送文本到频道，支持 [SPLIT] 分段和 2000 字符截断。"""
    sent: list[discord.Message] = []
    if rt.split_signal in text:
        parts = [p.strip() for p in text.split(rt.split_signal) if p.strip()]
        for i, part in enumerate(parts):
            part = _truncate(part)
            split_files = files if (i == 0 and files) else None
            if i == 0 and reply_to:
                sent.append(await reply_to.reply(content=part, files=split_files or [], suppress_embeds=True))
            else:
                sent.append(await channel.send(content=part, files=split_files or [], suppress_embeds=True))
            if i < len(parts) - 1:
                next_len = len(parts[i + 1])
                delay = min(0.8 + next_len / 80, 3.5)
                try:
                    async with channel.typing():
                        await asyncio.sleep(delay)
                except Exception:
                    await asyncio.sleep(delay)
    else:
        text = _truncate(text)
        if reply_to:
            sent.append(await reply_to.reply(content=text, files=files or [], suppress_embeds=True))
        else:
            sent.append(await channel.send(content=text, files=files or [], suppress_embeds=True))
    return sent


def _record_bot_reply(rt: Any, channel_id: int, text: str, *, msg_id: int | None = None) -> None:
    """将 Bot 回复记入频道历史。"""
    ts = datetime.datetime.now(_CST).strftime("%H:%M")
    # [2026-05-14 refactor note] This mirrors context._push_history locally to
    # keep messaging.py independent from context.py, as required by the split.
    q = rt.channel_history.setdefault(channel_id, [])
    record_text = f"[{ts}] Bot: {text}"
    if len(record_text) > 500:
        record_text = record_text[:497] + "..."
    q.append({
        "text": record_text,
        "message": None,
        "msg_id": msg_id,
        "reactions": {},
        "seq": rt.history_seq_counter,
        "is_bot": True,
    })
    rt.history_seq_counter += 1
    if len(q) > rt.history_max_len:
        q.pop(0)


def _atts_to_discord_files(rt: Any, attachments: list[dict[str, Any]]) -> list[discord.File]:
    """将 Clonoth 附件列表转换为 discord.File 列表。"""
    files: list[discord.File] = []
    for att in attachments:
        att_path = rt.workspace / (att.get("original_path") or att.get("path", ""))
        if att_path.exists():
            files.append(discord.File(str(att_path), filename=att.get("name", att_path.name)))
    return files


def _format_progress_log(
    prefix: str,
    lines: list[str],
    *,
    dot_state: dict[str, Any] | None = None,
    status: str = "",
    max_lines: int = 6,
) -> str:
    """统一进度日志渲染。主节点和子节点共用。"""
    if not lines and not dot_state:
        return ""

    log_block = ""
    if lines:
        display = lines[-max_lines:]
        start_idx = max(0, len(lines) - max_lines) + 1
        numbered = [f"{start_idx + i}| {l}" for i, l in enumerate(display)]
        log_block = "```\n" + "\n".join(numbered) + "\n```"

    anim_line = ""
    preview_block = ""
    if dot_state:
        if dot_state.get("had_stream_activity"):
            dot_state["dot_step"] = dot_state.get("dot_step", 0) + 1
            dot_state["had_stream_activity"] = False
        step = dot_state.get("dot_step", 0)
        phase = step % 10
        n_dots = phase + 1 if phase <= 5 else 11 - phase
        anim_line = "•" * n_dots

        start_t = dot_state.get("thinking_start_time", 0)
        if start_t > 0:
            elapsed = int(time.time() - start_t)
            anim_line += f" ⏱ {elapsed}s"

        retry_info = dot_state.get("retry_info", "")
        if retry_info:
            anim_line += f"\n{retry_info}"
            if dot_state.get("had_stream_activity"):
                dot_state["retry_info"] = ""

        # --- stream preview（thinking / text）始终显示 ---
        thinking_pv = dot_state.get("thinking_preview", "")
        text_pv = dot_state.get("text_preview", "")
        preview = thinking_pv or text_pv
        if preview:
            if "<<<TOOL_CALL>>>" in preview or "<<<END_TOOL_CALL>>>" in preview:
                preview = ""
        if preview:
            p_lines = preview.strip().split("\n")
            p_lines = [l for l in p_lines if l.strip()][-2:]
            if p_lines:
                p_lines = [l[:80].replace("`", "'") for l in p_lines]
                preview_block = "```\n" + "\n".join(p_lines) + "\n```"

        # --- 工具摘要 + free prose（追加在 stream preview 之后） ---
        prose_lines = dot_state.get("prose_lines") or []
        tool_lines = dot_state.get("tool_lines") or []
        activity_parts: list[str] = []
        for pl in prose_lines[-2:]:
            activity_parts.append(pl.replace("`", "'")[:80])
        for tl in tool_lines[-4:]:
            mark = "✓" if tl.get("done") and tl.get("ok") else "✗" if tl.get("done") else "…"
            activity_parts.append(f"  {tl.get('text', '')}  {mark}")
        if activity_parts:
            preview_block = (preview_block + "\n" if preview_block else "") + "\n".join(activity_parts)

    header = f"⏳ {prefix}:"
    parts = [header]
    if anim_line:
        parts.append(anim_line)
    if preview_block:
        parts.append(preview_block)
    if log_block:
        parts.append(log_block)
    if status:
        parts.append(status)

    text = "\n".join(parts)
    if len(text) > 2000:
        text = text[-2000:]
    return text


async def _safe_restart(rt: Any, channel_id: int = 0, delay: float = 3.0) -> None:
    """延迟后通过 pm2 restart 安全重启自身。"""
    if channel_id:
        try:
            notify_path = Path(f"/tmp/clonoth_restart_notify_{rt.bridge_port}.json")
            notify_path.write_text(json.dumps({"channel_id": channel_id}))
        except Exception:
            pass
    await asyncio.sleep(delay)
    if rt.session_state:
        for _rs_seq, _rs_trigger in list(rt.session_state.triggers.items()):
            try:
                status_msg = _rs_trigger.platform_data.get("status_msg")
                if status_msg:
                    await status_msg.edit(content="⚠️ Bot 正在重启...", view=None)
            except Exception:
                pass
        # [2026-05-14 bug fix] The original function referenced self outside a
        # class. It now reaches the callback helper through DiscordRuntime.
        callback_obj = rt.callbacks
        if callback_obj:
            for _rs_seq, _rs_state in list(rt.session_state.main_task_states.items()):
                await callback_obj._settle_log_msg(_rs_state, "⚠️ Bot 重启中")
            for _rs_key, _rs_child in list(rt.session_state.child_task_states.items()):
                if getattr(_rs_child, "is_dm", False):
                    msg = _rs_child.platform_data.get("msg")
                    if msg:
                        await msg.delete()
                else:
                    await callback_obj._settle_log_msg(_rs_child, "⚠️ Bot 重启中")
        rt.session_state.triggers.clear()
        rt.session_state.main_task_states.clear()
        rt.session_state.child_task_states.clear()
        rt.session_state.conversation_sessions.clear()
        rt.session_state.session_conv_map.clear()
    print("[bot] 收到安全重启信号，正在退出（PM2 将自动重启）...")
    sys.exit(0)
