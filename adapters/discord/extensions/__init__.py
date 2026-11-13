"""Optional Discord command extensions; no plugin business logic in app.py."""
from __future__ import annotations

from importlib import import_module
import logging

import discord

from ..context import register_transient_msg

log = logging.getLogger(__name__)
_COMMANDS = {"!goal": ("ide_goal", "IDE 扩展")}


async def dispatch_command(rt, message, raw_text: str) -> bool:
    parts = raw_text.split(None, 1)
    spec = _COMMANDS.get(parts[0]) if parts else None
    if spec is None:
        return False
    # Neither unconfirmed commands nor their cards may be quoted into model input.
    register_transient_msg(rt, message)
    module, label = spec
    try:
        handler = import_module(f"{__name__}.{module}").handle
        await handler(rt, message, parts[1].strip() if len(parts) > 1 else "")
    except Exception:
        log.exception("Discord command extension failed: %s", module)
        notice = await message.reply(
            f"[{label}] 命令扩展当前不可用，未转为普通聊天。若已点击确认，请先核对目标状态。",
            mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
        )
        register_transient_msg(rt, notice)
    return True
