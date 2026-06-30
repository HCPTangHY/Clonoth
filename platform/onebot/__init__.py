"""TangQiu 的 Clonoth Agent QQ 接入插件。

本插件采用纯对话模式：QQ群成员 @Bot 或用户私聊 Bot 后，插件把当前请求提交到
ClonothZX；ClonothZX 返回最终结果后，插件再把最终回复发回对应 QQ 会话。
Clonoth 主动发出的中间回复会展示给 QQ；工具调用、进度日志、审批请求和子任务状态
仍不展示给 QQ。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, DefaultDict, Deque, Dict, List, Optional

import httpx
from nonebot import get_bot, get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.rule import Rule, to_me

from .config import (
    ALLOWED_GROUPS,
    BQBS_PATH,
    CLONOTH_BASE_URL,
    CLONOTH_WORKSPACE,
    ENTRY_NODE_ID,
)
from .emoji_handler import load_bqbs, process_emojis, strip_output_markers

# clonoth_sdk 安装在 ClonothZX 工作区内。插件加载时先加入 sys.path，
# 目的是让 TangQiu 进程不需要额外安装同名包也能导入 SDK。
if CLONOTH_WORKSPACE not in sys.path:
    sys.path.insert(0, CLONOTH_WORKSPACE)

from clonoth_sdk import (  # noqa: E402  # sys.path 必须先插入 ClonothZX 工作区。
    BotConfig,
    ChildTaskState,
    ClonothClient,
    EventRouter,
    MainTaskState,
    SessionState,
    TriggerInfo,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent")
driver = get_driver()

_CST = dt.timezone(dt.timedelta(hours=8))
_SPLIT_SIGNAL = "[SPLIT]"
_HISTORY_MAX_LEN = 20
_HISTORY_TEXT_LIMIT = 400
_QQ_MESSAGE_LIMIT = 4300
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")
# 2026-05-03 修改原因：QQ 端需要把 Clonoth 任务生命周期映射为离散 React 阶段。
# 做法是集中维护阶段到 emoji_id 的映射和阶段顺序，目的在于后续回调只声明
# 目标阶段，避免 stream_delta 高频到达时重复调用 OneBot React API。
_REACT_STAGE_EMOJIS = {
    "received": "76",
    "submitted": "281",
    "thinking": "178",
    "tool": "97",
    "writing": "326",
}
_REACT_STAGE_ORDER = {
    "received": 1,
    "submitted": 2,
    "thinking": 3,
    "tool": 4,
    "writing": 5,
}
_REACT_CLEANUP_EMOJIS = tuple(_REACT_STAGE_EMOJIS.values())

# 群聊上下文只保留最近 20 条。这样可以给入口节点提供社交语境，
# 同时避免每次 inbound 发送过长历史。
_group_history: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_LEN))

# EventRouter 回调只拿到 session/trigger，因此这里保存发送最终回复所需的平台对象。
_session_targets: Dict[str, Dict[str, Any]] = {}
_conversation_bots: Dict[str, Bot] = {}
_last_bot: Optional[Bot] = None

_client: Optional[ClonothClient] = None
_session_state: Optional[SessionState] = None
_event_router: Optional[EventRouter] = None
_router_task: Optional[asyncio.Task] = None
_callbacks: Optional["TangQiuCallbacks"] = None
_bqbs: List[str] = []


def _is_group_allowed(group_id: int) -> bool:
    """判断群是否允许接入 Clonoth；空列表表示全部群允许。"""
    return not ALLOWED_GROUPS or group_id in ALLOWED_GROUPS


async def _allowed_group_rule(event: Event) -> bool:
    """把群类型和白名单放在规则层过滤，避免无关消息进入上下文缓存。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    return _is_group_allowed(int(event.group_id))


async def _private_message_rule(event: Event) -> bool:
    """只匹配 QQ 私聊消息，避免私聊请求被群聊白名单逻辑误拦截。"""
    # 2026-05-01 修改原因：私聊没有群号，也不需要 @Bot；这里单独识别
    # PrivateMessageEvent，使私聊入口和现有群聊入口互不影响。
    return isinstance(event, PrivateMessageEvent)


def _sanitize_name(name: str, max_len: int = 32) -> str:
    """清洗成员名称，避免换行和结构符号破坏输入格式。"""
    name = (name or "").replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")


def _sender_display_name(sender: Any, fallback_user_id: Any = "") -> str:
    """优先使用群名片，其次使用昵称，最后回退到 QQ 号。"""
    card = getattr(sender, "card", "") or ""
    nickname = getattr(sender, "nickname", "") or ""
    return _sanitize_name(card or nickname or str(fallback_user_id or "未知成员"))


def _format_hhmm(timestamp: Optional[int]) -> str:
    """把 OneBot 秒级时间戳格式化为入口节点要求的 HH:MM。"""
    try:
        return dt.datetime.fromtimestamp(int(timestamp or time.time()), _CST).strftime("%H:%M")
    except Exception:
        return dt.datetime.now(_CST).strftime("%H:%M")


