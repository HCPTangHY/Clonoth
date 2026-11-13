"""Optional IDE Goal bridge: HTTP only, no imports from plugins.ide or core changes."""
from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
import httpx

from ..context import register_transient_msg

log = logging.getLogger(__name__)
GOAL_PATH = "/v1/plugins/ide/goal"
TIMEOUT = 120


class GoalError(Exception):
    """A safe, user-facing precondition or response error."""


async def _request(rt, method, path, **kwargs):
    response = await rt.clonoth_client._http().request(
        method, rt.clonoth_client.base_url + path, **kwargs,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise GoalError("服务返回无效响应，请核对状态后重试。") from None


def _failure(error, *, submitted=False):
    if isinstance(error, GoalError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if submitted and status >= 500:
            return "创建结果未确认：服务异常，目标可能已保存。请到 Web 核对后再操作，不要盲目重发；本次不会自动重试。"
        if status in (404, 503):
            return "IDE Goal 扩展未安装、未启用或服务尚未就绪。请核对插件和目标状态后重新发起。"
        if status == 409:
            return "目标或会话已变化，本次确认不再有效。请核对后重新发送 !goal。"
        if status in (401, 403):
            return "服务拒绝访问，请检查 Bot 接口授权；不会改变现有工具审批规则。"
        if status == 422:
            return "创建请求被拒绝，请检查正文或更新 IDE 插件；不会改用旧提交接口。"
    if submitted:
        return "创建结果未确认（网络或服务异常）。请先核对目标状态，不要盲目重发；本次不会自动重试。"
    return "无法读取 Goal 服务，未提交创建请求，请稍后重新发起。"


async def _notice(rt, message, text):
    notice = await message.reply(
        "[IDE 扩展] " + text, mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    register_transient_msg(rt, notice)


async def _available(rt):
    listing = await _request(rt, "GET", "/v1/plugins")
    if not any(p.get("name") == "ide" for p in listing.get("plugins", [])):
        raise GoalError("IDE Goal 扩展未安装或未启用，!goal 当前不可用。")


async def _session(rt, channel_type, conversation_key):
    result = await _request(rt, "POST", "/v1/sessions/get_or_create", json={
        "channel": channel_type, "conversation_key": conversation_key,
    })
    sid = result.get("session_id")
    if not isinstance(sid, str) or not sid or result.get("conversation_key") != conversation_key:
        raise GoalError("无法确认当前频道会话，未提交 Goal。")
    return sid


def _check_goal(data, sid):
    if (data.get("session_id") != sid or data.get("is_child")
            or type(data.get("revision")) is not int or data["revision"] < 0):
        raise GoalError("Goal 会话或版本无效，未提交创建请求。")
    if data.get("capabilities", {}).get("create") is not True:
        raise GoalError("当前 IDE 插件不支持独立创建，请更新插件；不会退回 submit。")
    if data.get("status") not in {"empty", "completed"}:
        raise GoalError("当前频道已有未完成 Goal，不能覆盖。请通过 Web 处理现有目标。")


async def _pristine(rt, sid, node):
    # Do not confuse an existing explicit binding to the default with a new session.
    events = await _request(rt, "GET", f"/v1/sessions/{sid}/events", params={"limit": 2})
    return (not node.get("is_override") and node.get("node_id") == node.get("default_node_id")
            and len(events) == 1 and events[0].get("type") == "session_created")


class GoalConfirmView(discord.ui.View):
    def __init__(self, rt, message, goal, data, channel_type, *, new_session, node):
        # View's native idle timeout can be extended by interactions. This deadline cannot.
        super().__init__(timeout=None)
        self.rt = rt
        self.origin = message
        self.author_id = message.author.id
        self.channel_id = message.channel.id
        self.conversation_key = f"discord:{self.channel_id}"
        self.channel_type = channel_type
        self.new_session = new_session
        self.node = node
        self.sid = data["session_id"]
        self.body = {"action": "create", "goal": goal, "expected_session_id": self.sid,
                     "expected_revision": data["revision"],
                     "request_id": f"discord-goal:{self.channel_id}:{message.id}"}
        self.deadline = time.monotonic() + TIMEOUT
        self.decided = False
        self.card = None
        self.expiry = asyncio.get_running_loop().call_later(
            TIMEOUT, lambda: asyncio.create_task(self.on_timeout()),
        )

    def stop(self):
        self.expiry.cancel()
        super().stop()

    def _settle(self):
        self.decided = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有命令发起者可以操作此确认卡。", ephemeral=True)
            return False
        if self.decided or time.monotonic() >= self.deadline:
            await interaction.response.send_message("此确认卡已处理或过期，请重新发起。", ephemeral=True)
            if not self.decided:
                await self.on_timeout()
            return False
        return True

    async def on_timeout(self):
        if self.decided:
            return
        self._settle()
        if self.card:
            try:
                await self.card.edit(content="[IDE 扩展] 确认已超时，未提交创建请求。", view=self)
            except discord.HTTPException:
                log.warning("Goal confirmation card could not be expired", exc_info=True)

    @discord.ui.button(label="确认创建并开始", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        # Lock synchronously before the first I/O; two simultaneous clicks cannot submit twice.
        self._settle()
        await interaction.response.defer()
        submitted = False
        monitor = None
        result = None
        try:
            await _available(self.rt)
            sid = await _session(self.rt, self.channel_type, self.conversation_key)
            if sid != self.sid:
                raise GoalError("频道会话已重置或替换，本次确认已失效，请重新发起。")
            latest = await _request(self.rt, "GET", GOAL_PATH, params={"session_id": self.sid})
            _check_goal(latest, self.sid)
            if latest["revision"] != self.body["expected_revision"]:
                raise GoalError("目标已变化，本次确认已失效，请核对后重新发起。")
            active = await _request(self.rt, "GET", f"/v1/sessions/{self.sid}/active_node")
            if active.get("node_id") != self.node.get("node_id"):
                raise GoalError("会话入口节点已变化，请核对后重新发起。")
            if self.new_session and self.rt.entry_node_id != active.get("node_id"):
                if not await _pristine(self.rt, self.sid, active):
                    raise GoalError("新会话已被其他操作使用或绑定，请核对入口后重新发起。")
                switched = await _request(self.rt, "POST", f"/v1/sessions/{self.sid}/switch_node",
                                          json={"target_node_id": self.rt.entry_node_id})
                if switched.get("ok") is not True or switched.get("node_id") != self.rt.entry_node_id:
                    raise GoalError("Discord 入口节点初始化失败，未提交创建请求。")
            # Register ownership before POST: the normal SDK background route sends subsequent replies.
            self.rt.session_state.register_session(self.conversation_key, self.sid)
            if self.channel_type == "discord_dm":
                self.rt.session_state.register_dm_channel(self.sid, self.channel_id)
            from .ide_goal_status import prepare
            monitor = prepare(self.rt, self.card, self.sid, latest)
            submitted = True
            result = await _request(self.rt, "POST", GOAL_PATH, params={"session_id": self.sid}, json=self.body)
            if (result.get("session_id") != self.sid or type(result.get("revision")) is not int
                    or result["revision"] <= self.body["expected_revision"]):
                raise GoalError("创建结果未确认，请先核对目标状态；本次不会自动重试。")
            status = result.get("status")
            if status in {"running", "waiting", "reviewing"}:
                text = "Goal 已开始推进；执行结果将发送到当前频道（不代表目标验收通过）。"
            elif status == "completed":
                text = "创建请求已接受；服务返回此 Goal 已完成。未重新启动或重复创建。"
            else:
                label = {"paused": "已暂停", "blocked": "受阻", "empty": "未设置"}.get(status, "未知")
                text = f"服务返回目标状态：{label}，并非已开始。请到 Web 核对；本次不会自动重试。"
        except asyncio.CancelledError:
            if monitor:
                monitor.close()
            raise
        except (GoalError, httpx.HTTPError) as error:
            result = None
            text = _failure(error, submitted=submitted)
        except Exception:
            result = None
            log.exception("Discord Goal confirmation failed")
            text = _failure(None, submitted=submitted)
        try:
            if monitor and result is not None and monitor.start(result):
                return  # The monitor is the only writer after successful creation.
            await interaction.edit_original_response(content="[IDE 扩展] " + text, view=None,
                                                     allowed_mentions=discord.AllowedMentions.none())
        finally:
            if monitor and monitor.task is None:
                monitor.close()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        self._settle()
        await interaction.response.edit_message(content="[IDE 扩展] 已取消，未提交创建请求。", view=self)


async def handle(rt, message, goal):
    if not goal:
        await _notice(rt, message, "用法：!goal 目标正文。确认后创建并开始。")
        return
    if message.attachments:
        await _notice(rt, message, "目前只支持目标正文，不支持附件。请移除附件后重新发送。")
        return
    if len(goal) > 16000:
        await _notice(rt, message, "目标正文最多 16000 字符。")
        return
    if not rt.clonoth_client or not rt.session_state:
        await _notice(rt, message, "SDK 尚未初始化，请稍后重试。")
        return
    channel_type = "discord_dm" if isinstance(message.channel, (discord.DMChannel, discord.GroupChannel)) else "discord_guild"
    conversation_key = f"discord:{message.channel.id}"
    try:
        await _available(rt)
        sid = await _session(rt, channel_type, conversation_key)
        data = await _request(rt, "GET", GOAL_PATH, params={"session_id": sid})
        _check_goal(data, sid)
        node = await _request(rt, "GET", f"/v1/sessions/{sid}/active_node")
        new_session = await _pristine(rt, sid, node)
    except (GoalError, httpx.HTTPError) as error:
        await _notice(rt, message, _failure(error))
        return
    view = GoalConfirmView(rt, message, goal, data, channel_type, new_session=new_session, node=node)
    shown = discord.utils.escape_markdown(goal)
    files = []
    if len(shown) > 3500:
        shown = shown[:3300] + "\n\n（预览已截断，请核对随卡附上的完整目标 goal.txt。）"
        files.append(discord.File(io.BytesIO(goal.encode("utf-8")), filename="goal.txt"))
    embed = discord.Embed(title="IDE 扩展 · 创建 Goal", description=shown, colour=0xFEAF2C)
    node_label = node.get("node_id", "未知")
    if new_session:
        node_label = rt.entry_node_id
    embed.add_field(name="范围", value=f"当前频道 <#{message.channel.id}> · 入口 `{node_label}`", inline=False)
    operation = "确认后创建并立即开始执行。工具审批规则保持不变。"
    if data["status"] == "completed":
        operation += "\n将替换上一个已完成目标，重置执行预算及 Goal 专属评审/证据记录；聊天历史和 Todo 保留。"
    embed.add_field(name="操作", value=operation, inline=False)
    embed.set_footer(text="仅发起者可确认或取消 · 120 秒后失效 · 修改原消息不会改变本卡目标")
    try:
        view.card = await message.reply(embed=embed, view=view, files=files, mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none())
        register_transient_msg(rt, view.card)
    except Exception:
        view.stop()
        raise
    finally:
        for file in files:
            file.close()
