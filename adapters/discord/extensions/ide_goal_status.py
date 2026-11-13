"""One optional, read-only Goal card; ordinary triggers and messages remain untouched."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time

import discord
import httpx

log = logging.getLogger(__name__)
LABELS = {"empty": "已清除", "running": "执行中", "waiting": "等待结果", "reviewing": "评审中",
          "paused": "已暂停", "blocked": "受阻", "completed": "已完成"}
ACTIVE = {"running", "waiting", "reviewing"}


class GoalStatusCard:
    """Subscribe before POST, but claim a Goal only after its creation is acknowledged."""

    def __init__(self, rt, message, sid):
        self.rt, self.message, self.sid = rt, message, sid
        self.conversation_key = f"discord:{message.channel.id}"
        self.progress = OrderedDict()
        self.data = None
        self.goal_id = None
        self.task = None
        self.closed = False
        self.error = ""
        self.last_render = None
        self.frozen_progress = None
        self.last_presence_check = time.monotonic()
        self.dispose = rt.callbacks.subscribe_progress(self.observe)

    def observe(self, trigger, text, terminal):
        # Never attach the card to a normal trigger or its status_msg/cancel_view.
        if self.closed or trigger.conversation_key != self.conversation_key or not trigger.platform_data.get("background"):
            return
        previous = self.progress.get(trigger.inbound_seq)
        if previous and previous[1]:
            return  # A delayed sweep cannot restart a finished round's animation.
        self.progress[trigger.inbound_seq] = (text, terminal)
        self.progress.move_to_end(trigger.inbound_seq)
        while len(self.progress) > 32:
            self.progress.popitem(last=False)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.dispose()
        self.progress.clear()
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()

    def start(self, data):
        if not isinstance(data.get("goal_id"), str) or not data["goal_id"]:
            self.close()
            return False
        self.goal_id = data["goal_id"]
        self.data = data
        self.task = asyncio.create_task(self.run())
        return True

    def embed(self):
        data = self.data
        embed = discord.Embed(title="IDE 扩展 · Goal",
                              description=discord.utils.escape_markdown(data.get("goal", ""))[:1800],
                              colour=0xFEAF2C)
        status = data["status"]
        embed.add_field(name="目标状态", value=self.error or LABELS[status], inline=True)
        embed.add_field(name="自动续轮", value=f"{data.get('rounds', 0)}/{data.get('max_rounds', 8)}", inline=True)
        if data.get("pause_reason"):
            embed.add_field(name="原因", value=discord.utils.escape_markdown(data["pause_reason"])[:1000], inline=False)
        execution = data.get("execution") or {}
        seq = execution.get("source_inbound_seq")
        selected = self.progress.get(seq, ("", False))
        if status not in {"running", "waiting"} or self.error:
            key = (seq, status, self.error)
            if self.frozen_progress is None or self.frozen_progress[0] != key:
                self.frozen_progress = (key, selected)
            selected = self.frozen_progress[1]
        else:
            self.frozen_progress = None
        progress, terminal = selected
        progress = progress[:1800]
        if progress:
            label = "最后进度" if status not in ACTIVE or self.error else "本轮已结束" if terminal else "当前轮进度"
            for offset in range(0, len(progress[:1800]), 1024):
                embed.add_field(name=label if offset == 0 else "进度（续）", value=progress[offset:offset + 1024], inline=False)
        if status in ACTIVE and not self.error:
            if not terminal and status != "reviewing":
                started = execution.get("started_at")
                if isinstance(started, (int, float)) and started > 0:
                    embed.add_field(name="本轮用时", value=f"⏱ {max(0, int(time.time() - started))}s", inline=True)
            if not progress:
                hint = "正在独立评审，尚未通过验收。" if status == "reviewing" else "等待本轮执行进度。"
                embed.add_field(name="当前轮进度", value=hint, inline=False)
        embed.set_footer(text="每轮回复仍正常发送；本轮结束不代表 Goal 验收通过。")
        return embed

    async def render(self, *, final=False):
        embed = self.embed()
        signature = embed.to_dict()
        if signature == self.last_render:
            return
        # Reuse the adapter's channel edit budget without consuming ordinary keys.
        callbacks = self.rt.callbacks
        if not final and not callbacks._should_edit(f"goal-card:{self.message.id}", min_interval=3.0,
                                                    channel_id=self.message.channel.id):
            return
        await self.message.edit(content=None, embed=embed, view=None,
                                allowed_mentions=discord.AllowedMentions.none())
        self.last_render = signature

    async def refresh(self):
        response = await self.rt.clonoth_client._http().get(
            self.rt.clonoth_client.base_url + "/v1/plugins/ide/goal", params={"session_id": self.sid},
        )
        response.raise_for_status()
        data = response.json()
        if (data.get("session_id") != self.sid or type(data.get("revision")) is not int
                or data.get("status") not in LABELS):
            raise ValueError("Goal 状态响应无效")
        if data["revision"] < self.data["revision"]:
            return False  # A delayed read must never make the card go backwards.
        if data.get("goal_id") != self.goal_id or data["status"] == "empty":
            self.error = "此 Goal 已清除或被新目标替换，停止更新。"
            return True
        self.data = data
        self.error = ""
        execution = data.get("execution") or {}
        seq = execution.get("source_inbound_seq")
        # A switched entry node uses the SDK's existing child-progress path.
        # Look up the exact backend root, never another task in the same session.
        if seq and data["status"] in {"running", "waiting"} and not self.progress.get(seq, ("", False))[1]:
            getter = getattr(self.rt.callbacks, "get_task_progress", None)
            snapshot = getter(execution["task_id"]) if callable(getter) else None
            if snapshot:
                text, root_ended = snapshot
                if root_ended:
                    text = "根任务已结束，仍需等待输出和后续结果。\n" + text
                # Child lifecycle ends the root, not its output chain. Only the
                # main progress delivery callback can mark the whole round ended.
                self.progress[seq] = (text[:1800], False)
        return data["status"] == "completed"

    async def run(self):
        failures = 0
        edit_failures = 0
        try:
            # No special SDK routing: exact execution seq selects cached, ordinary
            # progress notifications even when the first task outran the POST reply.
            while not self.closed:
                stop = False
                try:
                    stop = await self.refresh()
                    failures = 0
                except httpx.HTTPStatusError as exc:
                    failures += 1
                    self.error = "IDE Goal 服务不可用，状态未确认。"
                    stop = exc.response.status_code in {401, 403, 404, 503}
                except (httpx.HTTPError, ValueError, TypeError):
                    failures += 1
                    self.error = "读取未确认，保留上次状态。" if failures < 3 else "多次读取失败，停止更新，请到 Web 核对。"
                try:
                    # Unchanged paused cards are not edited. Check existence at a
                    # low frequency so deletion still releases polling/listeners.
                    if time.monotonic() - self.last_presence_check >= 60:
                        await self.message.channel.fetch_message(self.message.id)
                        self.last_presence_check = time.monotonic()
                    await self.render()
                    if stop or failures >= 3:
                        # Terminal edits have the same reserved margin as ordinary progress.
                        if self.last_render != self.embed().to_dict():
                            await asyncio.sleep(5)
                            await self.render(final=True)
                        return
                    edit_failures = 0
                except (discord.NotFound, discord.Forbidden):
                    raise
                except discord.HTTPException:
                    edit_failures += 1
                    if edit_failures >= 3:
                        log.warning("Goal card stopped after three Discord HTTP failures")
                        return
                await asyncio.sleep(3 if self.data["status"] in ACTIVE else 10)
        except discord.NotFound:
            pass  # A deleted card must not be recreated or produce another message.
        except discord.Forbidden:
            log.warning("Goal card edit permission was revoked")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Goal status card stopped")
        finally:
            self.close()
            self.rt.callbacks._last_edit_time.pop(f"goal-card:{self.message.id}", None)


def prepare(rt, message, sid, data):
    if (data.get("capabilities", {}).get("progress") is not True
            or not callable(getattr(getattr(rt, "callbacks", None), "subscribe_progress", None))):
        return None
    return GoalStatusCard(rt, message, sid)
