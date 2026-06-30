"""Session 状态管理 — 集中管理会话运行时状态。

Phase 2 (2026-04-17): 初始创建，将 bot_adapter.py 中散落的全局 dict 收纳为结构化类。

替代的全局变量（来自 bot_adapter.py）：
  _trigger_messages        → SessionState.triggers (dict[int, TriggerInfo])
  _session_dm_channels     → SessionState.session_dm_channels
  _main_task_state         → SessionState.main_task_states (dict[int, MainTaskState])
  _child_task_logs         → SessionState.child_task_states (dict[str, ChildTaskState])
  _conversation_sessions   → SessionState.conversation_sessions
  session_conv_map         → SessionState.session_conv_map
  _pending_watermarks      → SessionState.pending_watermarks
  _last_ctx_seq            → SessionState.last_ctx_seq

不纳入的状态：
  _handled_approval_ids — 已在 approval.py 的 ApprovalTracker 中管理
  _channel_history / _history_seq_counter — 平台相关（频道历史队列），留在 Bot 适配器

设计约束：
  - 不依赖 discord.py 或任何平台库
  - asyncio 单线程模型，不加线程锁
  - platform_data: dict[str, Any] 携带平台特定数据（如 discord.Message）
  - register_trigger / consume_trigger 的时序保证与原代码一致
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ================================================================
#  数据类
# ================================================================


# 2026-04-17: 删除 DotState 类 — 点阵动画（dot_step, advance）、
# 思维链预览（thinking_preview, text_preview）、流式活动标志（had_stream_activity）
# 均属 Bot 展示层逻辑，不属于协议 SDK，已迁至 Bot 适配器层。


@dataclass
class TriggerInfo:
    """触发消息信息 — 跟踪 inbound 请求与平台消息的映射。

    替代 bot_adapter.py _trigger_messages[seq] 中的内联 dict（L131-132）：
      {"message": discord.Message, "channel_id": int, "conversation_key": str,
       "is_dm": bool, "status_msg": discord.Message|None, "session_id": str,
       "created_at": float, "cancel_view": CancelView, "last_typing_time": float}

    SDK 管协议字段（inbound_seq, conversation_key, session_id, is_dm, created_at,
    task_id）。平台数据（discord.Message, status_msg, cancel_view 等）通过
    platform_data 字典携带，不引入 discord.py 依赖。

    platform_data 典型用法（Discord 适配器）：
      message — discord.Message（触发消息，用于 reply）
      status_msg — discord.Message | None（⏳处理中 的状态消息）
      cancel_view — CancelView（取消按钮 UI 组件）
      last_typing_time — float（上次触发 typing 的时间）
      channel_id — int（冗余存储供平台快速定位频道）
    """
    inbound_seq: int
    conversation_key: str
    session_id: str
    is_dm: bool
    created_at: float = field(default_factory=time.time)
    task_id: str | None = None
    platform_data: dict[str, Any] = field(default_factory=dict)

    def refresh(self) -> None:
        """刷新 created_at，防止被超时清理。

        对应 bot_adapter.py 中多处 trigger["created_at"] = time.time()。
        """
        self.created_at = time.time()

    def is_stale(self, timeout: float = 600.0) -> bool:
        """判断 trigger 是否已超时（默认 600 秒 = 10 分钟）。

        对应 bot_adapter.py L1252：
          _now - info["created_at"] > 600
        """
        return time.time() - self.created_at > timeout


@dataclass
class MainTaskState:
    """主节点（入口节点）任务运行状态。

    替代 bot_adapter.py _main_task_state[seq] 中的内联 dict：
      {"progress_records": list, "log_msg": Message|None,
       "stream_parts": [], "handled_approvals": set}

    handled_approvals 是 per-trigger 级别的审批去重（区别于 ApprovalTracker 的
    全局级去重），防止同一 trigger 关联的审批被重复记录到进度日志。

    platform_data 典型用法（Discord 适配器）：
      log_msg — discord.Message | None（Agent 日志频道的进度消息）
    """
    progress_records: list[str] = field(default_factory=list)
    stream_parts: list[str] = field(default_factory=list)
    # 2026-04-17: 移除 dot_state, last_edit_time 字段及对应方法
    # （点阵动画、edit 节流属展示层逻辑，已迁至 Bot 适配器层）
    handled_approvals: set[str] = field(default_factory=set)
    platform_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChildTaskState:
    """子节点任务运行状态。

    替代 bot_adapter.py _child_task_logs[task_key] 中的内联 dict：
      {"msg": Message, "lines": list, "prefix": str}

    platform_data 典型用法（Discord 适配器）：
      msg — discord.Message（子节点进度日志消息）
    """
    lines: list[str] = field(default_factory=list)
    prefix: str = ""
    # 2026-04-17: 移除 dot_state, last_edit_time 字段及对应方法（同 MainTaskState）
    platform_data: dict[str, Any] = field(default_factory=dict)


# ================================================================
#  SessionState
# ================================================================


class SessionState:
    """集中管理所有会话运行时状态。

    替代 bot_adapter.py 中散落在模块级的多个全局 dict，
    提供结构化方法操作 trigger 注册/消费、session 映射、watermark 推进等。

    asyncio 单线程模型下运行，不加线程锁。
    register_trigger 和 consume_trigger 的时序保证与原代码一致：
      - register_trigger 在 submit_inbound 成功后、任何后续 await 之前调用
      - consume_trigger 在 outbound_message 事件处理时调用
      - 两者之间不会有并发竞争（同一个 event loop tick 内 register 完成）

    用法::

        state = SessionState()

        # 注册触发
        trigger = TriggerInfo(
            inbound_seq=42, conversation_key="discord:123",
            session_id="sid-abc", is_dm=False,
            platform_data={"message": discord_msg, "channel_id": 123},
        )
        state.register_trigger(trigger)

        # 消费触发
        t = state.consume_trigger(42)

        # Legacy helper remains available for adapters, but EventRouter does not use it.
        result = state.find_trigger_by_session("sid-abc")
    """

    def __init__(self) -> None:
        # ---- conversation_key ↔ session_id 双向映射 ----
        # 对应 bot_adapter.py:
        #   _conversation_sessions: dict[str, str]  (conv_key → session_id, L136)
        #   session_conv_map: dict[str, str]        (session_id → conv_key, L140)
        self.conversation_sessions: dict[str, str] = {}
        self.session_conv_map: dict[str, str] = {}

        # ---- trigger 管理 ----
        # 对应 _trigger_messages: dict[int, dict]  (inbound_seq → trigger info, L130)
        self.triggers: dict[int, TriggerInfo] = {}

        # ---- 任务状态 ----
        # 对应 _main_task_state: dict[int, dict]   (inbound_seq → state, L134)
        self.main_task_states: dict[int, MainTaskState] = {}
        # 对应 _child_task_logs: dict[str, dict]   (task_key → state, L135)
        self.child_task_states: dict[str, ChildTaskState] = {}

        # ---- watermark 管理 ----
        # 对应 _pending_watermarks: dict[int, tuple[int, int]]  (inbound_seq → (channel_id, wm_seq), L251)
        self.pending_watermarks: dict[int, tuple[int, int]] = {}
        # 对应 _last_ctx_seq: dict[int, int]  (channel_id → high watermark seq, L250)
        self.last_ctx_seq: dict[int, int] = {}

        # ---- DM 频道映射 ----
        # trigger 消费后子节点仍能找到 DM 频道
        # 对应 _session_dm_channels: dict[str, int]  (session_id → channel_id, L133)
        self.session_dm_channels: dict[str, int] = {}

    # ================================================================
    #  Session 映射
    # ================================================================

    def register_session(self, conversation_key: str, session_id: str) -> None:
        """注册 conversation_key ↔ session_id 双向映射。

        对应 bot_adapter.py 两处赋值：
          _conversation_sessions[conversation_key] = session_id  (L880)
          session_conv_map[session_id] = conv_key                (L1282)
        合并为单次调用，保证双向一致性。
        """
        self.conversation_sessions[conversation_key] = session_id
        self.session_conv_map[session_id] = conversation_key

    def get_session_id(self, conversation_key: str) -> str | None:
        """根据 conversation_key 查 session_id。"""
        return self.conversation_sessions.get(conversation_key)

    def get_conversation_key(self, session_id: str) -> str | None:
        """根据 session_id 查 conversation_key。"""
        return self.session_conv_map.get(session_id)

    def unregister_session(self, conversation_key: str) -> None:
        """删除 conversation_key 的双向映射。

        对应 bot_adapter.py context_reset(clear) L1976：
          _conversation_sessions.pop(_cr_conv, None)
        此处同时清理反向映射，避免残留。
        """
        sid = self.conversation_sessions.pop(conversation_key, None)
        if sid is not None:
            self.session_conv_map.pop(sid, None)

    # ================================================================
    #  Trigger 管理
    # ================================================================

    def register_trigger(self, trigger: TriggerInfo) -> None:
        """注册触发消息。在 submit_inbound 成功后、任何后续 await 之前调用。

        对应 bot_adapter.py L888-902：
          _trigger_messages[my_inbound_seq] = trigger_info
          if is_dm: _session_dm_channels[session_id] = channel_id

        时序保证：此方法在同步路径中调用（submit_inbound 返回后立即调用，
        中间没有 await），因此 outbound_poller 不会在注册完成前拿到事件。

        如果 trigger.is_dm 且 platform_data 中包含 channel_id，
        会自动注册 DM 频道映射。
        """
        self.triggers[trigger.inbound_seq] = trigger
        # DM 场景：自动注册 session → channel 映射，
        # 使 trigger 被消费后子节点仍能找到发送目标
        if trigger.is_dm:
            channel_id = trigger.platform_data.get("channel_id")
            if channel_id is not None:
                self.session_dm_channels[trigger.session_id] = channel_id

    def consume_trigger(self, inbound_seq: int) -> TriggerInfo | None:
        """消费（pop）指定 inbound_seq 的 trigger 并返回。不存在时返回 None。

        对应 bot_adapter.py L1302：
          trigger = _trigger_messages.pop(_out_src_seq)
        """
        return self.triggers.pop(inbound_seq, None)

    def get_trigger(self, inbound_seq: int) -> TriggerInfo | None:
        """获取但不消费 trigger。不存在时返回 None。"""
        return self.triggers.get(inbound_seq)

    def find_trigger_by_session(self, session_id: str) -> tuple[int, TriggerInfo] | None:
        """按 session_id 查找 trigger（仅保留给旧适配器直接调用）。

        [Fork/Merge 2026-05-12] EventRouter must not use this fallback anymore.
        Why: several concurrent branch tasks can share one parent session, so session-only
        lookup can attach an event to the wrong inbound. How: keep the helper for external
        compatibility but route SDK events through source_inbound_seq. Purpose: avoid breaking
        adapters that may call this method manually while making core routing exact.

        Returns:
            (inbound_seq, TriggerInfo) 元组，未找到则返回 None。
        """
        for seq, trigger in self.triggers.items():
            if trigger.session_id == session_id:
                return seq, trigger
        return None

    def resolve_trigger(
        self, source_inbound_seq: int, session_id: str,
    ) -> tuple[int, TriggerInfo] | None:
        """只按 source_inbound_seq 精确查找 trigger。

        [Fork/Merge 2026-05-12] session_id is intentionally ignored by SDK event routing.
        Why: concurrent forked inbounds can share the same parent session, so a session fallback
        can update or consume the wrong trigger. How: keep the parameter for API compatibility,
        but return a match only when source_inbound_seq exists in triggers. Purpose: make event
        routing deterministic while preserving old callers' function signature.

        Returns:
            (inbound_seq, TriggerInfo) 元组，未找到则返回 None。
        """
        # [Fork/Merge 2026-05-12] Keep session_id only for signature compatibility.
        # Why: using it as a fallback would be ambiguous under concurrent branch tasks.
        # How: bind it to '_' and perform no lookup with it. Purpose: exact trigger routing.
        _ = session_id
        if source_inbound_seq:
            trigger = self.triggers.get(source_inbound_seq)
            if trigger is not None:
                return source_inbound_seq, trigger
        return None

    def cleanup_stale_triggers(self, timeout: float = 600.0) -> list[TriggerInfo]:
        """清理超时的 trigger，返回被清理的列表。

        对应 bot_adapter.py L1250-1266 的超时清理循环：
          _stale_triggers = [seq for seq, info in _trigger_messages.items()
                             if _now - info["created_at"] > 600]
          for seq in _stale_triggers: ...

        同时清理关联的 pending_watermarks 和 main_task_states。
        调用方负责处理平台侧清理（edit status_msg、delete log_msg 等），
        可遍历返回的列表和对应的 MainTaskState（通过 platform_data）操作。
        """
        now = time.time()
        stale_seqs = [
            seq for seq, trigger in self.triggers.items()
            if now - trigger.created_at > timeout
        ]
        stale_triggers: list[TriggerInfo] = []
        for seq in stale_seqs:
            trigger = self.triggers.pop(seq)
            self.pending_watermarks.pop(seq, None)
            # 把关联的 MainTaskState 附带在 trigger 的 platform_data 中，
            # 供调用方执行平台侧清理（如删除 log_msg）
            main_state = self.main_task_states.pop(seq, None)
            if main_state is not None:
                trigger.platform_data["_stale_main_state"] = main_state
            stale_triggers.append(trigger)
        return stale_triggers

    def update_trigger_platform(
        self, inbound_seq: int, **updates: Any,
    ) -> TriggerInfo | None:
        """更新 trigger 的 platform_data 字段。

        对应 bot_adapter.py L918-919：
          _trigger_messages[my_inbound_seq]["status_msg"] = status_msg
        以及 preempt V2 中更新 trigger 的 message/status_msg（L833-836）。

        Args:
            inbound_seq: trigger 的 inbound_seq
            **updates: 要更新/新增的 platform_data 键值对

        Returns:
            更新后的 TriggerInfo，不存在则返回 None。
        """
        trigger = self.triggers.get(inbound_seq)
        if trigger is not None:
            trigger.platform_data.update(updates)
        return trigger

    # ================================================================
    #  MainTaskState 管理
    # ================================================================

    def get_or_create_main_state(self, inbound_seq: int) -> MainTaskState:
        """获取或创建主任务状态。

        对应 bot_adapter.py 中的 setdefault 模式（如 L1542, L1598, L1769）：
          state = _main_task_state.setdefault(seq,
              {"progress_records": [], "log_msg": None, "stream_parts": [],
               "handled_approvals": set()})
        """
        if inbound_seq not in self.main_task_states:
            self.main_task_states[inbound_seq] = MainTaskState()
        return self.main_task_states[inbound_seq]

    def get_main_state(self, inbound_seq: int) -> MainTaskState | None:
        """获取主任务状态，不存在时返回 None。"""
        return self.main_task_states.get(inbound_seq)

    def remove_main_state(self, inbound_seq: int) -> MainTaskState | None:
        """移除并返回主任务状态。

        对应 bot_adapter.py L1303：
          state = _main_task_state.pop(_out_src_seq, None)
        """
        return self.main_task_states.pop(inbound_seq, None)

    # ================================================================
    #  ChildTaskState 管理
    # ================================================================

    def get_or_create_child_state(
        self, task_key: str, *, prefix: str = "",
    ) -> tuple[ChildTaskState, bool]:
        """获取或创建子任务状态。

        对应 bot_adapter.py L1672-1685 的子节点日志首次创建逻辑。

        Returns:
            (state, is_new)。is_new=True 表示新创建，
            适配器需要在平台侧创建显示消息（如 Discord send）。
        """
        is_new = task_key not in self.child_task_states
        if is_new:
            # 2026-04-17: 移除 dot_state/last_edit_time 参数（展示层逻辑已迁出 SDK）
            self.child_task_states[task_key] = ChildTaskState(
                prefix=prefix,
            )
        return self.child_task_states[task_key], is_new

    def get_child_state(self, task_key: str) -> ChildTaskState | None:
        """获取子任务状态，不存在时返回 None。"""
        return self.child_task_states.get(task_key)

    def remove_child_state(self, task_key: str) -> ChildTaskState | None:
        """移除并返回子任务状态。

        对应 bot_adapter.py L1831：
          del _child_task_logs[task_key]
        """
        return self.child_task_states.pop(task_key, None)

    def trim_child_states(self, max_count: int = 50) -> None:
        """当子任务状态过多时，删除最旧的一半。

        对应 bot_adapter.py L1688-1691 的大小限制清理：
          if len(_child_task_logs) > 50:
              _to_remove = list(_child_task_logs.keys())[:25]
              for _k in _to_remove: del _child_task_logs[_k]
        """
        if len(self.child_task_states) > max_count:
            keys_to_remove = list(self.child_task_states.keys())[: max_count // 2]
            for key in keys_to_remove:
                del self.child_task_states[key]

    def find_child_states_by_session(
        self, session_id: str,
    ) -> list[tuple[str, ChildTaskState]]:
        """查找属于指定 session 的所有子任务状态。

        通过 task_key 后缀 ":{session_id}" 匹配（与 bot_adapter.py 的
        task_key 生成规则一致：task_id 或 "{node_id}:{session_id}"）。

        对应 bot_adapter.py task_preempted 事件处理 L1876 中的子节点清理。
        """
        suffix = f":{session_id}"
        return [
            (key, state)
            for key, state in self.child_task_states.items()
            if key.endswith(suffix)
        ]

    # ================================================================
    #  Watermark 管理
    # ================================================================

    def register_watermark(
        self, inbound_seq: int, channel_id: int, watermark_seq: int,
    ) -> None:
        """注册待确认的水位标记。

        对应 bot_adapter.py L882-883：
          _pending_watermarks[my_inbound_seq] = (channel_id, _new_wm)

        水位在 inbound_accepted 事件到达后才正式推进，防止 engine 未接受
        消息时错误地认为历史已发送。
        """
        self.pending_watermarks[inbound_seq] = (channel_id, watermark_seq)

    def accept_watermark(self, inbound_seq: int) -> tuple[int, int] | None:
        """确认水位标记并推进 last_ctx_seq 高水位。

        对应 bot_adapter.py L1984-1989 inbound_accepted 事件处理：
          _ia_ch_id, _ia_wm = _pending_watermarks.pop(_ia_seq)
          _last_ctx_seq[_ia_ch_id] = max(_last_ctx_seq.get(..., -1), _ia_wm)

        Returns:
            成功推进时返回 (channel_id, new_watermark)，无待确认水位返回 None。
        """
        wm = self.pending_watermarks.pop(inbound_seq, None)
        if wm is None:
            return None
        channel_id, watermark_seq = wm
        self.last_ctx_seq[channel_id] = max(
            self.last_ctx_seq.get(channel_id, -1), watermark_seq,
        )
        return channel_id, self.last_ctx_seq[channel_id]

    def reset_channel_watermark(self, channel_id: int) -> None:
        """重置频道的高水位（context_reset 时调用）。

        对应 bot_adapter.py L1956：
          _last_ctx_seq.pop(_cr_ch_id, None)
        重置后下一轮 inbound 会带完整频道历史。
        """
        self.last_ctx_seq.pop(channel_id, None)

    def get_high_watermark(self, channel_id: int) -> int:
        """获取频道的当前高水位序号。默认 -1（表示无历史发送记录）。"""
        return self.last_ctx_seq.get(channel_id, -1)

    # ================================================================
    #  DM Channel 管理
    # ================================================================

    def register_dm_channel(self, session_id: str, channel_id: int) -> None:
        """注册 DM 频道映射。

        对应 bot_adapter.py L901-902：
          if is_dm: _session_dm_channels[session_id] = channel_id

        通常由 register_trigger 内部自动调用。
        也可在 preempt V2 重新注册 trigger 时显式调用。
        """
        self.session_dm_channels[session_id] = channel_id

    def get_dm_channel(self, session_id: str) -> int | None:
        """查找 session 对应的 DM 频道 ID。

        trigger 被消费后，子节点仍可通过此方法找到 DM 发送目标。
        对应 bot_adapter.py L1654-1661。
        """
        return self.session_dm_channels.get(session_id)

    def cleanup_dm_channels_for(self, channel_id: int) -> None:
        """清理指定频道的所有 DM 映射。

        对应 bot_adapter.py L1978-1980 context_reset(clear)：
          _cr_stale_sdc = [sid for sid, ch in _session_dm_channels.items() if ch == _cr_ch_id]
          for sid in _cr_stale_sdc: _session_dm_channels.pop(sid, None)
        """
        stale_sids = [
            sid for sid, ch in self.session_dm_channels.items()
            if ch == channel_id
        ]
        for sid in stale_sids:
            del self.session_dm_channels[sid]

    # ================================================================
    #  复合清理操作
    # ================================================================

    def cleanup_for_context_reset(
        self,
        conversation_key: str,
        channel_id: int | None = None,
    ) -> list[TriggerInfo]:
        """context_reset(clear) 时清理指定会话的所有关联状态。

        对应 bot_adapter.py L1964-1982 context_reset 非 compact 分支的完整清理。

        操作内容：
          1. 清理 conversation_key 匹配的所有 trigger 及关联状态
          2. 删除 conversation_key ↔ session_id 双向映射
          3. 如提供 channel_id，重置水位并清理 DM 频道映射

        Args:
            conversation_key: 被重置的会话键（如 "discord:123456"）
            channel_id: 平台频道标识符（可选；提供时额外清理水位和 DM 映射）

        Returns:
            被清理的 trigger 列表，供适配器处理平台侧清理
            （edit status_msg, delete log_msg 等）。
            每个 trigger 的 platform_data["_stale_main_state"] 中附带关联的
            MainTaskState（如有）。
        """
        # 1. 清理匹配的 triggers
        stale_seqs = [
            seq for seq, trigger in self.triggers.items()
            if trigger.conversation_key == conversation_key
        ]
        cleaned_triggers: list[TriggerInfo] = []
        for seq in stale_seqs:
            trigger = self.triggers.pop(seq)
            main_state = self.main_task_states.pop(seq, None)
            self.pending_watermarks.pop(seq, None)
            if main_state is not None:
                trigger.platform_data["_stale_main_state"] = main_state
            cleaned_triggers.append(trigger)

        # 2. 清理 session 映射
        self.unregister_session(conversation_key)

        # 3. 清理水位和 DM 映射（需要 channel_id）
        if channel_id is not None:
            self.reset_channel_watermark(channel_id)
            self.cleanup_dm_channels_for(channel_id)

        return cleaned_triggers

    def cleanup_for_task_preempted(
        self,
        source_inbound_seq: int,
        session_id: str,
        task_id: str = "",
    ) -> tuple[TriggerInfo | None, list[tuple[str, ChildTaskState]]]:
        """task_preempted 事件时按精确键清理被打断任务的关联状态。

        [Fork/Merge 2026-05-12] This cleanup no longer falls back to session_id.
        Why: several active triggers can share one parent session, so session fallback can clear
        an unrelated inbound. How: consume a trigger only by source_inbound_seq and remove child
        progress only by exact task_id when available. Purpose: keep preempt cleanup scoped to
        the task that actually emitted the event.

        Returns:
            (trigger_or_none, child_states_list)。
            trigger 可能为 None（无匹配 trigger 时）。
            child_states_list 是 (task_key, ChildTaskState) 的列表。
        """
        # [Fork/Merge 2026-05-12] Keep session_id only for old call sites.
        # Why: task_preempted cleanup must not use session fallback. How: ignore this value and
        # rely on source_inbound_seq/task_id. Purpose: prevent sibling branch state from being removed.
        _ = session_id
        trigger: TriggerInfo | None = None
        trigger_seq: int = 0

        if source_inbound_seq and source_inbound_seq in self.triggers:
            trigger = self.triggers.get(source_inbound_seq)
            trigger_seq = source_inbound_seq

        # 消费 trigger 及关联状态。
        # [Fork/Merge 2026-05-12] The source sequence is the only trigger key accepted here.
        # Why: using session_id would be ambiguous under concurrent entry branches. How: pop only
        # the exact trigger found above. Purpose: avoid clearing another inbound's status message.
        if trigger is not None:
            self.triggers.pop(trigger_seq, None)
            self.main_task_states.pop(trigger_seq, None)

        child_states: list[tuple[str, ChildTaskState]] = []
        exact_task_key = str(task_id or "").strip()
        if exact_task_key:
            child_state = self.child_task_states.pop(exact_task_key, None)
            if child_state is not None:
                child_states.append((exact_task_key, child_state))

        return trigger, child_states
