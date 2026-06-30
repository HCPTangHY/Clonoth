"""Discord bot application wiring.

[2026-05-14 refactor note] The legacy entry point was reduced to a compatibility
entry point. This module owns settings, DiscordRuntime, event registration, SDK
initialization, and the top-level Discord client startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import discord  # type: ignore[import-untyped]
import yaml

# ---- SDK 导入 ----
# [2026-05-14 refactor note] Keep the repository root on sys.path when app.py is
# imported through the compatibility shim or directly during deployment checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from clonoth_sdk import BotConfig, ClonothClient, EventRouter, SessionState  # noqa: E402

from .agent import ApprovalView, _start_bridge, handle_agent, handle_model_command
from .callbacks import DiscordCallbacks
from .context import (
    _format_member_entry,
    _get_display_name,
    _previous_history_text,
    _push_history,
    _resolve_mentions_in_text,
    record_reaction_add,
    record_reaction_remove,
)
from .messaging import _current_message_media_text, _resolve_image_mime


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_bot")


# ============================================================
#  配置常量（Discord 平台专属，不进 SDK）
# ============================================================

CLONOTH_BASE_URL = os.environ.get("CLONOTH_URL", "http://127.0.0.1:8765")
CLONOTH_WORKSPACE: Path = Path(
    os.environ.get("CLONOTH_WORKSPACE", "") or Path(__file__).resolve().parents[2]
)
AGENT_LOG_CHANNEL_ID = int(os.environ.get("DISCORD_LOG_CHANNEL", "0"))
BRIDGE_PORT = int(os.environ.get("DISCORD_BRIDGE_PORT", "8768"))
HISTORY_MAX_LEN = int(os.environ.get("DISCORD_HISTORY_LEN", "15"))
_RESTART_SIGNAL = "[BOT_RESTART]"
_SPLIT_SIGNAL = "[SPLIT]"
_REACT_PATTERN = re.compile(r'\[REACT:(.*?)\]')
# Override via env: DISCORD_ENTRY_NODE (default: main)
_ENTRY_NODE_ID = os.environ.get("DISCORD_ENTRY_NODE", "main")
# Node display names — override by setting DISCORD_NODE_NAMES as JSON, e.g.
# '{"coder": "🔧 Coder", "reporter": "📰 Reporter"}'
_NODE_DISPLAY_NAMES: dict[str, str] = {}
try:
    _raw_names = os.environ.get("DISCORD_NODE_NAMES", "")
    if _raw_names:
        import json as _json
        _NODE_DISPLAY_NAMES = _json.loads(_raw_names)
except Exception:
    pass
_DEFAULT_DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")


def _parse_superusers(raw: str) -> set[int]:
    """Parse DISCORD_SUPERUSERS into the integer set used by runtime."""
    superusers: set[int] = set()
    if raw:
        for s in raw.split(","):
            s = s.strip()
            if s.isdigit():
                superusers.add(int(s))
    return superusers


SUPERUSERS: set[int] = _parse_superusers(os.environ.get("DISCORD_SUPERUSERS", ""))


@dataclass
class DiscordRuntime:
    """Shared Discord adapter state passed into all split modules."""

    dc_client: discord.Client
    workspace: Path
    superusers: set[int]
    agent_log_channel_id: int
    bridge_port: int
    entry_node_id: str
    child_node_display_names: dict[str, str]
    base_url: str
    history_max_len: int
    restart_signal: str = _RESTART_SIGNAL
    split_signal: str = _SPLIT_SIGNAL
    react_pattern: re.Pattern[str] = _REACT_PATTERN
    clonoth_client: ClonothClient | None = None
    bot_config: BotConfig | None = None
    session_state: SessionState | None = None
    event_router: EventRouter | None = None
    callbacks: DiscordCallbacks | None = None
    bridge_started: bool = False
    router_task: asyncio.Task[Any] | None = None
    channel_history: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    history_seq_counter: int = 0
    # [2026-05-14 refactor note] callbacks.py uses this factory so ApprovalView
    # can stay in agent.py without creating a callbacks -> agent import cycle.
    approval_view_factory: Callable[[str], discord.ui.View] | None = None
    bridge_runner: Any | None = None
    bridge_site: Any | None = None


def _create_discord_client() -> discord.Client:
    """Create the Discord client with the same intents as the monolithic file."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.reactions = True
    return discord.Client(intents=intents)