def _compact_text(text: str, limit: int = _HISTORY_TEXT_LIMIT) -> str:
    """把历史消息压缩到单行，避免每轮请求携带过长上下文。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def _message_to_text(message: Message, bot_self_id: Any = None) -> str:
    """把 OneBot 消息段转换为模型可读文本，同时用占位符保留非文本内容。"""
    parts: List[str] = []
    for segment in message:
        seg_type = getattr(segment, "type", "")
        data = getattr(segment, "data", {}) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = data.get("qq", "")
            if str(qq) == str(bot_self_id):
                continue
            parts.append("@全体成员" if str(qq).lower() == "all" else f"@{qq}")
        elif seg_type == "image":
            # 2026-05-03 修改原因：图片现在会被下载为 Clonoth 附件；这里仍
            # 保留文本占位符，做法是不改变原有消息转文本输出，目的是让模型
            # 在读取附件的同时也能从正文知道用户发送过图片。
            parts.append("[图片]")
        elif seg_type == "face":
            face_id = data.get("id", "")
            parts.append(f"[QQ表情:{face_id}]" if face_id else "[QQ表情]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "reply":
            continue
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def _guess_image_mime(url: str, content_type: str = "") -> str:
    """根据 QQ 图片 URL 和下载响应头推断 Clonoth 需要的图片 MIME 类型。"""
    # 2026-05-03 修改原因：OneBot 图片段通常只给临时 URL，不稳定提供文件名
    # 或 MIME。这里按 URL 与 Content-Type 共同推断，目的是让 Clonoth 的
    # 图片附件获得可识别的 mime_type；无法判断时按 JPEG 兜底。
    url_lower = (url or "").lower()
    content_type_lower = (content_type or "").lower()
    if "png" in url_lower or "png" in content_type_lower:
        return "image/png"
    if "gif" in url_lower or "gif" in content_type_lower:
        return "image/gif"
    if "webp" in url_lower or "webp" in content_type_lower:
        return "image/webp"
    return "image/jpeg"


def _image_ext_from_url_or_mime(url: str, mime_type: str) -> str:
    """为保存到本地的 QQ 图片选择稳定后缀。"""
    # 2026-05-03 修改原因：附件文件名需要随机生成，但后缀会影响后续图片
    # 识别。这里优先复用 URL 路径中的常见图片后缀，其次用推断出的 MIME
    # 映射后缀，目的是避免没有扩展名的 QQ 临时 URL 生成不可识别文件。
    clean_url_path = (url or "").split("?", 1)[0].split("#", 1)[0]
    ext = Path(clean_url_path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return ext
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/gif":
        return ".gif"
    if mime_type == "image/webp":
        return ".webp"
    return ".jpg"


def _iter_qq_image_urls(message: Any) -> List[str]:
    """从 OneBot Message 中提取 image segment 的下载 URL。"""
    # 2026-05-03 修改原因：当前消息和引用消息都要复用同一套图片段解析逻辑。
    # 做法是只读取 image segment 的 data["url"]，目的是严格按 OneBot 提供
    # 的下载地址采集附件，并让缺少 URL 的图片段自然跳过。
    urls: List[str] = []
    if message is None:
        return urls
    try:
        segments = list(message)
    except Exception:
        return urls
    for segment in segments:
        if getattr(segment, "type", "") != "image":
            continue
        data = getattr(segment, "data", {}) or {}
        url = str(data.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


async def _collect_qq_attachments(event, conversation_key: str) -> list[dict]:
    """下载 QQ 当前消息及引用消息中的图片，并返回 Clonoth 附件列表。"""
    # 2026-05-03 修改原因：QQ 图片以前只被转为 [图片] 文本，占位符无法让
    # Clonoth 读取图像内容。这里把 image segment 的 URL 下载到工作区
    # data/attachments 下，并返回相对路径，目的是与 Discord 端附件格式一致。
    result: list[dict] = []
    messages: List[Any] = []

    try:
        messages.append(event.get_message())
    except Exception as exc:
        logger.warning("collect QQ attachments skipped current message: %s", exc)

    reply = getattr(event, "reply", None)
    reply_msg = getattr(reply, "message", None) if reply else None
    if reply_msg is not None:
        messages.append(reply_msg)

    image_urls: List[str] = []
    for message in messages:
        image_urls.extend(_iter_qq_image_urls(message))
    if not image_urls:
        return result

    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("collect QQ attachments cannot create directory %s: %s", att_dir, exc)
        return result

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for url in image_urls:
            try:
                response = await client.get(url)
                response.raise_for_status()
                if not response.content:
                    logger.warning("collect QQ image attachment skipped empty response: %s", url)
                    continue
                content_type = response.headers.get("content-type", "")
                mime_type = _guess_image_mime(url, content_type)
                ext = _image_ext_from_url_or_mime(url, mime_type)
                filename = f"{os.urandom(16).hex()}{ext}"
                file_path = att_dir / filename
                file_path.write_bytes(response.content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "image",
                    "path": rel_path,
                    "mime_type": mime_type,
                    "name": f"image{ext}",
                })
            except Exception as exc:
                logger.warning("collect QQ image attachment failed: url=%s error=%s", url, exc)
    return result


def _format_history_line(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行。"""
    text = _compact_text(override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None)))
    name = _sender_display_name(event.sender, event.user_id)
    return f"[{_format_hhmm(getattr(event, 'time', None))}] {name}({event.user_id}): {text}"


