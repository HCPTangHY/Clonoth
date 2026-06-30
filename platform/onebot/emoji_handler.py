"""QQ 表情和输出文本清理工具。

Clonoth 后端可能输出 QQ 表情标记、Discord 表情标记、Reaction 标记或 Markdown。
本模块在发往 QQ 前统一处理这些内容，目的是让 QQ 群只看到可渲染的纯文本
和可发送的自定义表情图片。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

# QQ 端约定的自定义表情标记。模型输出 [QQ_EMOJI:名称] 时，
# 适配器按 bqbs.txt 的名称索引替换为 OneBot 图片消息段。
_QQ_EMOJI_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")

# Clonoth 模型输出 [at:QQ号] 格式的 at 标记。QQ 端需要将其
# 转换为 OneBot MessageSegment.at()，否则只会被当作纯文本发送。
# 同时兼容旧的 [CQ:at,qq=xxx] 格式。
_AT_RE = re.compile(r"\[at:(\d+)\]|\[CQ:at,qq=(\d+)\]")

# Discord 自定义表情在 QQ 无法渲染。这里直接剥离，避免群内出现平台私有格式。
_DC_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

# [REACT:...] 反应标记。QQ 端现在支持 reaction，由 _extract_reactions 提取后执行。
_REACT_RE = re.compile(r"\[REACT:[^\]]+\]")


def _extract_reactions(text: str) -> tuple[str, List[str]]:
    """从文本中提取 [REACT:emoji_id] 标记，返回 (清理后文本, emoji_id列表)。"""
    reactions: List[str] = []

    def _repl(m: re.Match) -> str:
        inner = m.group(0)[7:-1].strip()  # 去掉 [REACT: 和 ]
        if inner:
            reactions.append(inner)
        return ""

    cleaned = _REACT_RE.sub(_repl, text).strip()
    return cleaned, reactions

# Markdown 在 QQ 中不会按预期渲染。以下规则只做轻量剥离，保留正文内容。
_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
_UNDERLINE_BOLD_RE = re.compile(r"__(.*?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", re.DOTALL)
_UNDERLINE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)


def load_bqbs(path: str) -> List[str]:
    """读取 QQ 自定义表情名称列表。

    这里在文件不存在或读取失败时返回空列表，是为了让 Agent 文本回复不因
    表情资源异常而中断；表情缺失时只会跳过对应 [QQ_EMOJI:...] 标记。
    """
    try:
        content = Path(path).read_text("utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return [line.strip() for line in content.splitlines() if line.strip()]


def strip_output_markers(text: str) -> str:
    """清理 QQ 不支持的输出标记并剥离基础 Markdown。

    这样处理的目的，是把 Clonoth 面向多平台的输出转换成 QQ 可直接阅读的文本。
    这里只做保守替换，不重写内容语义。
    """
    if not text:
        return ""

    text = _DC_EMOJI_RE.sub("", text)
    text = _REACT_RE.sub("", text)
    text = _CODE_BLOCK_RE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
    text = _BOLD_RE.sub(lambda m: m.group(1), text)
    text = _UNDERLINE_BOLD_RE.sub(lambda m: m.group(1), text)
    text = _ITALIC_RE.sub(lambda m: m.group(1), text)
    text = _UNDERLINE_ITALIC_RE.sub(lambda m: m.group(1), text)
    text = _HEADING_RE.sub("", text)
    return text.strip()


def _custom_face_url(face: Any) -> str:
    """从 fetch_custom_face 的返回项中取出可发送的图片地址。

    不同 OneBot 实现可能返回字符串，也可能返回包含 url/file/image 字段的字典。
    这里做兼容处理，是为了让 [QQ_EMOJI:name] 在多种适配端上都能尽量渲染成图片。
    """
    if isinstance(face, str):
        return face
    if isinstance(face, dict):
        return str(face.get("url") or face.get("file") or face.get("image") or "")
    return ""


async def process_emojis(text: str, bot: Any, bqbs: List[str]) -> List[Dict[str, Any]]:
    """处理文本中的 QQ 表情标记，返回可发送的消息段描述。

    处理方式如下：
    1. 先移除 Discord 表情、Reaction 标记和 Markdown 标记。
    2. 遇到 [QQ_EMOJI:name] 时拆分文本。
    3. 通过 fetch_custom_face 懒加载 QQ 自定义表情列表，并按 bqbs.txt 下标取图。

    返回 dict 而不是直接返回 MessageSegment，是为了让该模块保持轻量，避免
    在工具层直接依赖 NoneBot2 的消息段实现。
    """
    text = strip_output_markers(text)
    segments: List[Dict[str, Any]] = []
    last_end = 0
    bqblist = None

    # 合并 QQ_EMOJI 和 CQ:at 标记，按位置排序统一处理
    emoji_matches = [(m.start(), m.end(), "emoji", m) for m in _QQ_EMOJI_RE.finditer(text)]
    at_matches = [(m.start(), m.end(), "at", m) for m in _AT_RE.finditer(text)]
    all_matches = sorted(emoji_matches + at_matches, key=lambda x: x[0])

    for start, end, match_type, match in all_matches:
        before = text[last_end:start]
        if before.strip():
            segments.append({"type": "text", "content": before})

        if match_type == "at":
            # group(1) 匹配 [at:xxx]，group(2) 匹配 [CQ:at,qq=xxx]
            qq_id = match.group(1) or match.group(2)
            segments.append({"type": "at", "qq": qq_id})
            last_end = end
            continue

        # QQ_EMOJI 处理
        name = match.group(1).strip()
        if name in bqbs:
            if bqblist is None:
                try:
                    bqblist = await bot.call_api("fetch_custom_face")
                except Exception:
                    # 表情 API 失败时不应影响最终文本回复，因此退回到文本占位。
                    bqblist = []
            index = bqbs.index(name)
            if index < len(bqblist):
                url = _custom_face_url(bqblist[index])
                if url:
                    segments.append({"type": "image", "url": url})
                else:
                    # 找到名称但没有可发送地址时，保留可读占位，避免内容无声丢失。
                    segments.append({"type": "text", "content": f"[表情:{name}]"})
            else:
                # bqbs.txt 与 fetch_custom_face 返回数量不一致时，保留可读占位。
                segments.append({"type": "text", "content": f"[表情:{name}]"})
        elif name:
            # 模型输出了未知表情名时，转成 QQ 可读文本，而不是暴露内部标记。
            segments.append({"type": "text", "content": f"[表情:{name}]"})

        last_end = end

    after = text[last_end:]
    if after.strip():
        segments.append({"type": "text", "content": after})

    return segments