def create_runtime() -> DiscordRuntime:
    """Create DiscordRuntime from current environment variables."""
    # [2026-05-14 refactor note] Preserve the old two-phase configuration:
    # module import establishes the default/process superusers, then main()
    # loads .env and adds any DISCORD_SUPERUSERS values on top of that set.
    runtime_superusers = set(SUPERUSERS)
    runtime_superusers.update(_parse_superusers(os.environ.get("DISCORD_SUPERUSERS", "")))
    rt = DiscordRuntime(
        dc_client=_create_discord_client(),
        workspace=Path(os.environ.get("CLONOTH_WORKSPACE", "") or CLONOTH_WORKSPACE),
        superusers=runtime_superusers,
        agent_log_channel_id=int(os.environ.get("DISCORD_LOG_CHANNEL", str(AGENT_LOG_CHANNEL_ID))),
        bridge_port=int(os.environ.get("DISCORD_BRIDGE_PORT", str(BRIDGE_PORT))),
        entry_node_id=_ENTRY_NODE_ID,
        child_node_display_names=dict(_NODE_DISPLAY_NAMES),
        base_url=os.environ.get("CLONOTH_URL", CLONOTH_BASE_URL),
        history_max_len=int(os.environ.get("DISCORD_HISTORY_LEN", str(HISTORY_MAX_LEN))),
    )
    rt.approval_view_factory = lambda approval_id: ApprovalView(rt, approval_id)
    return rt


def _load_extra_roots(rt: DiscordRuntime) -> list[Path]:
    """Load policy.yaml extra_roots exactly as the former on_ready block did."""
    extra_roots: list[Path] = []
    try:
        policy_path = rt.workspace / "data" / "policy.yaml"
        if policy_path.exists():
            policy_data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            if isinstance(policy_data, dict):
                raw_roots = policy_data.get("extra_roots", [])
                if isinstance(raw_roots, list):
                    for item in raw_roots:
                        if isinstance(item, str) and item.strip():
                            p = Path(item)
                            p = p.resolve() if p.is_absolute() else (rt.workspace / p).resolve()
                            extra_roots.append(p)
        if extra_roots:
            print(f"[bot] extra_roots: {extra_roots}")
    except Exception as exc:
        print(f"[bot] 加载 extra_roots 失败: {exc}")
    return extra_roots


async def _initialize_sdk(rt: DiscordRuntime) -> None:
    """Initialize SDK objects and EventRouter after Discord is ready."""
    # 读取 Supervisor admin token 用于鉴权受保护的端点（!model 等）
    _token_path = Path(os.environ.get("CLONOTH_WORKSPACE", "/www/wwwroot/Clonoth")) / "data" / ".admin_token"
    admin_token = ""
    try:
        if _token_path.exists():
            admin_token = _token_path.read_text().strip()
            print(f"[bot] admin_token loaded from {_token_path}")
    except Exception as exc:
        print(f"[bot] 读取 admin_token 失败: {exc}")
    rt.clonoth_client = ClonothClient(
        rt.base_url,
        admin_token=admin_token,
        admin_token_path=str(_token_path),
    )

    await _start_bridge(rt)

    if not os.environ.get("CLONOTH_WORKSPACE"):
        for attempt in range(10):
            try:
                health = await rt.clonoth_client.get_health()
                if health.workspace_root:
                    rt.workspace = Path(health.workspace_root)
                    print(f"[bot] workspace_root 从 Supervisor 获取: {rt.workspace}")
                    break
            except Exception as e:
                if attempt < 9:
                    print(f"[bot] 获取 workspace_root 失败 (第 {attempt + 1} 次)，2s 后重试: {e}")
                    await asyncio.sleep(2)
                else:
                    print(f"[bot] 获取 workspace_root 全部失败，使用默认值 {rt.workspace}: {e}")

    print(f"[bot] CLONOTH_WORKSPACE = {rt.workspace}")
    if not rt.superusers:
        print("[bot] ⚠ DISCORD_SUPERUSERS 未配置；取消任务与审批按钮已默认禁用。")

    extra_roots = _load_extra_roots(rt)
    rt.bot_config = BotConfig(
        base_url=rt.base_url,
        entry_node_id=rt.entry_node_id,
        conversation_key_prefix="discord",
        workspace_root=rt.workspace,
        extra_roots=extra_roots,
        auto_approve_internal=True,
    )
    rt.session_state = SessionState()
    rt.callbacks = DiscordCallbacks(rt)
    rt.event_router = EventRouter(
        rt.clonoth_client,
        rt.session_state,
        rt.callbacks,
        rt.bot_config,
        entry_node_id=rt.entry_node_id,
        poll_interval=3.0,
    )
    rt.event_router.set_raw_event_hook(rt.callbacks.raw_event_hook)
    rt.router_task = asyncio.create_task(rt.event_router.run())
    print("[bot] EventRouter 已启动")