def _record_group_message(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> None:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。"""
    text = override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))
    if text.strip():
        _group_history[int(event.group_id)].append(_format_history_line(event, bot, override_text=text))


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(text).replace(_SPLIT_SIGNAL, " ")
    text = _QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = _compact_text(text)
    if text:
        _group_history[int(group_id)].append(f"[{dt.datetime.now(_CST).strftime('%H:%M')}] Bot: {text}")


def _build_reply_context(event: GroupMessageEvent, bot: Bot) -> str:
    """提取当前消息引用；OneBot reply 字段不完整时静默跳过。"""
    reply = getattr(event, "reply", None)
    if not reply:
        return ""
    try:
        reply_msg = getattr(reply, "message", None)
        if reply_msg is None:
            return ""
        text = _compact_text(_message_to_text(reply_msg, getattr(bot, "self_id", None)))
        if not text:
            return ""
        sender = getattr(reply, "sender", None)
        sender_id = getattr(sender, "user_id", "") if sender else ""
        name = _sender_display_name(sender, sender_id) if sender else "原作者"
        return f"[{_format_hhmm(getattr(reply, 'time', None))}] {name}: {text}"
    except Exception:
        return ""


def _build_inbound_text(event: GroupMessageEvent, bot: Bot, user_text: str) -> str:
    """组装提交给 tangqiu_main 的群聊 inbound 文本。"""
    group_id = int(event.group_id)
    history_lines = list(_group_history[group_id])[-_HISTORY_MAX_LEN:]
    current_name = _sender_display_name(event.sender, event.user_id)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")

    parts: List[str] = ["【群聊上下文记录】"]
    parts.extend(history_lines or ["（暂无）"])

    reply_context = _build_reply_context(event, bot)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])

    parts.extend([
        "",
        f"当前时间: {now}",
        "【当前用户指令】",
        f"{current_name}（{event.user_id}）: {user_text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


def _build_private_inbound_text(event: PrivateMessageEvent, bot: Bot, user_text: str) -> str:
    """组装提交给 tangqiu_main 的私聊 inbound 文本。"""
    text = (user_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))).strip() or "你好"
    name = _sender_display_name(event.sender, event.user_id)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")
    parts: List[str] = [
        f"当前时间: {now}",
        "【当前用户指令】",
        f"{name}（{event.user_id}）: {text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ]
    return "\n".join(parts)


async def _try_preempt_running_task(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    inbound_text: str,
    attachments: Optional[List[Dict[str, Any]]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
) -> bool:
    """尝试把 QQ 新消息注入当前会话正在运行的入口任务。"""
    # 2026-05-03 修改原因：TangQiu 以前每次消息都 submit_inbound，会在同一
    # 会话已有入口任务运行时并发启动新任务。这里移植 Discord 的 Preempt V2：
    # 先查询当前 session 的入口任务，再用 preempt_task 注入新消息；目的
    # 是让同一会话的新指令打断并接续旧任务，而不是重复开任务。
    if _client is None or _session_state is None:
        return False

    existing_sid = _session_state.get_session_id(conversation_key)
    if not existing_sid:
        return False

    try:
        running_tasks = await _client.get_running_tasks(existing_sid)
    except Exception:
        logger.exception("preempt_v2: get running tasks failed")
        return False

    for rt in running_tasks:
        if not rt.task_id or not rt.is_user_entry:
            continue

        rt_src_seq = rt.source_inbound_seq or 0
        rt_trigger = _session_state.get_trigger(rt_src_seq) if rt_src_seq else None
        if not rt_trigger:
            result = _session_state.find_trigger_by_session(existing_sid)
            rt_trigger = result[1] if result else None

        if not is_dm:
            # 2026-05-03 修改原因：群聊里多个用户共享同一个 conversation_key。
            # 这里必须用旧 trigger 保存的 event.user_id 校验来源；无法确认同一用户
            # 时不执行 preempt，目的是避免一个群成员打断另一个群成员的任务。
            if not rt_trigger:
                continue
            rt_event = rt_trigger.platform_data.get("event")
            if getattr(rt_event, "user_id", None) != getattr(event, "user_id", None):
                continue

        try:
            ok = await _client.preempt_task(
                rt.task_id,
                message=inbound_text,
                attachments=attachments,
            )
        except Exception:
            logger.exception("preempt_v2: preempt task failed")
            continue

        if not ok:
            continue

        # 2026-05-03 修改原因：Preempt 成功后不会生成新的 inbound_seq。
        # 因此必须把旧 trigger 的平台对象更新为本次 QQ event 和 bot；目的
        # 是让最终回复引用新的触发消息，并继续发到正确的 QQ 会话。
        fresh_platform = dict(platform_updates)
        fresh_platform["last_typing_time"] = time.time()
        if rt_trigger:
            rt_trigger.platform_data.update(fresh_platform)
        elif rt_src_seq:
            _session_state.register_trigger(
                TriggerInfo(
                    inbound_seq=rt_src_seq,
                    conversation_key=conversation_key,
                    session_id=existing_sid,
                    is_dm=is_dm,
                    platform_data=fresh_platform,
                )
            )

        _session_targets[existing_sid] = dict(fresh_platform)
        _conversation_bots[conversation_key] = bot
        logger.info("preempt_v2: injected into QQ task %s", rt.task_id[:8])
        return True

    return False


def _group_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_group:{group_id} 会话键解析群号。"""
    prefix = "qq_group:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _private_user_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_private:{user_id} 会话键解析私聊用户号。"""
    prefix = "qq_private:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _target_from_conversation_key(conversation_key: str) -> Optional[Dict[str, Any]]:
    """根据 conversation_key 还原 QQ 回复目标，供无 trigger 的回调兜底使用。"""
    # 2026-05-01 修改原因：EventRouter 的 fallback 回调只有 conversation_key；
    # 这里集中解析 group/private 两种 key，避免发送逻辑继续硬编码群聊。
    group_id = _group_id_from_conversation_key(conversation_key)
    if group_id is not None:
        return {"type": "group", "group_id": group_id}
    user_id = _private_user_id_from_conversation_key(conversation_key)
    if user_id is not None:
        return {"type": "private", "user_id": user_id}
    return None


def _target_from_platform_data(platform_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 trigger 平台数据中提取 QQ 回复目标。"""
    # 2026-05-01 修改原因：主回调会携带 matcher 保存的平台数据；优先使用
    # 显式 type 字段区分私聊和群聊，避免只有 user_id 时误判群聊成员为私聊。
    # 2026-05-03 新增：提取触发消息 ID 和发送者 ID，发第一条回复时自动引用 + @
    event = platform_data.get("event")
    msg_id = getattr(event, "message_id", None) if event else None
    sender_id = getattr(event, "user_id", None) if event else None
    target_type = platform_data.get("type")
    if target_type == "private" and platform_data.get("user_id") is not None:
        t: Dict[str, Any] = {"type": "private", "user_id": int(platform_data["user_id"])}
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        return t
    if target_type == "group" and platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    if platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    return None


def _get_fallback_bot() -> Optional[Bot]:
    """获取 fallback 发送用 Bot；EventRouter 可能在 matcher 外触发回调。"""
    if _last_bot is not None:
        return _last_bot
    try:
        return get_bot()
    except Exception:
        return None


def _truncate_qq_text(text: str) -> str:
    """限制单条 QQ 消息长度，避免超过 OneBot 实现的消息上限。"""
    if len(text) <= _QQ_MESSAGE_LIMIT:
        return text
    suffix = "\n（内容过长，已截断）"
    return text[: _QQ_MESSAGE_LIMIT - len(suffix)] + suffix


def _message_from_processed_segments(segments: List[Dict[str, Any]]) -> Message:
    """把 emoji_handler 的轻量段描述转换为 OneBot Message。"""
    message_segments: List[MessageSegment] = []
    for segment in segments:
        if segment.get("type") == "text":
            content = str(segment.get("content", ""))
            if content:
                message_segments.append(MessageSegment.text(content))
        elif segment.get("type") == "image":
            url = segment.get("url")
            if url:
                message_segments.append(MessageSegment.image(url))
        elif segment.get("type") == "at":
            qq_id = segment.get("qq")
            if qq_id:
                message_segments.append(MessageSegment.at(qq_id))
    return Message(message_segments)


async def _send_qq_message(bot: Bot, target: Dict[str, Any], message: Any) -> None:
    """按 QQ 会话类型选择 OneBot 发送接口。"""
    # 2026-05-01 修改原因：私聊回复必须调用 send_private_msg，群聊回复继续调用
    # send_group_msg。通过 target 分发，回调发送文本和附件时不再硬编码群聊接口。
    if target.get("type") == "private":
        user_id = target.get("user_id")
        if user_id is None:
            raise ValueError("private target missing user_id")
        await bot.send_private_msg(user_id=int(user_id), message=message)
        return
    if target.get("type") == "group":
        group_id = target.get("group_id")
        if group_id is None:
            raise ValueError("group target missing group_id")
        await bot.send_group_msg(group_id=int(group_id), message=message)
        return
    raise ValueError(f"unknown QQ target type: {target!r}")


async def _send_split_text(bot: Bot, target: Dict[str, Any], text: str) -> bool:
    """按 [SPLIT] 拆分最终文本，逐段发送并清理 QQ 不支持的标记。"""
    # 2026-05-01 修改原因：文本拆分逻辑对群聊和私聊相同，实际发送交给
    # _send_qq_message 处理，确保私聊也能复用表情替换和分段发送能力。
    sent_any = False
    parts = text.split(_SPLIT_SIGNAL) if text else []
    for index, raw_part in enumerate(parts):
        part = _truncate_qq_text(raw_part.strip())
        if not part:
            continue
        segments = await process_emojis(part, bot, _bqbs)
        if not segments:
            continue
        msg = _message_from_processed_segments(segments)
        # 第一条消息带引用回复 + @发送者（与 ZhenXia 逻辑一致）
        if not sent_any and target.get("reply_message_id"):
            prefix = MessageSegment.reply(target["reply_message_id"])
            if target.get("reply_sender_id"):
                prefix = prefix + MessageSegment.at(target["reply_sender_id"]) + MessageSegment.text(" ")
            msg = prefix + msg
        await _send_qq_message(bot, target, msg)
        sent_any = True
        if index < len(parts) - 1:
            await asyncio.sleep(0.5)
    return sent_any


def _resolve_attachment_path(attachment: Dict[str, Any]) -> Optional[Path]:
    """把 Clonoth 附件 dict 解析为本地路径。"""
    raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


async def _send_attachment_path(bot: Bot, target: Dict[str, Any], path: Path, filename: str = "") -> None:
    """图片附件发送为图片消息；非图片附件通过 NapCat 文件上传 API 发送。"""
    display_name = filename or path.name
    if path.exists() and path.suffix.lower() in _IMAGE_SUFFIXES:
        try:
            await _send_qq_message(bot, target, MessageSegment.image(file=path))
            return
        except Exception:
            # 部分 OneBot 实现不接受 Path 对象；失败后改用字符串路径重试以增强兼容性。
            await _send_qq_message(bot, target, MessageSegment.image(file=str(path)))
            return
    # [2026-05-08] 非图片附件通过 NapCat upload_group_file / upload_private_file 发送
    if not path.exists():
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}（文件不存在）")
        return
    try:
        file_str = str(path.resolve())
        if target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None:
                await bot.call_api("upload_group_file", group_id=group_id, file=file_str, name=display_name)
                return
        elif target.get("type") == "private":
            user_id = target.get("user_id")
            if user_id is not None:
                await bot.call_api("upload_private_file", user_id=user_id, file=file_str, name=display_name)
                return
        # fallback: 未知 target type
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}")
    except Exception as e:
        logger.warning("文件上传失败 (%s): %s", display_name, e)
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}（上传失败）")


async def _send_attachments(bot: Bot, target: Dict[str, Any], attachments: List[Dict[str, Any]]) -> None:
    """发送 Clonoth 返回的附件列表。"""
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        filename = str(attachment.get("name") or attachment.get("filename") or "")
        if path is None:
            if filename:
                await _send_qq_message(bot, target, f"Clonoth 生成了文件：{filename}")
            continue
        await _send_attachment_path(bot, target, path, filename=filename)


async def _send_text_and_attachments(bot: Bot, target: Dict[str, Any], text: str, attachments: List[Dict[str, Any]]) -> None:
    """统一发送最终文本与附件，并仅在群聊中把最终文本写回群历史。"""
    # 2026-05-01 修改原因：私聊没有群历史缓存，不应写入 _group_history；群聊
    # 仍保留原来的 Bot 回复入库逻辑，维持后续 @Bot 请求的上下文连续性。
    if text:
        if await _send_split_text(bot, target, text) and target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None:
                _record_bot_reply(int(group_id), text)
    if attachments:
        await _send_attachments(bot, target, attachments)


async def _set_message_react(bot: Bot, event: Any, emoji_id: str, enabled: bool) -> bool:
    """设置或移除触发消息上的 QQ React，失败时静默返回 False。"""
    # 2026-05-03 修改原因：React 阶段切换需要多处复用 OneBot 扩展 API。
    # 做法是把单次 set_msg_emoji_like 调用包进独立函数，并强制 emoji_id 为 str；
    # 目的在于满足每次 API 调用都单独容错，避免 React 失败影响消息回复。
    if not bot or not event or not hasattr(event, "message_id"):
        return False
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=int(event.message_id),
            emoji_id=str(emoji_id),
            set=enabled,
        )
        return True
    except Exception:
        return False


async def _switch_react_stage(
    bot: Bot,
    event: Any,
    platform_data: Dict[str, Any],
    stage: str,
) -> None:
    """按单调阶段切换触发消息 React，防止流式事件造成重复抖动。"""
    # 2026-05-03 修改原因：stream_delta 会持续到达，tool 进度也可能被 sweep 多次看到。
    # 做法是在 trigger.platform_data 中记录当前阶段和 emoji_id，并只允许阶段前进；
    # 目的在于首个推理片段和首次工具调用各切换一次，不对每条事件重复调 API。
    next_order = _REACT_STAGE_ORDER.get(stage, 0)
    current_stage = str(platform_data.get("_react_stage") or "")
    current_order = _REACT_STAGE_ORDER.get(current_stage, 0)
    if not next_order or current_order >= next_order:
        return

    next_emoji = _REACT_STAGE_EMOJIS[stage]
    current_emoji = str(platform_data.get("_react_stage_emoji") or _REACT_STAGE_EMOJIS.get(current_stage, ""))
    if current_emoji and current_emoji != next_emoji:
        await _set_message_react(bot, event, current_emoji, False)
    elif not current_emoji:
        # 2026-05-03 修改原因：旧 trigger 或私聊路径可能没有阶段元数据。
        # 做法是在缺少当前 emoji 时清理所有已知阶段 emoji；目的在于补齐状态链时
        # 不让 76、281、178 或 97 残留在同一条触发消息上。
        for emoji_id in _REACT_CLEANUP_EMOJIS:
            if emoji_id != next_emoji:
                await _set_message_react(bot, event, emoji_id, False)

    if await _set_message_react(bot, event, next_emoji, True):
        platform_data["_react_stage"] = stage
        platform_data["_react_stage_emoji"] = next_emoji


async def _clear_message_reacts(bot: Bot, event: Any) -> None:
    """移除任务生命周期使用过的全部 QQ React。"""
    # 2026-05-03 修改原因：最终回复代表任务结束，触发消息不应继续显示中间状态。
    # 做法是按统一清理列表逐个移除 76、281、178、97、326；目的在于包含本轮
    # 新增的推理和工具阶段，并让任一移除失败都不影响最终回复发送。
    for emoji_id in _REACT_CLEANUP_EMOJIS:
        await _set_message_react(bot, event, emoji_id, False)


class TangQiuCallbacks:
    """Clonoth SDK 的 QQ 平台回调实现。

    发送最终回复、附件和主节点中间回复；其余进度、审批、typing、子任务日志均静默。

    2026-05-01 修改原因：QQ 端已开启 send_intermediate_reply，因此类说明需要
    同步说明中间回复会发送；审批流程仍由 Discord 端处理，避免 QQ 侧误展示审批控件。
    """

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        main_state: Optional[MainTaskState] = None,
    ) -> None:
        """发送主节点最终回复。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot or not target:
            logger.warning("send_reply skipped: missing bot or target for session=%s", trigger.session_id)
            return
        # 提取 [REACT:ID] 标记
        final_text = text or ""
        if final_text:
            from .emoji_handler import _extract_reactions
            final_text, reactions = _extract_reactions(final_text)
            if reactions:
                await self.add_reactions(trigger, reactions)
        await _send_text_and_attachments(bot, target, final_text, attachments or [])
        # 2026-05-03 修改原因：最终回复到达时，触发消息上可能仍残留生命周期 React。
        # 做法是调用统一清理函数移除 76、281、178、97、326，目的在于把本轮
        # 新增的推理和工具阶段也纳入最终收尾。
        event = platform_data.get("event")
        await _clear_message_reacts(bot, event)

    async def send_reply_attachment(self, session_id: str, path: str, *args: Any, **kwargs: Any) -> None:
        """兼容旧式 session_id 附件回调；当前 SDK 通常把附件放在 send_reply 中。"""
        target = _session_targets.get(session_id)
        if not target:
            return
        bot = target.get("bot") or _get_fallback_bot()
        if bot:
            await _send_attachment_path(bot, target, Path(path))

    async def send_intermediate_reply(self, trigger: TriggerInfo, text: str) -> None:
        """发送主节点中间回复，但不写入群历史。"""
        if not text:
            return
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot or not target:
            return
        # 2026-05-03 修改原因：中间回复回调表示 Clonoth 已经开始产出用户可见内容。
        # 做法是通过统一阶段切换进入 writing，目的在于从 281、178 或 97 中
        # 任一状态平滑切到 326，并继续让 React API 失败不影响中间文本发送。
        event = platform_data.get("event")
        await _switch_react_stage(bot, event, platform_data, "writing")
        # 提取 [REACT:ID] 标记
        from .emoji_handler import _extract_reactions
        text, reactions = _extract_reactions(text)
        if reactions:
            await self.add_reactions(trigger, reactions)
        if text:
            await _send_split_text(bot, target, text)

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        node_id: str = "",
    ) -> None:
        """处理没有 trigger 的 fallback 最终输出。"""
        target = _target_from_conversation_key(conversation_key)
        if target is None:
            return
        bot = _conversation_bots.get(conversation_key) or _get_fallback_bot()
        if bot:
            await _send_text_and_attachments(bot, target, text or "", attachments or [])

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        return None

    async def update_progress(self, trigger: TriggerInfo, state: MainTaskState) -> None:
        """根据主任务进度切换触发消息上的 React 表情。"""
        # 2026-05-03 修改原因：EventRouter 不展示 QQ 进度文本，但 QQ 侧需要补齐
        # React 链中的 LLM 推理和工具阶段。做法是读取 MainTaskState.stream_parts
        # 与 progress_records：首次流式文本进入 thinking，真实工具执行进度进入 tool；
        # 目的在于复用 SDK sweep 回调并用阶段元数据避免高频 stream_delta 抖动。
        platform_data = trigger.platform_data
        event = platform_data.get("event")
        if not event or not hasattr(event, "message_id"):
            return
        bot = platform_data.get("bot") or _get_fallback_bot()
        if not bot:
            return

        has_tool_progress = any("执行" in record and "个工具" in record for record in state.progress_records)
        if has_tool_progress:
            await _switch_react_stage(bot, event, platform_data, "tool")
            return
        if state.stream_parts:
            await _switch_react_stage(bot, event, platform_data, "thinking")

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: Optional[TriggerInfo] = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        return None

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        return None

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: Dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """QQ 端不展示审批界面；收到审批请求时直接放行。"""
        # 2026-05-03 修改原因：本轮要求 TangQiu QQ 插件审批全部放行。
        # EventRouter 对外部操作仍会进入 show_approval_ui，所以这里直接提交
        # allow 决策；目的是避免 QQ 端没有审批控件时任务卡在等待审批。
        if _client is None:
            logger.warning(
                "approval auto-approve skipped on QQ: client unavailable id=%s operation=%s",
                approval_id,
                operation,
            )
            return
        try:
            ok = await _client.approve(
                approval_id,
                decision="allow",
                comment="TangQiu QQ adapter auto-approved.",
            )
        except Exception:
            logger.exception(
                "approval auto-approve failed on QQ: id=%s operation=%s",
                approval_id,
                operation,
            )
            return
        if ok:
            logger.info("approval auto-approved on QQ: id=%s operation=%s", approval_id, operation)
        else:
            logger.warning("approval auto-approve rejected on QQ: id=%s operation=%s", approval_id, operation)

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: List[str]) -> None:
        """在 QQ 消息上添加表情反应。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        event = platform_data.get("event")
        if not bot or not event or not hasattr(event, "message_id"):
            return
        for emoji_id in reactions:
            eid = emoji_id.strip()
            if not eid:
                continue
            try:
                await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id=eid, set=True)
            except Exception:
                pass

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: List[TriggerInfo]) -> None:
        """上下文重置时同步清理 QQ 侧历史缓存。"""
        target = _target_from_conversation_key(conversation_key)
        if target and target.get("type") == "group" and reason != "compact":
            group_id = target.get("group_id")
            if group_id is not None:
                _group_history.pop(int(group_id), None)

    async def on_engine_restarted(self, payload: Dict[str, Any]) -> None:
        return None

    async def on_task_complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_fail(self, *args: Any, **kwargs: Any) -> None:
        return None


@driver.on_startup
async def _startup() -> None:
    """NoneBot 启动时初始化 Clonoth SDK 与事件路由。"""
    global _client, _session_state, _event_router, _router_task, _callbacks, _bqbs
    if _router_task is not None and not _router_task.done():
        return

    _bqbs = load_bqbs(BQBS_PATH)
    _client = ClonothClient(CLONOTH_BASE_URL)
    _session_state = SessionState()
    _callbacks = TangQiuCallbacks()

    bot_config = BotConfig(
        base_url=CLONOTH_BASE_URL,
        entry_node_id=ENTRY_NODE_ID,
        conversation_key_prefix="qq_group",
        workspace_root=Path(CLONOTH_WORKSPACE),
        # 2026-05-03 修改原因：TangQiu QQ 侧不展示审批控件。本轮要求
        # 审批默认放行，所以打开 SDK 的内部操作自动审批开关，目的是避免
        # 内部工具调用停在等待审批状态。
        auto_approve_internal=True,
    )
    _event_router = EventRouter(
        _client,
        _session_state,
        _callbacks,
        bot_config,
        entry_node_id=ENTRY_NODE_ID,
        poll_interval=1.0,
    )
    _router_task = asyncio.create_task(_event_router.run())
    logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    global _client, _event_router, _router_task, _callbacks
    if _event_router is not None:
        _event_router.stop()
    if _router_task is not None:
        _router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _router_task
        _router_task = None
    if _client is not None:
        await _client.close()
        _client = None
    _event_router = None
    _callbacks = None
    logger.info("Clonoth Agent QQ adapter stopped")


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(_allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        _record_group_message(event, bot)


# Agent 入口 matcher。只处理 @Bot 的群消息，并阻断后续 matcher，避免重复响应。
_agent_matcher = on_message(rule=to_me() & Rule(_allowed_group_rule), priority=10, block=True)


@_agent_matcher.handle()
async def _handle_agent(bot: Bot, event: GroupMessageEvent) -> None:
    """把当前 QQ 群请求提交给 ClonothZX。"""
    global _last_bot
    _last_bot = bot

    if _client is None or _session_state is None:
        await _agent_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    group_id = int(event.group_id)
    conversation_key = f"qq_group:{group_id}"
    user_text = _message_to_text(event.get_message(), getattr(bot, "self_id", None)).strip() or "你好"
    inbound_text = _build_inbound_text(event, bot, user_text)
    # 2026-05-03 修改原因：QQ 群聊图片需要以 Clonoth 附件形式传入。
    # 做法是在构造 inbound 文本后立刻下载当前消息和引用消息中的图片，目的
    # 是让 preempt 与 submit_inbound 使用同一份附件载荷。
    attachments = await _collect_qq_attachments(event, conversation_key)
    # 把 inbound_text 中的 [图片] 占位符替换为带路径版本，让 engine 能定位图片
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    # 2026-05-03 修改原因：在 submit_inbound 前优先尝试 Preempt V2。做法是
    # 查询当前会话已有 session 的 running task，并只允许同一群用户打断自己的
    # 入口任务；目的是避免连续 @Bot 时为同一用户并发启动多个入口任务。
    preempt_ok = await _try_preempt_running_task(
        bot=bot,
        event=event,
        conversation_key=conversation_key,
        inbound_text=inbound_text,
        attachments=attachments or None,
        is_dm=False,
        platform_updates={
            "bot": bot,
            "event": event,
            "type": "group",
            "group_id": group_id,
        },
    )
    if preempt_ok:
        _record_group_message(event, bot, override_text=user_text)
        await _agent_matcher.finish()

    try:
        # 当前 ClonothZX SDK 暴露的方法名是 submit_inbound；它对应需求中的
        # send_inbound 语义，即向 Supervisor 提交一条用户输入。
        result = await _client.submit_inbound(
            channel="qq_group",
            conversation_key=conversation_key,
            text=inbound_text,
            message_id=str(event.message_id),
            attachments=attachments or None,
            use_context=True,
            entry_node_id=ENTRY_NODE_ID,
        )
        # 2026-05-01 修改原因：群聊 @Bot 后需要给触发消息一个反馈；这里在
        # submit_inbound 成功后立刻调用 OneBot 扩展 API，目的只是展示 reaction，
        # 因此不支持该接口时静默跳过，不影响原有主流程。
        try:
            await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id="76")
        except Exception:
            pass
        # 2026-05-03 修改原因：新增离散 React 状态链。submit_inbound 成功后，
        # 触发消息不应继续停留在“收到”状态。做法是移除既有 76 并添加 281，
        # 目的在于提示用户请求已经进入 Clonoth 处理阶段；React 失败仍静默跳过。
        try:
            await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id="76", set=False)
            await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id="281")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("submit inbound failed")
        _record_group_message(event, bot, override_text=user_text)
        await _agent_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    _record_group_message(event, bot, override_text=user_text)

    if not result.session_id or not result.accepted:
        await _agent_matcher.finish("Clonoth Agent 未接受本次请求。")

    _session_state.register_session(conversation_key, result.session_id)
    _session_targets[result.session_id] = {"type": "group", "group_id": group_id, "bot": bot, "event": event}
    _conversation_bots[conversation_key] = bot

    if result.inbound_seq:
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=conversation_key,
            session_id=result.session_id,
            is_dm=False,
            platform_data={
                "bot": bot,
                "event": event,
                "type": "group",
                "group_id": group_id,
                "last_typing_time": time.time(),
                # 2026-05-03 修改原因：submit_inbound 成功后已经把触发消息切到 281。
                # 做法是在 trigger 平台数据中记录 submitted 阶段，目的在于后续
                # update_progress 能只向 178、97、326 前进，不重复或倒退切换。
                "_react_stage": "submitted",
                "_react_stage_emoji": _REACT_STAGE_EMOJIS["submitted"],
            },
        )
        _session_state.register_trigger(trigger)

    # 纯对话模式不发送“处理中”消息；最终结果由 EventRouter 回调发送。
    await _agent_matcher.finish()


# 私聊入口 matcher。私聊不需要 @Bot，直接阻断后续 matcher，避免重复响应。
_private_matcher = on_message(rule=Rule(_private_message_rule), priority=10, block=True)


@_private_matcher.handle()
async def _handle_private_agent(bot: Bot, event: PrivateMessageEvent) -> None:
    """把当前 QQ 私聊请求提交给 ClonothZX。"""
    # 2026-05-01 修改原因：新增 QQ 私聊接入。私聊使用 qq_private:{user_id}
    # 作为独立会话键，不读取也不写入群聊历史，最终回复通过私聊 API 发回。
    global _last_bot
    _last_bot = bot

    if _client is None or _session_state is None:
        await _private_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    user_id = int(event.user_id)
    conversation_key = f"qq_private:{user_id}"
    user_text = _message_to_text(event.get_message(), getattr(bot, "self_id", None)).strip() or "你好"
    inbound_text = _build_private_inbound_text(event, bot, user_text)
    # 2026-05-03 修改原因：QQ 私聊图片同样需要作为 Clonoth 附件传入。
    # 做法与群聊入口一致，先下载当前消息和引用消息中的图片，目的是让
    # preempt 与 submit_inbound 在私聊路径也收到正确附件。
    attachments = await _collect_qq_attachments(event, conversation_key)
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    # 2026-05-03 修改原因：私聊同一 conversation_key 只对应同一用户。做法是
    # 在 submit_inbound 前直接尝试打断该私聊 session 的入口任务；目的是让
    # 用户连续发私聊时复用正在运行的任务，而不是新建并发任务。
    preempt_ok = await _try_preempt_running_task(
        bot=bot,
        event=event,
        conversation_key=conversation_key,
        inbound_text=inbound_text,
        attachments=attachments or None,
        is_dm=True,
        platform_updates={
            "bot": bot,
            "event": event,
            "type": "private",
            "user_id": user_id,
        },
    )
    if preempt_ok:
        await _private_matcher.finish()

    try:
        # 当前 ClonothZX SDK 暴露的方法名是 submit_inbound；它对应需求中的
        # send_inbound 语义，即向 Supervisor 提交一条用户输入。
        result = await _client.submit_inbound(
            channel="qq_private",
            conversation_key=conversation_key,
            text=inbound_text,
            message_id=str(event.message_id),
            attachments=attachments or None,
            use_context=True,
            entry_node_id=ENTRY_NODE_ID,
        )
        # 2026-05-01 修改原因：私聊提交成功后需要提示对方“正在输入”；这里调用
        # OneBot 的 set_input_status 扩展 API，只作为状态提示，目的不是改变会话
        # 流程，所以兼容不支持该接口的实现并静默跳过。
        try:
            await bot.call_api("set_input_status", user_id=int(event.user_id), event_type=1)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("submit private inbound failed")
        await _private_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not result.session_id or not result.accepted:
        await _private_matcher.finish("Clonoth Agent 未接受本次请求。")

    _session_state.register_session(conversation_key, result.session_id)
    _session_targets[result.session_id] = {"type": "private", "user_id": user_id, "bot": bot, "event": event}
    _conversation_bots[conversation_key] = bot

    if result.inbound_seq:
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=conversation_key,
            session_id=result.session_id,
            is_dm=True,
            platform_data={
                "bot": bot,
                "event": event,
                "type": "private",
                "user_id": user_id,
                "last_typing_time": time.time(),
            },
        )
        _session_state.register_trigger(trigger)

    # 纯对话模式不发送“处理中”消息；最终结果由 EventRouter 回调发送。
    await _private_matcher.finish()
