"""EventRouter callbacks for the Discord adapter.

[2026-05-14 refactor note] DiscordCallbacks now receives DiscordRuntime, so every
SDK object, Discord client, channel cache, and display setting is read through
one runtime object instead of module-level globals.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import discord  # type: ignore[import-untyped]

from clonoth_sdk import ChildTaskState, MainTaskState, TriggerInfo
from clonoth_sdk.types import Event

from .messaging import (
    _atts_to_discord_files,
    _extract_reactions,
    _format_progress_log,
    _format_status_summary,
    _record_bot_reply,
    _safe_restart,
    _send_split_text,
)


logger = logging.getLogger("discord_bot")

# [2026-05-20 edit-rate-limit] Why: Discord allows roughly 5 message edits per
# channel per 5 seconds, and parallel task logs can exhaust that budget. How: use
# a shared per-channel token bucket capped at 4 edits per 5 seconds. Purpose:
# leave one edit of margin for forced final/status updates that intentionally
# bypass this helper.
_CHANNEL_EDIT_LIMIT = 4
_CHANNEL_EDIT_WINDOW = 5.0


def _clip(text: str, limit: int = 80) -> str:
    s = text.replace("\n", " ").strip()
    return s[:limit] + "..." if len(s) > limit else s


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


def _tool_inline_summary(tool_name: str, args: dict) -> str:
    """tool_call_start 时生成工具摘要，复用前端 ToolCallCard 的逻辑。"""
    name = tool_name or "unknown"
    if name == "read_file":
        files = args.get("files") or []
        if isinstance(files, list) and len(files) > 1:
            return f"阅读 {len(files)} 文件"
        p = str(args.get("path") or (files[0].get("path", "") if files and isinstance(files[0], dict) else ""))
        if not p:
            return "阅读"
        fn = _basename(p)
        sl = args.get("start_line") or args.get("startLine") or (files[0].get("startLine") if files and isinstance(files[0], dict) else None)
        el = args.get("end_line") or args.get("endLine") or (files[0].get("endLine") if files and isinstance(files[0], dict) else None)
        if sl and el:
            return f"阅读 {fn} L{sl}-{el}"
        if sl:
            return f"阅读 {fn} L{sl}+"
        return f"阅读 {fn}"
    if name == "write_file":
        p = str(args.get("path") or "")
        return f"写入 {_basename(p)}" if p else "写入"
    if name == "apply_diff":
        p = str(args.get("path") or "")
        diffs = args.get("diffs")
        n = len(diffs) if isinstance(diffs, list) else 0
        fn = _basename(p)
        return f"修改 {fn} · {n} 处" if fn else "修改"
    if name == "execute_command":
        cmd = str(args.get("command") or "")
        # 取第一行核心命令
        first = cmd.split("\n")[0].split("&&")[0].split("|")[0].strip()
        return _clip(first, 60) if first else "执行"
    if name in ("grep", "search_in_files"):
        q = str(args.get("query") or "")[:25]
        return f'搜索 "{q}"' if q else "搜索"
    if name == "list_dir":
        p = str(args.get("path") or "")
        paths = args.get("paths")
        if isinstance(paths, list) and len(paths) > 1:
            return f"列出 {len(paths)} 目录"
        target = p or (str(paths[0]) if isinstance(paths, list) and paths else "")
        return f"列出 {_basename(target)}/" if target else "列出"
    if name == "save_memory":
        return f'写入记忆 {str(args.get("id") or "")[:30]}'
    if name == "delete_memory":
        return f'删除记忆 {str(args.get("id") or "")[:30]}'
    if name == "read_image":
        p = str(args.get("image_path") or "")
        return f"读取图片 {_basename(p)}" if p else "读取图片"
    if name == "discord_manage":
        port = args.get("port", "")
        return f"Discord({port})" if port else "Discord"
    if name.startswith("dispatch_to_") or name.startswith("dispatch:"):
        target = name.replace("dispatch_to_", "").replace("dispatch:", "")
        inst = str(args.get("instruction") or args.get("text") or "")[:30]
        return f"委派 {target}: {inst}" if inst else f"委派 {target}"
    if name.startswith("mcp_"):
        provider = name.split("_")[1] if "_" in name else "MCP"
        # 取第一个有意义的参数
        for k, v in args.items():
            if k == "params" and isinstance(v, dict):
                for pk, pv in v.items():
                    s = str(pv)[:30]
                    if s:
                        return f"{provider}: {s}"
            elif v:
                s = str(v)
                if len(s) <= 40 and "\n" not in s:
                    return f"{provider}: {s[:30]}"
        return provider
    # 通用 fallback
    return name


class DiscordCallbacks:
    """Discord 平台适配器回调实现。"""

    def __init__(self, rt: Any) -> None:
        # [2026-05-14 refactor note] Runtime ownership replaces the old module
        # globals while preserving the callback state dictionaries themselves.
        self.rt = rt
        self._last_edit_time: dict[int | str, float] = {}
        # [2026-05-20 edit-rate-limit] Why: per-key throttling is insufficient
        # when multiple task keys edit messages in the same Discord channel. How:
        # store recent edit timestamps by channel id in deques. Purpose: enforce
        # the shared Discord channel budget without affecting non-edit sends.
        self._channel_edit_timestamps: dict[int, deque[float]] = {}
        self._dot_states: dict[int, dict[str, Any]] = {}
        self._child_dot_states: dict[str, dict[str, Any]] = {}

    def _should_edit(self, key: int | str, min_interval: float = 1.0, *, channel_id: int = 0) -> bool:
        """检查 per-key 节流和可选的 per-channel edit 令牌桶。"""
        now = time.time()
        last = self._last_edit_time.get(key, 0)
        # [2026-05-20 edit-rate-limit] Why: each log message should still avoid
        # excessive redraws. How: keep the existing per-key interval check, but
        # lower the default to one second. Purpose: allow smoother websocket
        # progress while preserving local debounce behavior.
        if now - last < min_interval:
            return False

        if channel_id:
            bucket = self._channel_edit_timestamps.setdefault(channel_id, deque())
            # [2026-05-20 edit-rate-limit] Why: old edits should not consume the
            # current Discord window. How: discard timestamps outside the rolling
            # window before counting. Purpose: enforce 4 edits per 5 seconds per
            # channel while automatically recovering after the window passes.
            while bucket and bucket[0] < now - _CHANNEL_EDIT_WINDOW:
                bucket.popleft()
            if len(bucket) >= _CHANNEL_EDIT_LIMIT:
                return False
            bucket.append(now)

        self._last_edit_time[key] = now
        return True

    def _mark_edited(self, key: int | str) -> None:
        """手动标记已 edit（用于不经 _should_edit 的强制 edit 场景）。"""
        self._last_edit_time[key] = time.time()

    def _get_or_create_dot_state(self, seq: int) -> dict[str, Any]:
        """获取或创建主节点 dot_state。"""
        if seq not in self._dot_states:
            self._dot_states[seq] = {
                "dot_step": 0, "had_stream_activity": False,
                "thinking_start_time": time.time(),
                "thinking_preview": "", "text_preview": "",
                "prose_lines": [], "tool_lines": [],
            }
        return self._dot_states[seq]

    def _get_or_create_child_dot_state(self, task_key: str) -> dict[str, Any]:
        """获取或创建子节点 dot_state。"""
        if task_key not in self._child_dot_states:
            self._child_dot_states[task_key] = {
                "dot_step": 0, "had_stream_activity": False,
                "thinking_start_time": 0,
                "thinking_preview": "", "text_preview": "",
                "prose_lines": [], "tool_lines": [],
            }
        return self._child_dot_states[task_key]



    async def _get_log_channel(self) -> Any:
        """获取日志频道对象。"""
        if not self.rt.agent_log_channel_id:
            return None
        ch = self.rt.dc_client.get_channel(self.rt.agent_log_channel_id)
        if ch is None:
            try:
                ch = await self.rt.dc_client.fetch_channel(self.rt.agent_log_channel_id)
            except Exception:
                ch = None
        return ch

    async def _resolve_channel_from_conv_key(self, conv_key: str) -> Any:
        """从 conversation_key 解析 Discord 频道对象。"""
        if not conv_key.startswith("discord:"):
            return None
        try:
            ch_id = int(conv_key[len("discord:"):])
            ch = self.rt.dc_client.get_channel(ch_id)
            if ch is None:
                ch = await self.rt.dc_client.fetch_channel(ch_id)
            return ch
        except Exception:
            return None

    async def _settle_log_msg(
        self,
        state: MainTaskState | ChildTaskState,
        status: str,
    ) -> None:
        """统一处理日志消息的终态转换（保留不删除）。"""
        log_msg = state.platform_data.get("log_msg") or state.platform_data.get("msg")
        if not log_msg:
            return
        try:
            if isinstance(state, MainTaskState):
                display = _format_progress_log("Agent 执行中", state.progress_records, status=status)
            else:
                display = _format_progress_log(state.prefix, state.lines, status=status)
            await log_msg.edit(content=display)
        except Exception:
            pass

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        main_state: MainTaskState | None = None,
    ) -> None:
        pd = trigger.platform_data
        msg: discord.Message = pd.get("message")
        channel_id: int = pd.get("channel_id", 0)

        status_msg = pd.get("status_msg")
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        if main_state:
            await self._settle_log_msg(main_state, "✓ 完成")

        should_restart = False
        if text and text.rstrip().endswith(self.rt.restart_signal):
            should_restart = True
            text = text.rstrip()[:-len(self.rt.restart_signal)].strip()

        if text:
            text, reacts = _extract_reactions(self.rt, text)
            for r in reacts:
                try:
                    await msg.add_reaction(r.strip())
                except Exception:
                    pass

        ch = msg.channel
        if text or attachments:
            files = _atts_to_discord_files(self.rt, attachments) if attachments else []
            try:
                await _send_split_text(self.rt, ch, text or "", reply_to=msg, files=files or None)
            except Exception:
                pass
            if text:
                _record_bot_reply(self.rt, channel_id, text)

        seq = trigger.inbound_seq
        self._dot_states.pop(seq, None)
        self._last_edit_time.pop(seq, None)

        if should_restart:
            ch = msg.channel
            if self.rt.bot_restart_view_factory:
                view = self.rt.bot_restart_view_factory(channel_id)
                confirm_msg = await ch.send("🔄 **Bot 重启需要管理员确认**", view=view)
                view._msg = confirm_msg
            else:
                asyncio.create_task(_safe_restart(self.rt, channel_id=channel_id, delay=2.0))

        logger.info("send_reply ch=%s seq=%s", channel_id, seq)

    async def send_intermediate_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        pd = trigger.platform_data
        msg: discord.Message = pd.get("message")
        channel_id: int = pd.get("channel_id", 0)

        dc_files = _atts_to_discord_files(self.rt, attachments or [])

        if text or dc_files:
            if text:
                text, reacts = _extract_reactions(self.rt, text)
                for r in reacts:
                    try:
                        await msg.add_reaction(r.strip())
                    except Exception:
                        pass
            if text or dc_files:
                int_sent_id = None
                try:
                    int_sent = await _send_split_text(
                        self.rt, msg.channel, text or "",
                        reply_to=msg, files=dc_files or None,
                    )
                    if int_sent:
                        int_sent_id = int_sent[0].id
                except Exception:
                    pass
                if text:
                    _record_bot_reply(self.rt, channel_id, text, msg_id=int_sent_id)

        logger.info("send_intermediate_reply ch=%s atts=%d", channel_id, len(dc_files))

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        node_id: str = "",
    ) -> None:
        if not conversation_key.startswith("discord:"):
            logger.debug("send_to_channel SKIP non-discord conv_key=%s", conversation_key)
            return

        poller_restart = False
        if text and text.rstrip().endswith(self.rt.restart_signal):
            poller_restart = True
            text = text.rstrip()[:-len(self.rt.restart_signal)].strip()

        if text:
            text, _ = _extract_reactions(self.rt, text)

        if text and node_id and node_id != self.rt.entry_node_id:
            prefix = self.rt.child_node_display_names.get(node_id, f"🔧 [{node_id}]")
            text = f"{prefix}\n{text}"

        ch = await self._resolve_channel_from_conv_key(conversation_key)
        if ch:
            files = _atts_to_discord_files(self.rt, attachments) if attachments else []
            try:
                await _send_split_text(self.rt, ch, text or "", files=files or None)
            except Exception as e:
                logger.error("send_to_channel failed: %s", e)
        else:
            logger.warning("send_to_channel: channel not found for %s", conversation_key)

        if poller_restart:
            try:
                ch_id = int(conversation_key[len("discord:"):])
                asyncio.create_task(_safe_restart(self.rt, channel_id=ch_id, delay=2.0))
            except ValueError:
                pass

    async def delete_status_message(
        self,
        trigger: TriggerInfo,
    ) -> None:
        pd = trigger.platform_data
        seq = trigger.inbound_seq
        dot = self._dot_states.get(seq)
        status_msg = pd.get("status_msg")
        if status_msg:
            # 尝试保留已生成的部分文本（避免超时后全吞）
            partial = (dot.get("text_preview", "") if dot else "").strip()
            if partial:
                content = partial + "\n\n⚠️ 回复被截断，请求超时。"
            else:
                content = "⚠️ Agent 请求超时。"
            try:
                await status_msg.edit(content=content, view=None)
            except Exception:
                pass

        stale_main = pd.get("_stale_main_state")
        if stale_main:
            await self._settle_log_msg(stale_main, "⚠️ 超时")

        self._dot_states.pop(seq, None)
        self._last_edit_time.pop(seq, None)

    async def edit_status_message(
        self,
        trigger: TriggerInfo,
        content: str,
    ) -> None:
        status_msg = trigger.platform_data.get("status_msg")
        if status_msg:
            try:
                await status_msg.edit(content=content, view=None)
            except Exception:
                pass

    async def update_progress(
        self,
        trigger: TriggerInfo,
        state: MainTaskState,
    ) -> None:
        seq = trigger.inbound_seq
        pd = trigger.platform_data
        dot_state = self._get_or_create_dot_state(seq)

        if trigger.is_dm:
            # 私聊：status_msg 显示全部进度（stream preview + 工具 + 日志）
            status_msg = pd.get("status_msg")
            ch_id = status_msg.channel.id if status_msg else 0
            if not self._should_edit(seq, channel_id=ch_id):
                return
            display = _format_progress_log(
                "Agent 执行中", state.progress_records, dot_state=dot_state,
            )
            if display and status_msg:
                try:
                    await status_msg.edit(content=display)
                except Exception as e:
                    logger.error("update_progress DM edit FAILED: %s", e)
        else:
            # 群聊：日志频道 log_msg + 触发频道 status_msg 分开更新
            # 1) 日志频道：stream preview + 计时器 + handoff 日志
            log_ch_id = self.rt.agent_log_channel_id or 0
            if log_ch_id and self._should_edit(f"log:{seq}", channel_id=log_ch_id):
                log_display = _format_progress_log(
                    "Agent 执行中", state.progress_records, dot_state=dot_state,
                )
                if log_display:
                    try:
                        log_msg = state.platform_data.get("log_msg")
                        log_ch = await self._get_log_channel()
                        if log_ch:
                            if log_msg:
                                await log_msg.edit(content=log_display)
                            else:
                                state.platform_data["log_msg"] = await log_ch.send(log_display)
                    except Exception as e:
                        logger.error("update_progress log edit FAILED: %s", e)
            # 2) 触发频道：工具摘要 + free prose
            status_msg = pd.get("status_msg")
            if status_msg:
                st_ch_id = status_msg.channel.id
                if self._should_edit(f"status:{seq}", channel_id=st_ch_id):
                    status_display = _format_status_summary(dot_state)
                    if status_display:
                        try:
                            await status_msg.edit(content=status_display)
                        except Exception as e:
                            logger.error("update_progress status edit FAILED: %s", e)

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: TriggerInfo | None = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        log_ch = None
        child_is_dm = False
        if trigger and trigger.is_dm:
            child_is_dm = True
            log_ch = trigger.platform_data.get("message")
            if log_ch:
                log_ch = log_ch.channel
        elif not trigger and self.rt.session_state and self.rt.session_state.get_dm_channel(session_id):
            child_is_dm = True
            dm_ch_id = self.rt.session_state.get_dm_channel(session_id)
            log_ch = self.rt.dc_client.get_channel(dm_ch_id)
            if log_ch is None:
                try:
                    log_ch = await self.rt.dc_client.fetch_channel(dm_ch_id)
                except Exception:
                    log_ch = None
        else:
            log_ch = await self._get_log_channel()

        if not log_ch:
            return

        state.is_dm = child_is_dm  # persist for finalize_child_progress
        child_dot = self._get_or_create_child_dot_state(task_key)
        display = _format_progress_log(state.prefix, state.lines, dot_state=child_dot)
        try:
            sent = await log_ch.send(display)
            state.platform_data["msg"] = sent
            self._mark_edited(task_key)
        except Exception as e:
            logger.error("create_child_progress send failed: %s", e)

    async def update_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
    ) -> None:
        msg = state.platform_data.get("msg")
        # [2026-05-20 edit-rate-limit] Why: child tasks are often concurrent and
        # can target the same log channel. How: derive the channel id from the
        # stored progress message before calling the shared edit gate. Purpose:
        # let child progress edits participate in the per-channel token bucket.
        ch_id = msg.channel.id if msg else 0
        if not self._should_edit(task_key, channel_id=ch_id):
            return

        if not msg:
            return

        child_dot = self._child_dot_states.get(task_key)
        display = _format_progress_log(state.prefix, state.lines, dot_state=child_dot)
        if display:
            try:
                await msg.edit(content=display)
            except Exception:
                pass

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        msg = state.platform_data.get("msg")
        is_dm = getattr(state, "is_dm", is_dm)
        if msg:
            try:
                if is_dm:
                    await msg.delete()
                else:
                    child_dot = self._child_dot_states.get(task_key)
                    display = _format_progress_log(
                        state.prefix, state.lines,
                        dot_state=child_dot, status=status,
                    )
                    await msg.edit(content=display)
            except Exception:
                pass

        self._child_dot_states.pop(task_key, None)
        self._last_edit_time.pop(task_key, None)

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        ch = None

        if self.rt.session_state:
            result = self.rt.session_state.find_trigger_by_session(session_id)
            if result:
                _, trig = result
                ch = trig.platform_data.get("message")
                if ch:
                    ch = ch.channel

        if not ch:
            ch = await self._resolve_channel_from_conv_key(conversation_key)

        if not ch and self.rt.session_state:
            dm_id = self.rt.session_state.get_dm_channel(session_id)
            if dm_id:
                try:
                    ch = self.rt.dc_client.get_channel(dm_id) or await self.rt.dc_client.fetch_channel(dm_id)
                except Exception:
                    pass

        if not ch:
            ch = await self._get_log_channel()

        if not ch:
            logger.warning("show_approval_ui: no channel found for session=%s", session_id)
            return

        pth = details.get("path", "?")
        rsn = details.get("reason", "")
        desc = f"\U0001f512 **需要审批**\n操作: `{operation}`\n路径: `{pth}`"
        if rsn:
            desc += f"\n原因: {rsn}"
        try:
            # [2026-05-14 refactor note] The ApprovalView class lives in
            # agent.py, so callbacks receive a factory through runtime to avoid
            # importing agent.py and creating a circular dependency.
            view = self.rt.approval_view_factory(approval_id) if self.rt.approval_view_factory else None
            msg = await ch.send(desc, view=view)
            # [AutoC 2026-05-31] 保存消息引用给 ApprovalView.on_timeout 编辑用
            if view and hasattr(view, '_msg'):
                view._msg = msg
        except Exception as e:
            logger.error("show_approval_ui send failed: %s", e)

    async def refresh_typing(
        self,
        trigger: TriggerInfo,
    ) -> None:
        pd = trigger.platform_data
        now = time.time()
        if now - pd.get("last_typing_time", 0) <= 8:
            return
        msg = pd.get("message")
        if msg:
            try:
                async with msg.channel.typing():
                    pass
                pd["last_typing_time"] = now
            except Exception:
                pass

    async def add_reactions(
        self,
        trigger: TriggerInfo,
        reactions: list[str],
    ) -> None:
        msg = trigger.platform_data.get("message")
        if msg:
            for r in reactions:
                try:
                    await msg.add_reaction(r.strip())
                except Exception:
                    pass

    async def on_task_created(
        self,
        trigger: TriggerInfo,
        task_id: str,
    ) -> None:
        cancel_view = trigger.platform_data.get("cancel_view")
        if cancel_view and not cancel_view.task_id:
            cancel_view.task_id = task_id

    async def on_restart_signal(
        self,
        conversation_key: str,
    ) -> None:
        if conversation_key.startswith("discord:"):
            try:
                ch_id = int(conversation_key[len("discord:"):])
                asyncio.create_task(_safe_restart(self.rt, channel_id=ch_id, delay=2.0))
            except ValueError:
                pass

    async def on_context_reset(
        self,
        conversation_key: str,
        reason: str,
        cleaned_triggers: list[TriggerInfo],
    ) -> None:
        if conversation_key.startswith("discord:"):
            try:
                ch_id = int(conversation_key[len("discord:"):])
                if reason != "compact":
                    self.rt.channel_history.pop(ch_id, None)
            except ValueError:
                pass

        for t in cleaned_triggers:
            pd = t.platform_data
            status_msg = pd.get("status_msg")
            if status_msg:
                try:
                    await status_msg.edit(content="🔄 上下文已重置。", view=None)
                except Exception:
                    pass
            stale_main = pd.get("_stale_main_state")
            if stale_main:
                await self._settle_log_msg(stale_main, "🔄 已重置")
            stale_main = pd.get("_stale_main_state")
            if stale_main:
                await self._settle_log_msg(stale_main, "🔄 已重置")

            seq = t.inbound_seq
            self._dot_states.pop(seq, None)
            self._last_edit_time.pop(seq, None)

        logger.info("on_context_reset conv=%s reason=%s cleaned=%d",
                    conversation_key, reason, len(cleaned_triggers))

    async def on_assistant_text(
        self,
        trigger: TriggerInfo,
        text: str,
    ) -> None:
        """hybrid 模式下工具调用伴随的 free prose，追加到 dot_state 进度显示。"""
        seq = trigger.inbound_seq
        if seq and self.rt.session_state and seq in self.rt.session_state.triggers:
            dot = self._get_or_create_dot_state(seq)
            lines = dot["prose_lines"]
            for line in text.strip().split("\n"):
                line = line.strip()
                if line:
                    lines.append(line)
            dot["prose_lines"] = lines[-4:]

    async def on_engine_restarted(
        self,
        payload: dict[str, Any],
    ) -> None:
        """Engine restarted — close stale progress messages and local task state."""
        logger.info("on_engine_restarted: generation=%s prev=%s orphans=%s",
                    payload.get("generation_id", "")[:12],
                    payload.get("previous_generation_id", "")[:12],
                    payload.get("orphans_cancelled", 0))

        session_state = self.rt.session_state
        if not session_state:
            return

        # [2026-05-21 engine-restart cleanup] Why: an engine restart cancels old
        # tasks while the Discord adapter process keeps its in-memory trigger and
        # progress dictionaries. How: snapshot those dictionaries before cleaning
        # their old entries, then close the Discord messages that still represent
        # the cancelled work. Purpose: avoid stale progress logs, stale cancel UI, and typing
        # refreshes after the backend generation has changed.
        triggers = list(session_state.triggers.items())
        main_states = list(session_state.main_task_states.items())
        child_states = list(session_state.child_task_states.items())

        for seq, trigger in triggers:
            status_msg = trigger.platform_data.get("status_msg")
            if status_msg:
                try:
                    if trigger.is_dm:
                        # [2026-05-21 engine-restart cleanup] Why: in DM flows the
                        # status message is the progress surface itself. How: delete
                        # it instead of editing it to a terminal state. Purpose:
                        # match normal DM completion behavior and leave no dead
                        # progress box.
                        await status_msg.delete()
                    else:
                        # [2026-05-21 engine-restart cleanup] Why: in guild flows
                        # the status message mostly holds the cancel UI. How: edit it
                        # to a terminal backend-restart notice and remove the view.
                        # Purpose: avoid leaving a visible but unusable cancel button
                        # after the old engine task has been cancelled.
                        await status_msg.edit(content="⚠️ 后端已重启", view=None)
                except Exception:
                    pass
            self._dot_states.pop(seq, None)
            self._last_edit_time.pop(seq, None)

        for seq, state in main_states:
            # [2026-05-21 engine-restart cleanup] Why: group main tasks keep their
            # progress in the log channel. How: reuse the existing terminal-log
            # formatter with a backend-restart status. Purpose: visibly close the
            # log instead of deleting useful diagnostic context.
            await self._settle_log_msg(state, "⚠️ 后端已重启")
            self._dot_states.pop(seq, None)
            self._last_edit_time.pop(seq, None)

        for task_key, child_state in child_states:
            if getattr(child_state, "is_dm", False):
                msg = child_state.platform_data.get("msg")
                if msg:
                    try:
                        # [2026-05-21 engine-restart cleanup] Why: child-node DM
                        # progress messages are transient user-facing indicators.
                        # How: delete them like finalize_child_progress does for DM
                        # children. Purpose: avoid leaving cancelled subtask logs in
                        # private chats after engine restart.
                        await msg.delete()
                    except Exception:
                        pass
            else:
                # [2026-05-21 engine-restart cleanup] Why: guild/log-channel child
                # logs are useful audit records. How: edit them to the same terminal
                # backend-restart status. Purpose: close the display while retaining
                # what had already been logged.
                await self._settle_log_msg(child_state, "⚠️ 后端已重启")
            self._child_dot_states.pop(task_key, None)
            self._last_edit_time.pop(task_key, None)

        # [2026-05-21 engine-restart cleanup] Why: EventRouter refreshes typing and
        # progress from these dictionaries, but a new Discord inbound could be
        # registered while message edits/deletes above are awaiting Discord I/O.
        # How: remove only keys captured in the restart snapshot instead of calling
        # dict.clear(). Purpose: stop old typing refreshes while preserving any new
        # work and keeping conversation_sessions/session_conv_map bound to the
        # existing Clonoth session.
        for seq, _trigger in triggers:
            session_state.triggers.pop(seq, None)
            session_state.pending_watermarks.pop(seq, None)
        for seq, _state in main_states:
            session_state.main_task_states.pop(seq, None)
        for task_key, _child_state in child_states:
            session_state.child_task_states.pop(task_key, None)

        logger.info("on_engine_restarted cleanup: triggers=%d main=%d child=%d",
                    len(triggers), len(main_states), len(child_states))

    async def raw_event_hook(self, event: Event) -> str | None:
        """Layer 1 钩子：更新 dot_state，并让 SDK 继续默认处理。"""
        # ── llm_retry: 更新 retry 状态，清空旧 preview ──
        if event.type == "llm_retry":
            p = event.payload
            src_seq = int(p.get("source_inbound_seq") or 0)
            node_id = p.get("node_id", "")
            retry_info = f"⚠️ Retry {p.get('attempt', '?')}/{p.get('max_retries', '?')} — {p.get('error', 'unknown')[:60]}"
            if src_seq and self.rt.session_state and src_seq in self.rt.session_state.triggers:
                dot = self._get_or_create_dot_state(src_seq)
                dot["retry_info"] = retry_info
                dot["thinking_preview"] = ""
                dot["text_preview"] = ""
                dot["thinking_start_time"] = time.time()
            if node_id and node_id != self.rt.entry_node_id:
                task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
                if self.rt.session_state and self.rt.session_state.get_child_state(task_key):
                    cd = self._get_or_create_child_dot_state(task_key)
                    cd["retry_info"] = retry_info
                    cd["thinking_preview"] = ""
                    cd["text_preview"] = ""
                    cd["thinking_start_time"] = time.time()
            return None

        # ── tool_call_start: 记录工具开始执行 ──
        if event.type == "tool_call_start":
            p = event.payload
            src_seq = int(p.get("source_inbound_seq") or 0)
            node_id = p.get("node_id", "")
            tool_name = p.get("tool_name", "")
            arguments = p.get("arguments") if isinstance(p.get("arguments"), dict) else None
            summary = _tool_inline_summary(tool_name, arguments or {})
            entry = {"text": summary, "tool_call_id": p.get("tool_call_id", ""), "done": False, "ok": False}
            if src_seq and self.rt.session_state and src_seq in self.rt.session_state.triggers:
                dot = self._get_or_create_dot_state(src_seq)
                dot["tool_lines"].append(entry)
                dot["tool_lines"] = dot["tool_lines"][-6:]
            if node_id and node_id != self.rt.entry_node_id:
                task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
                if self.rt.session_state and self.rt.session_state.get_child_state(task_key):
                    cd = self._get_or_create_child_dot_state(task_key)
                    cd["tool_lines"].append(entry)
                    cd["tool_lines"] = cd["tool_lines"][-6:]
            return None

        # ── tool_call_end: 标记工具执行完成 ──
        if event.type == "tool_call_end":
            p = event.payload
            src_seq = int(p.get("source_inbound_seq") or 0)
            node_id = p.get("node_id", "")
            tc_id = p.get("tool_call_id", "")
            status = p.get("status", "")
            ok = status in ("success", "async_started")
            end_summary = (p.get("summary") or "").strip()
            def _mark_done(lines: list) -> None:
                for tl in reversed(lines):
                    if tl.get("tool_call_id") == tc_id:
                        tl["done"] = True
                        tl["ok"] = ok
                        if end_summary:
                            tl["text"] = _clip(end_summary, 80)
                        break
            if src_seq and self.rt.session_state and src_seq in self.rt.session_state.triggers:
                dot = self._get_or_create_dot_state(src_seq)
                _mark_done(dot["tool_lines"])
            if node_id and node_id != self.rt.entry_node_id:
                task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
                if self.rt.session_state and self.rt.session_state.get_child_state(task_key):
                    cd = self._get_or_create_child_dot_state(task_key)
                    _mark_done(cd["tool_lines"])
            return None

        if event.type != "stream_delta":
            return None

        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        content = p.get("content", "")
        sd_type = p.get("type", "text")
        node_id = p.get("node_id", "")
        req_id = p.get("llm_request_id", "")

        def _reset_if_new_llm(dot: dict) -> None:
            """llm_request_id 变化时清空上一轮的工具/prose摘要。"""
            if req_id and req_id != dot.get("_llm_req_id", ""):
                dot["_llm_req_id"] = req_id
                dot["tool_lines"] = []
                dot["prose_lines"] = []
                dot["thinking_preview"] = ""
                dot["text_preview"] = ""

        if src_seq and self.rt.session_state and src_seq in self.rt.session_state.triggers:
            dot = self._get_or_create_dot_state(src_seq)
            _reset_if_new_llm(dot)
            dot["had_stream_activity"] = True
            if not dot.get("thinking_start_time"):
                dot["thinking_start_time"] = time.time()
            if content:
                if sd_type == "thinking":
                    dot["thinking_preview"] = (dot.get("thinking_preview", "") + content)[-300:]
                elif sd_type == "text":
                    if dot.get("thinking_preview"):
                        dot["thinking_preview"] = ""
                        dot["thinking_start_time"] = time.time()
                    dot["text_preview"] = (dot.get("text_preview", "") + content)[-300:]

        if node_id and node_id != self.rt.entry_node_id:
            task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
            if self.rt.session_state and self.rt.session_state.get_child_state(task_key):
                cd = self._get_or_create_child_dot_state(task_key)
                _reset_if_new_llm(cd)
                cd["had_stream_activity"] = True
                if not cd.get("thinking_start_time"):
                    cd["thinking_start_time"] = time.time()
                if content:
                    if sd_type == "thinking":
                        cd["thinking_preview"] = (cd.get("thinking_preview", "") + content)[-300:]
                    elif sd_type == "text":
                        if cd.get("thinking_preview"):
                            cd["thinking_preview"] = ""
                            cd["thinking_start_time"] = time.time()
                        cd["text_preview"] = (cd.get("text_preview", "") + content)[-300:]

        return None