async def _send_restart_notification_if_needed(rt: DiscordRuntime) -> None:
    """Send and inject the bot restart notification if the restart marker exists."""
    restart_notify_file = Path(f"/tmp/clonoth_restart_notify_{rt.bridge_port}.json")
    if not restart_notify_file.exists():
        return
    try:
        notify_data = json.loads(restart_notify_file.read_text())
        ch_id = notify_data.get("channel_id")
        if ch_id:
            ch = rt.dc_client.get_channel(int(ch_id)) or await rt.dc_client.fetch_channel(int(ch_id))
            if ch:
                sent_msg = await ch.send("✅ Bot 已安全重启，恢复服务！")
                asyncio.create_task(
                    handle_agent(rt, sent_msg, "✅ Bot 已安全重启，恢复服务！新代码已生效。", int(ch_id))
                )
        restart_notify_file.unlink(missing_ok=True)
    except Exception as e:
        print(f"[bot] 重启通知失败: {e}")


def register_events(rt: DiscordRuntime) -> None:
    """Register Discord events as closures bound to the runtime object."""

    @rt.dc_client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
        record_reaction_add(rt, payload)

    @rt.dc_client.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
        record_reaction_remove(rt, payload)

    @rt.dc_client.event
    async def on_ready() -> None:
        print(f"[bot] 已登录: {rt.dc_client.user}")
        if not rt.event_router or not rt.router_task or rt.router_task.done():
            await _initialize_sdk(rt)
        await _send_restart_notification_if_needed(rt)

    @rt.dc_client.event
    async def on_message(message: discord.Message) -> None:
        if message.author == rt.dc_client.user:
            return
        if message.author.bot:
            return

        channel_id = message.channel.id
        raw_text = message.content.strip()

        guild = message.guild
        bot_id = rt.dc_client.user.id if rt.dc_client.user else None
        clean_text = await _resolve_mentions_in_text(raw_text, guild, bot_id)

        reply_author_name = ""
        if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
            reply_author_name = _get_display_name(message.reference.resolved.author)

        snapshots = getattr(message, "message_snapshots", None)
        if not clean_text and snapshots and len(snapshots) > 0:
            snap = snapshots[0]
            snap_msg = getattr(snap, "message", snap)
            snap_content = (getattr(snap_msg, "content", "") or "").strip()
            snap_embeds = getattr(snap_msg, "embeds", []) or getattr(snap, "embeds", [])
            if snap_content:
                fwd_preview = snap_content[:80] + ("..." if len(snap_content) > 80 else "")
            elif snap_embeds:
                e = snap_embeds[0]
                fwd_preview = ((getattr(e, "title", "") or "") or (getattr(e, "description", "") or ""))[:80] or "[嵌入内容]"
            else:
                fwd_preview = "[转发内容]"
            snap_atts = getattr(snap_msg, "attachments", None) or getattr(snap, "attachments", None) or []
            img_count = sum(
                1 for a in snap_atts
                if _resolve_image_mime(getattr(a, "content_type", None), getattr(a, "filename", "") or "")
            )
            if img_count:
                fwd_preview += f" [含{img_count}张图片]"
            clean_text = f"[转发] {fwd_preview}"

        if not clean_text:
            clean_text = _current_message_media_text(message)

        if clean_text:
            entry = _format_member_entry(
                message.author, clean_text, msg_time=message.created_at, reply_author=reply_author_name,
            )
            _push_history(rt, channel_id, entry, message=message, msg_id=message.id)

        if await handle_model_command(rt, message, raw_text):
            return

        is_dm = isinstance(message.channel, (discord.DMChannel, discord.GroupChannel))
        if is_dm:
            pass
        else:
            if not rt.dc_client.user.mentioned_in(message):
                return
            if message.mention_everyone:
                return

        user_input = clean_text or _current_message_media_text(message)
        if not user_input:
            user_input = _previous_history_text(rt, channel_id, exclude_msg_id=message.id) or "[空消息]"

        asyncio.create_task(handle_agent(rt, message, user_input, channel_id))


def main() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
        load_dotenv()
    except ImportError:
        pass

    token = os.environ.get("DISCORD_TOKEN", _DEFAULT_DISCORD_TOKEN)
    if not token:
        print("请设置环境变量 DISCORD_TOKEN")
        return

    rt = create_runtime()
    register_events(rt)
    rt.dc_client.run(token)


if __name__ == "__main__":
    main()
