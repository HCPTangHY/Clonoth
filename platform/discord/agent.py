"""Agent entry handling, Discord UI views, and bridge server.

[2026-05-14 refactor note] This module receives DiscordRuntime from app.py so
agent submission, cancellation, approval, and bridge execution use the same
runtime state instead of reading module-level globals.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import Any

import discord  # type: ignore[import-untyped]
from aiohttp import web

from clonoth_sdk import TriggerInfo

from .context import _build_context_text, _build_reply_context, _ensure_channel
from .messaging import _collect_attachments


logger = logging.getLogger("discord_bot")


class _SuperuserView(discord.ui.View):
    """基类：仅允许 SUPERUSERS 交互。"""

    def __init__(self, rt: Any, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        # [2026-05-14 refactor note] The superuser set now lives on runtime so
        # the view remains valid after app.py reloads environment settings.
        self.rt = rt

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.rt.superusers:
            await interaction.response.send_message("管理员名单未配置，当前已禁用此操作。", ephemeral=True)
            return False
        if interaction.user.id not in self.rt.superusers:
            await interaction.response.send_message("权限不足：只有管理员可以操作。", ephemeral=True)
            return False
        return True


class CancelView(_SuperuserView):
    """v2 改动：cancel 操作改用 ClonothClient 替代直接 httpx 调用。"""

    def __init__(self, rt: Any, session_id: str) -> None:
        super().__init__(rt, timeout=300)
        self.session_id = session_id
        self.task_id: str | None = None
        self.cancelled = False

    @discord.ui.button(label="取消任务", style=discord.ButtonStyle.secondary, emoji="🛑")
    async def cancel_task(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        button.disabled = True
        try:
            if self.task_id and self.rt.clonoth_client:
                ok = await self.rt.clonoth_client.cancel_task(self.task_id)
            elif self.rt.clonoth_client:
                ok = await self.rt.clonoth_client.cancel_active_tasks(self.session_id)
            else:
                ok = False
            if not ok:
                await interaction.response.edit_message(content="⚠️ 任务已结束。", view=None)
                return
        except Exception as e:
            await interaction.response.send_message(f"发送取消请求失败: {e}", ephemeral=True)
            return
        await interaction.response.edit_message(content="🛑 正在取消任务...", view=self)


class ApprovalView(_SuperuserView):
    """v2 改动：审批操作改用 ClonothClient 替代直接 httpx 调用。"""

    def __init__(self, rt: Any, approval_id: str):
        super().__init__(rt, timeout=120)
        self.approval_id = approval_id
        self.decided = False
        self._msg: discord.Message | None = None  # 保存消息引用，超时时编辑用

    async def on_timeout(self) -> None:
        """审批按钮超时后自动拒绝，防止后端 task 卡死。

        [AutoC 2026-05-31] Why: 超时后 discord.py 只是灰化按钮，后端
        approval 仍然 pending 导致 task 永久挂起。
        How: 超时时自动调用 deny API 并编辑消息提示。
        Purpose: 防止审批超时卡死整个 task 链。
        """
        if self.decided:
            return
        self.decided = True
        try:
            if self.rt.clonoth_client:
                await self.rt.clonoth_client.approve(
                    self.approval_id, decision="deny",
                    comment="auto-denied: approval timed out (2min)",
                )
        except Exception as e:
            logger.warning("ApprovalView on_timeout deny failed: %s", e)
        # 编辑消息提示超时
        if self._msg:
            try:
                for child in self.children:
                    child.disabled = True
                await self._msg.edit(content="⏰ 审批已超时自动拒绝（2分钟未操作）", view=self)
            except Exception:
                pass

    @discord.ui.button(label="放行", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.decided:
            return
        self.decided = True
        try:
            if self.rt.clonoth_client:
                await self.rt.clonoth_client.approve(
                    self.approval_id, decision="allow",
                    comment="manually approved via Discord",
                )
        except Exception as e:
            await interaction.response.send_message(f"审批失败: {e}", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="拒绝", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.decided:
            return
        self.decided = True
        try:
            if self.rt.clonoth_client:
                await self.rt.clonoth_client.approve(
                    self.approval_id, decision="deny",
                    comment="manually denied via Discord",
                )
        except Exception as e:
            await interaction.response.send_message(f"审批失败: {e}", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ 已拒绝", view=self)
        self.stop()


async def _bridge_handler(rt: Any, request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    code = body.get("code", "")
    if not code:
        return web.json_response({"ok": False, "error": "缺少 code 参数"}, status=400)
    try:
        result = await _exec_code(rt, code)
        text = json.dumps({"ok": True, "result": result}, default=str, ensure_ascii=False)
        return web.Response(text=text, content_type="application/json")
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def _exec_code(rt: Any, code: str) -> Any:
    """在 Bot 进程内执行 async Python 代码。"""
    # [2026-05-14 refactor note] The bridge keeps the old function arguments,
    # but dc_client and _channel_history now come from DiscordRuntime.
    wrapped = "async def __bridge_exec__(client, discord, datetime, asyncio, _channel_history):\n"
    for line in code.split("\n"):
        wrapped += "    " + line + "\n"
    local_ns: dict[str, Any] = {}
    exec(compile(wrapped, "<bridge>", "exec"), {}, local_ns)
    func = local_ns["__bridge_exec__"]
    raw = await asyncio.wait_for(
        func(rt.dc_client, discord, datetime, asyncio, rt.channel_history), timeout=60.0,
    )
    if raw is None:
        return {"done": True}
    if isinstance(raw, (str, int, float, bool)):
        return {"value": raw}
    if isinstance(raw, (list, dict)):
        return raw
    return {"value": str(raw)}


async def _start_bridge(rt: Any) -> None:
    # [2026-05-14 refactor note] on_ready may run again after reconnects, so
    # runtime tracks whether the bridge has already been started.
    if rt.bridge_started:
        return
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        return await _bridge_handler(rt, request)

    app.router.add_post("/discord", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", rt.bridge_port)
    await site.start()
    rt.bridge_started = True
    rt.bridge_runner = runner
    rt.bridge_site = site
    print(f"[bridge] Discord Bridge Server: http://127.0.0.1:{rt.bridge_port}")


async def handle_agent(
    rt: Any,
    message: discord.Message,
    user_input: str,
    channel_id: int,
) -> None:
    """入口：所有请求都走同一个 session，直接并发执行。"""
    conv_key = f"discord:{channel_id}"
    await _handle_agent_inner(rt, message, user_input, channel_id, conv_key)


async def _handle_agent_inner(
    rt: Any,
    message: discord.Message,
    user_input: str,
    channel_id: int,
    conversation_key: str,
) -> None:
    """handle_agent 核心逻辑。"""
    if not rt.clonoth_client or not rt.session_state:
        await message.reply("Clonoth SDK 尚未初始化，请稍候重试。")
        return

    is_dm = isinstance(message.channel, (discord.DMChannel, discord.GroupChannel))

    reply_context = await _build_reply_context(rt, message)
    pre_q = _ensure_channel(rt, channel_id)
    new_wm = pre_q[-1].get("seq", -1) if pre_q else -1
    full_text = _build_context_text(
        rt, channel_id, message.author, user_input,
        reply_context=reply_context,
        exclude_last=True,
    )
    inbound_attachments = await _collect_attachments(rt, message, conversation_key)

    # --- Preempt V2: 打断同 conversation 的 running task ---
    existing_sid = rt.session_state.get_session_id(conversation_key)
    preempt_succeeded = False
    preempted_src_seq = None
    if existing_sid:
        try:
            running_tasks = await rt.clonoth_client.get_running_tasks(existing_sid)
            for running_task in running_tasks:
                if running_task.task_id and running_task.is_user_entry:
                    if not is_dm:
                        rt_src_seq = running_task.source_inbound_seq or 0
                        rt_trigger = rt.session_state.get_trigger(rt_src_seq) if rt_src_seq else None
                        if rt_trigger:
                            rt_msg = rt_trigger.platform_data.get("message")
                            if rt_msg and rt_msg.author.id != message.author.id:
                                continue
                        elif rt_src_seq:
                            # [AutoC 2026-06-24] Trigger reclaimed by stale cleanup — ghost task, skip
                            logger.info("preempt_v2: skip ghost task %s (trigger seq=%s gone)", running_task.task_id[:8], rt_src_seq)
                            continue
                    try:
                        ok = await rt.clonoth_client.preempt_task(
                            running_task.task_id,
                            message=full_text,
                            attachments=inbound_attachments or None,
                        )
                        if ok:
                            preempt_succeeded = True
                            preempted_src_seq = running_task.source_inbound_seq
                            logger.info("preempt_v2: injected into task %s", running_task.task_id[:8])
                    except Exception:
                        pass
        except Exception:
            pass

    if preempt_succeeded:
        session_id = existing_sid
        old_trigger = None
        old_trigger_seq = None
        if preempted_src_seq:
            old_trigger = rt.session_state.get_trigger(preempted_src_seq)
            old_trigger_seq = preempted_src_seq
        # [2026-05-14 refactor note] old_trigger_seq is kept to preserve the
        # old branch structure during the move; the original code did not use it.
        _ = old_trigger_seq

        cancel_view = CancelView(rt, session_id)
        try:
            status_msg = await message.reply("⏳ 处理中...", view=cancel_view)
        except Exception:
            status_msg = None

        if old_trigger:
            old_status = old_trigger.platform_data.get("status_msg")
            if old_status:
                try:
                    await old_status.delete()
                except Exception:
                    pass
            old_trigger.platform_data["message"] = message
            old_trigger.platform_data["status_msg"] = status_msg
            old_trigger.platform_data["cancel_view"] = cancel_view
            old_trigger.platform_data["last_typing_time"] = time.time()
        elif preempted_src_seq:
            new_trigger = TriggerInfo(
                inbound_seq=preempted_src_seq,
                conversation_key=conversation_key,
                session_id=session_id,
                is_dm=is_dm,
                platform_data={
                    "message": message, "channel_id": channel_id,
                    "status_msg": status_msg, "cancel_view": cancel_view,
                    "last_typing_time": time.time(),
                },
            )
            rt.session_state.register_trigger(new_trigger)
            logger.info("preempt_v2: re-registered trigger for src_seq=%s", preempted_src_seq)
        else:
            logger.warning("preempt_v2: no trigger and no src_seq, falling back")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            preempt_succeeded = False

        if preempt_succeeded:
            try:
                async with message.channel.typing():
                    pass
            except Exception:
                pass
            return

    channel_type = "discord_dm" if is_dm else "discord_guild"
    try:
        result = await rt.clonoth_client.submit_inbound(
            channel=channel_type,
            conversation_key=conversation_key,
            text=full_text,
            message_id=str(message.id),
            attachments=inbound_attachments or None,
            use_context=True,
            entry_node_id=rt.entry_node_id,
        )
    except Exception as e:
        await message.reply(f"无法连接到 Clonoth Agent: {e}")
        return

    if not result.session_id:
        await message.reply("Clonoth Agent 未返回 session_id")
        return

    session_id = result.session_id
    my_inbound_seq = result.inbound_seq

    rt.session_state.register_session(conversation_key, session_id)
    if my_inbound_seq:
        rt.session_state.register_watermark(my_inbound_seq, channel_id, new_wm)

    cancel_view = CancelView(rt, session_id)
    if my_inbound_seq:
        trigger = TriggerInfo(
            inbound_seq=my_inbound_seq,
            conversation_key=conversation_key,
            session_id=session_id,
            is_dm=is_dm,
            platform_data={
                "message": message, "channel_id": channel_id,
                "status_msg": None,
                "cancel_view": cancel_view,
                "last_typing_time": time.time(),
            },
        )
        rt.session_state.register_trigger(trigger)

    try:
        status_msg: discord.Message | None = await message.reply("⏳ 处理中...", view=cancel_view)
    except Exception:
        status_msg = None

    try:
        async with message.channel.typing():
            pass
    except Exception:
        pass

    if my_inbound_seq:
        existing = rt.session_state.get_trigger(my_inbound_seq)
        if existing:
            existing.platform_data["status_msg"] = status_msg
        elif status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass


async def handle_model_command(rt: Any, message: discord.Message, raw_text: str) -> bool:
    """Handle !model commands and return True when the message was consumed.

    Default scope is SESSION (current channel). Use --global to modify the
    supervisor-level global config instead.
    """
    if not raw_text.startswith("!model"):
        return False
    if message.author.id not in rt.superusers:
        await message.reply("⛔ Permission denied.", mention_author=False)
        return True
    if not rt.clonoth_client:
        await message.reply("⚠️ SDK 尚未初始化", mention_author=False)
        return True

    # Parse --global flag
    is_global = "--global" in raw_text
    clean_text = raw_text.replace("--global", "").strip()
    parts = clean_text.split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else "show"

    # Resolve session_id for session-scoped operations
    session_id = ""
    conv_key = f"discord:{message.channel.id}"
    if not is_global and hasattr(rt, "session_state") and rt.session_state:
        session_id = rt.session_state.get_session_id(conv_key) or ""
    # Fallback: query supervisor if local cache misses
    if not is_global and not session_id and rt.clonoth_client:
        import logging as _logging
        _log = _logging.getLogger("model_cmd")
        _log.warning("[!model] local cache miss for %s, trying supervisor fallback", conv_key)
        try:
            _http = rt.clonoth_client._http()
            _base = rt.clonoth_client._base_url
            _log.warning("[!model] querying %s/v1/sessions", _base)
            _resp = await _http.get(f"{_base}/v1/sessions", params={"limit": "100"})
            _log.warning("[!model] supervisor returned status=%s, count=%s", _resp.status_code, len(_resp.json()) if _resp.status_code == 200 else "N/A")
            if _resp.status_code == 200:
                for _s in _resp.json():
                    if _s.get("conversation_key") == conv_key:
                        session_id = _s.get("session_id", "")
                        _log.warning("[!model] FOUND session via fallback: %s", session_id)
                        break
                if not session_id:
                    _log.warning("[!model] no match in %d sessions for %s", len(_resp.json()), conv_key)
        except Exception as _e:
            _logging.getLogger("model_cmd").warning("[!model] fallback error: %s: %s", type(_e).__name__, _e)

    if sub == "show":
        try:
            cfg = await rt.clonoth_client.get_openai_config()
            lines = [
                "🤖 **Global Config**",
                f"Model: `{cfg.model}`",
                f"Base URL: `{cfg.base_url}`",
                f"API Key: {'✅ ' + cfg.api_key if cfg.api_key_present else '❌ not set'}",
            ]
            if session_id:
                try:
                    ov = await rt.clonoth_client.get_session_provider_override(session_id)
                    override = ov.get("provider_override", {})
                    if override:
                        lines.append("")
                        lines.append("🔧 **Session Override** (this channel)")
                        for k, v in override.items():
                            display = v if k != "api_key" else ("✅ ..." + str(v)[-4:] if v else "❌")
                            lines.append(f"{k}: `{display}`")
                    else:
                        lines.append("\n📌 Session override: none (using global)")
                except Exception:
                    lines.append("\n📌 Session override: unavailable")
            await message.reply("\n".join(lines), mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    if sub == "set":
        if len(parts) < 3:
            await message.reply("Usage: `!model set <model>` or `!model set <model> --global`", mention_author=False)
            return True
        model_name = parts[2].strip()
        try:
            if is_global:
                resp = await rt.clonoth_client.update_openai_config(model=model_name)
                actual = resp.get("openai", {}).get("model", model_name)
                await message.reply(f"✅ **Global** model → `{actual}`", mention_author=False)
            elif session_id:
                ov = await rt.clonoth_client.get_session_provider_override(session_id)
                current = ov.get("provider_override", {})
                current["model"] = model_name
                await rt.clonoth_client.set_session_provider_override(session_id, current)
                await message.reply(f"✅ **Session** model → `{model_name}`", mention_author=False)
            else:
                await message.reply("⚠️ No session found for this channel. Use `--global` instead.", mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    if sub == "key":
        if len(parts) < 3:
            await message.reply("Usage: `!model key <api_key>` or `!model key <api_key> --global`", mention_author=False)
            return True
        api_key = parts[2].strip()
        try:
            if is_global:
                await rt.clonoth_client.update_openai_config(api_key=api_key)
                await message.reply("✅ **Global** API key updated", mention_author=False)
            elif session_id:
                ov = await rt.clonoth_client.get_session_provider_override(session_id)
                current = ov.get("provider_override", {})
                current["api_key"] = api_key
                await rt.clonoth_client.set_session_provider_override(session_id, current)
                await message.reply("✅ **Session** API key updated", mention_author=False)
            else:
                await message.reply("⚠️ No session found. Use `--global`.", mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    if sub == "url":
        if len(parts) < 3:
            await message.reply("Usage: `!model url <base_url>` or `!model url <base_url> --global`", mention_author=False)
            return True
        base_url = parts[2].strip()
        try:
            if is_global:
                resp = await rt.clonoth_client.update_openai_config(base_url=base_url)
                actual = resp.get("openai", {}).get("base_url", base_url)
                await message.reply(f"✅ **Global** base URL → `{actual}`", mention_author=False)
            elif session_id:
                ov = await rt.clonoth_client.get_session_provider_override(session_id)
                current = ov.get("provider_override", {})
                current["base_url"] = base_url
                await rt.clonoth_client.set_session_provider_override(session_id, current)
                await message.reply(f"✅ **Session** base URL → `{base_url}`", mention_author=False)
            else:
                await message.reply("⚠️ No session found. Use `--global`.", mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    if sub == "clear":
        if not session_id:
            await message.reply("⚠️ No session found for this channel.", mention_author=False)
            return True
        try:
            await rt.clonoth_client.clear_session_provider_override(session_id)
            await message.reply("✅ Session override cleared — using global config", mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    if sub == "provider":
        if len(parts) < 3:
            await message.reply("Usage: `!model provider <type>` (e.g. openai, anthropic, gemini)", mention_author=False)
            return True
        provider_type = parts[2].strip()
        try:
            if is_global:
                await message.reply("⚠️ Global provider change not supported via command. Edit node YAML.", mention_author=False)
            elif session_id:
                ov = await rt.clonoth_client.get_session_provider_override(session_id)
                current = ov.get("provider_override", {})
                current["provider"] = provider_type
                await rt.clonoth_client.set_session_provider_override(session_id, current)
                await message.reply(f"✅ **Session** provider → `{provider_type}`", mention_author=False)
            else:
                await message.reply("⚠️ No session found. Use node YAML.", mention_author=False)
        except Exception as e:
            await message.reply(f"❌ Error: {e}", mention_author=False)
        return True

    await message.reply(
        "**!model** commands (default: session scope):\n"
        "`!model show` — show global + session config\n"
        "`!model set <model>` — change model (session)\n"
        "`!model set <model> --global` — change model (global)\n"
        "`!model key <api_key>` — change API key\n"
        "`!model url <base_url>` — change base URL\n"
        "`!model provider <type>` — change provider type (session)\n"
        "`!model clear` — clear session override\n"
        "Add `--global` to any command to affect global config.",
        mention_author=False,
    )
    return True

