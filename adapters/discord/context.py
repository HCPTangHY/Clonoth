"""Discord text context and channel-history helpers.

[2026-05-14 refactor note] This module was split out of the legacy entry point so that
Discord identity cleanup, history storage, reaction bookkeeping, and reply
context extraction can receive the shared DiscordRuntime object instead of
reading or mutating module-level globals.
"""
from __future__ import annotations

import datetime
from collections import deque
import re
from pathlib import Path
from typing import Any

import discord  # type: ignore[import-untyped]


_CST = datetime.timezone(datetime.timedelta(hours=8))

# [2026-05-14 refactor note] The small image resolver is kept local to this
# module so context.py and messaging.py do not import each other. The mapping is
# intentionally identical to the attachment resolver used when saving files.
_CONTEXT_IMAGE_EXT_MAP: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".jfif": "image/jpeg", ".tiff": "image/tiff",
    ".tif": "image/tiff", ".ico": "image/x-icon", ".avif": "image/avif",
}


def _context_image_mime(att_content_type: str | None, filename: str) -> str | None:
    """Resolve image MIME type for history and forwarded-message previews."""
    if att_content_type and att_content_type.startswith("image/"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _CONTEXT_IMAGE_EXT_MAP.get(ext)


def _sanitize_name(name: str, max_len: int = 32) -> str:
    """清洗用户名/身份组名：去换行、去结构字符、限长度。"""
    name = name.replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")")
    name = name.strip()
    if len(name) > max_len:
        name = name[:max_len] + "…"
    return name


def _get_display_name(member: discord.Member | discord.User) -> str:
    return _sanitize_name(getattr(member, "display_name", str(member)))


def _get_role_names(
    member: discord.Member | discord.User, key_roles: set[str] | None = None,
) -> list[str]:
    if not key_roles:
        return []
    return [
        _sanitize_name(r.name, 24) for r in getattr(member, "roles", [])
        if r.name != "@everyone" and r.name in key_roles
    ]


def _format_identity(
    member: discord.Member | discord.User,
    key_roles: set[str] | None = None,
    is_admin: bool = False,
) -> str:
    """统一历史、引用与指令身份，仅保留不同的用户名及特定身份组。"""
    display_name = _get_display_name(member)
    username = getattr(member, "name", "") or ""
    identity = display_name if not username or display_name.lower() == username.lower() else f"{display_name}[{_sanitize_name(username)}]"
    roles = _get_role_names(member, key_roles)
    if roles:
        identity += f"({','.join(roles)})"
    if is_admin:
        identity += " ✓ADMIN"
    return identity


async def _resolve_mention(match: re.Match[str], guild: discord.Guild | None) -> str:
    """将 <@123456> 解析为 @显示名。解析失败则保留原文。"""
    uid_str = match.group(1)
    if not guild:
        return match.group(0)
    try:
        uid = int(uid_str)
        member = guild.get_member(uid)
        if member:
            return f"@{_get_display_name(member)}"
    except Exception:
        pass
    return match.group(0)


async def _resolve_mentions_in_text(text: str, guild: discord.Guild | None, bot_id: int | None) -> str:
    """解析文本中的用户 mention，统一解析为 @显示名。"""
    if not text:
        return text
    pattern = re.compile(r"<@!?(\d+)>")
    parts: list[str] = []
    last_end = 0
    for m in pattern.finditer(text):
        parts.append(text[last_end:m.start()])
        resolved = await _resolve_mention(m, guild)
        parts.append(resolved)
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts).strip()


def _ensure_channel(rt: Any, channel_id: int) -> list[dict[str, Any]]:
    """Return the mutable history queue for a Discord channel."""
    if channel_id not in rt.channel_history:
        rt.channel_history[channel_id] = []
    return rt.channel_history[channel_id]


def _push_history(
    rt: Any,
    channel_id: int,
    text: str = "",
    *,
    message: discord.Message | None = None,
    msg_id: int | None = None,
) -> None:
    """向频道历史队列追加一条记录。支持同时存储 Message 对象供引用查找。"""
    q = _ensure_channel(rt, channel_id)
    if len(text) > 500:
        text = text[:497] + "..."
    resolved_id = msg_id or (message.id if message else None)
    q.append({
        "text": text,
        "message": message,
        "msg_id": resolved_id,
        "reactions": {},
        "seq": rt.history_seq_counter,
    })
    rt.history_seq_counter += 1
    if len(q) > rt.history_max_len:
        q.pop(0)


def _previous_history_text(rt: Any, channel_id: int, *, exclude_msg_id: int | None = None) -> str:
    """Return the latest non-empty channel-history text before the current trigger."""
    for entry in reversed(_ensure_channel(rt, channel_id)):
        if exclude_msg_id is not None and entry.get("msg_id") == exclude_msg_id:
            continue
        text = (entry.get("text") or "").strip()
        if text:
            return text
    return ""


def _format_time_prefix(msg_time: datetime.datetime | None) -> str:
    """格式化时间前缀（北京时间）。当天 [HH:MM]，跨天 [MM-DD HH:MM]。"""
    if msg_time is None:
        return ""
    now_cst = datetime.datetime.now(_CST)
    msg_cst = msg_time.astimezone(_CST) if msg_time.tzinfo else msg_time
    if msg_cst.date() == now_cst.date():
        return f"[{msg_cst.strftime('%H:%M')}] "
    return f"[{msg_cst.strftime('%m-%d %H:%M')}] "


def _format_member_entry(
    member: discord.Member | discord.User,
    text: str,
    msg_time: datetime.datetime | None = None,
    reply_author: str = "",
    key_roles: set[str] | None = None,
) -> str:
    """构造带精简身份、时间与回复关系的历史条目。"""
    time_prefix = _format_time_prefix(msg_time)
    identity = _format_identity(member, key_roles)
    reply_tag = f" ↩ {reply_author}" if reply_author else ""
    return f"{time_prefix}{identity}{reply_tag}: {text}"


def _build_context_text(
    rt: Any,
    channel_id: int,
    member: discord.Member | discord.User,
    user_input: str,
    *,
    reply_context: str = "",
    exclude_last: bool = False,
    min_seq_override: int | None = None,
) -> str:
    """组装发送给 Clonoth 的完整文本。"""
    q = _ensure_channel(rt, channel_id)
    if exclude_last and q:
        history_entries = q[:-1]
    else:
        history_entries = list(q)
    # Only successfully delivered replies consume channel context. Concurrent
    # branches must still see questions whose replies have not landed yet.
    # A preempt consumes only its own branch's context, not a sibling's watermark.
    wm = min_seq_override
    if wm is None:
        wm = rt.session_state.get_high_watermark(channel_id) if rt.session_state else -1
    history_entries = [e for e in history_entries if e.get("seq", -1) > wm]
    # 排除 Bot 自己的消息，避免群聊上下文中出现 Bot 回复
    history_entries = [e for e in history_entries if not e.get("is_bot")]

    def _fmt_entry(e: dict[str, Any]) -> str:
        t = e["text"]
        r = e.get("reactions") or {}
        if r:
            r_parts = [f"{name}×{cnt}" for name, cnt in r.items() if cnt > 0]
            if r_parts:
                t += f" [{' '.join(r_parts)}]"
        return t

    if history_entries:
        history_block = "\n".join(_fmt_entry(e) for e in history_entries)
    else:
        history_block = ""

    identity = _format_identity(member, rt.key_roles, is_admin=member.id in rt.superusers)
    now_str = datetime.datetime.now(_CST).strftime("%m-%d %H:%M CST")

    parts = []
    if history_block:
        parts.append(f"【群聊上下文记录】\n{history_block}")
    if reply_context:
        parts.append(f"【当前消息引用】\n{reply_context}")
    parts.append(f"当前时间: {now_str}")
    # [2026-05-14 refactor note] The administrator marker now reads the
    # superuser set from DiscordRuntime rather than a module-level global.
    parts.append(
        f"【当前用户指令】\n{identity}: {user_input}\n\n"
        f"请根据以上上下文，执行当前用户的指令并给出回复。"
    )
    return "\n".join(parts)


def register_transient_msg(rt: Any, msg: Any) -> None:
    """Exclude status, progress and approval cards from quoted context by ID."""
    msg_id = getattr(msg, "id", None)
    if not isinstance(msg_id, int):
        return
    # Embedders may supply the original duck-typed runtime rather than DiscordRuntime.
    if not hasattr(rt, "transient_msg_ids"):
        rt.transient_msg_ids = deque(maxlen=1000)
    if msg_id not in rt.transient_msg_ids:
        rt.transient_msg_ids.append(msg_id)


def unregister_transient_msg(rt: Any, msg: Any) -> None:
    """Make a status card quotable after it successfully becomes a partial reply."""
    msg_id = getattr(msg, "id", None)
    if isinstance(msg_id, int) and msg_id in getattr(rt, "transient_msg_ids", ()):
        rt.transient_msg_ids.remove(msg_id)


async def _build_reply_context(rt: Any, message: discord.Message) -> str:
    """提取被引用消息的作者、身份组、时间和正文。"""
    if not message.reference:
        return ""
    if message.reference.message_id in getattr(rt, "transient_msg_ids", ()):
        return ""
    ref: discord.Message | None = None
    if message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
    if not ref and message.reference.message_id:
        q = _ensure_channel(rt, message.channel.id)
        for entry in reversed(q):
            if entry.get("msg_id") == message.reference.message_id and entry.get("message"):
                ref = entry["message"]
                break
    if not ref and message.reference.message_id:
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            pass
    if not ref or ref.id in getattr(rt, "transient_msg_ids", ()):
        return ""

    author = ref.author
    identity = _format_identity(author, rt.key_roles)
    time_prefix = _format_time_prefix(ref.created_at)
    text = ""
    guild = message.guild
    bot_id = rt.dc_client.user.id if rt.dc_client.user else None
    if ref.content:
        text = await _resolve_mentions_in_text(ref.content, guild, bot_id)
    if not text:
        snapshots = getattr(ref, "message_snapshots", None)
        if snapshots and len(snapshots) > 0:
            snap = snapshots[0]
            snap_msg = getattr(snap, "message", snap)
            snap_content = (getattr(snap_msg, "content", "") or "").strip()
            snap_embeds = getattr(snap_msg, "embeds", []) or getattr(snap, "embeds", [])
            if snap_content:
                text = f"[转发] {snap_content[:200]}" + ("..." if len(snap_content) > 200 else "")
            if snap_embeds:
                for e in snap_embeds:
                    parts = []
                    if getattr(e, "title", None):
                        parts.append(e.title)
                    if getattr(e, "description", None):
                        parts.append(e.description[:150])
                    for f in (getattr(e, "fields", None) or []):
                        fname = getattr(f, "name", "") or ""
                        fval = getattr(f, "value", "") or ""
                        if fname or fval:
                            parts.append(f"{fname}: {fval[:80]}")
                    if parts:
                        embed_text = " | ".join(parts)
                        text = (text + "\n" if text else "[转发] ") + embed_text
            if not text:
                text = "[转发内容]"
            snap_atts = getattr(snap_msg, "attachments", None) or getattr(snap, "attachments", None) or []
            img_count = sum(
                1 for a in snap_atts
                if _context_image_mime(getattr(a, "content_type", None), getattr(a, "filename", "") or "")
            )
            if img_count:
                text += f" [含{img_count}张图片]"
    if not text and ref.embeds:
        embed_parts = []
        for e in ref.embeds:
            parts = []
            if getattr(e, "title", None):
                parts.append(e.title)
            if getattr(e, "description", None):
                parts.append(e.description[:150])
            for f in (getattr(e, "fields", None) or []):
                fname = getattr(f, "name", "") or ""
                fval = getattr(f, "value", "") or ""
                if fname or fval:
                    parts.append(f"{fname}: {fval[:80]}")
            if getattr(e, "url", None):
                parts.append(e.url)
            if parts:
                embed_parts.append(" | ".join(parts))
        if embed_parts:
            text = "\n".join(embed_parts)
    if not text and ref.stickers:
        text = "[贴纸: " + ", ".join(s.name for s in ref.stickers) + "]"
    if not text and ref.attachments:
        text = f"[附件×{len(ref.attachments)}]"
    if not text:
        text = "[media]"
    if len(text) > 500:
        text = text[:497] + "..."
    return f"{time_prefix}{identity}: {text}"


def record_reaction_add(rt: Any, payload: discord.RawReactionActionEvent) -> None:
    """Record a reaction in the in-memory channel history for later context."""
    q = rt.channel_history.get(payload.channel_id)
    if not q:
        return
    name = payload.emoji.name or str(payload.emoji)
    for item in q:
        if item.get("msg_id") == payload.message_id:
            item["reactions"][name] = item["reactions"].get(name, 0) + 1
            break


def record_reaction_remove(rt: Any, payload: discord.RawReactionActionEvent) -> None:
    """Remove a reaction from the in-memory channel history for later context."""
    q = rt.channel_history.get(payload.channel_id)
    if not q:
        return
    name = payload.emoji.name or str(payload.emoji)
    for item in q:
        if item.get("msg_id") == payload.message_id:
            cur = item["reactions"].get(name, 0)
            if cur > 1:
                item["reactions"][name] = cur - 1
            else:
                item["reactions"].pop(name, None)
            break
