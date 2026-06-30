"""AdapterCallbacks Protocol — SDK 处理完协议逻辑后通知适配器执行平台操作的回调接口。

Phase 3 step 1 (2026-04-17): 初始创建。

从 bot_adapter.py _outbound_poller() (L1206-2040) 中提取所有
「SDK 完成协议处理后需要适配器执行平台操作」的点，为每个点定义一个回调方法。

设计原则：
  - 使用 typing.Protocol，适配器通过鸭子类型实现，无需继承
  - 不依赖 discord.py 或任何平台库
  - 所有方法均为 async
  - 方法签名使用 clonoth_sdk/state.py 和 clonoth_sdk/types.py 中的类型
  - SDK 协议标记（[CLONOTH_TOOL_TRACE]）由 SDK 清理；
    Bot 自定义标记（[SPLIT] / [REACT:...] / [BOT_RESTART]）由适配器自行处理

参考：
  - data/sdk_refactor_plan_final.md 第四节「双层钩子架构」
  - bot_adapter.py _outbound_poller() 中所有平台操作点
"""
from __future__ import annotations

from typing import Any, Protocol

from .state import ChildTaskState, MainTaskState, TriggerInfo


class AdapterCallbacks(Protocol):
    """适配器回调接口 — EventRouter 的下游通知目标。

    EventRouter 在处理每个事件的协议逻辑（trigger 匹配、状态更新、
    去重、节流）后，通过此接口通知适配器执行平台特定操作。

    适配器（如 bot_adapter.py 的 Discord Bot）需要实现全部方法。
    SDK 保证只在协议处理完成后调用回调，适配器无需关心事件解析、
    trigger 匹配、状态管理等协议细节。

    异常处理约定：
      所有回调中的异常由适配器自行 try/except 并记录日志，
      不应向 SDK 抛出，以免阻断事件处理循环。
    """

    # ================================================================
    #  消息发送
    #  对应 bot_adapter.py outbound_message / intermediate_reply 事件
    # ================================================================

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        main_state: MainTaskState | None = None,
    ) -> None:
        """主节点最终回复到达，发送到平台。

        触发时机：outbound_message 事件，source_inbound_seq 命中 trigger。
        对应 bot_adapter.py L1297-1373 的主节点回复处理分支。

        SDK 已完成：
          - 通过 source_inbound_seq 精确匹配并消费 trigger（从 triggers 中移除）
          - 移除关联的 MainTaskState（通过 main_state 参数传出）
          - 清理内部协议标记（[CLONOTH_TOOL_TRACE v...]...）

        适配器需要：
          - 删除 trigger 的 status_msg（platform_data["status_msg"]，「⏳处理中」提示）
          - 删除 main_state 中的 log_msg（platform_data["log_msg"]，进度日志消息）
          - 解析并处理 Bot 自定义标记（[SPLIT] 分段、[REACT:...] 反应、[BOT_RESTART] 信号）
          - 将文本和附件发送到平台
          - 记录机器人回复到频道历史缓存
          - 群聊场景下，若有 progress_records，可发送最终日志 embed

        Args:
            trigger: 被消费的触发信息。platform_data 中包含平台消息引用
                     （如 Discord 适配器的 message、status_msg、channel_id）。
            text: 清理过 SDK 协议标记后的回复文本（可能为空字符串）。
                  仍包含 Bot 自定义标记，适配器自行解析处理。
            attachments: 附件列表，每个元素为 dict（含 path / filename 等字段），
                         格式由 Supervisor 定义。
            main_state: 被移除的主任务状态。包含 progress_records（进度记录列表）、
                        platform_data（log_msg 等平台引用）。
                        为 None 表示该 trigger 没有关联的任务状态记录。
        """
        ...

    async def send_intermediate_reply(
        self,
        trigger: TriggerInfo,
        text: str,
    ) -> None:
        """主节点中间回复到达，发送到平台。

        触发时机：intermediate_reply 事件，source_inbound_seq 命中 trigger，
                  且 node_id 为入口节点。
        对应 bot_adapter.py L1435-1465。

        SDK 已完成：
          - 匹配 trigger（不消费，主节点仍在运行）
          - 刷新 trigger.created_at 防止超时清理
          - 清空 MainTaskState.stream_parts buffer
          - 清理内部协议标记

        适配器需要：
          - 解析并处理 Bot 自定义标记
          - 发送中间回复到平台（通常 reply 到触发消息）
          - 记录机器人回复到频道历史缓存

        Args:
            trigger: 关联的触发信息（仍在 triggers 中，未被消费）。
            text: 清理过 SDK 协议标记后的中间回复文本。
        """
        ...

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        node_id: str = "",
    ) -> None:
        """消息需要发送到频道，但没有匹配的 trigger（fallback 路径）。

        触发时机：
          - outbound_message 事件，source_inbound_seq 不在 triggers 中
            （主节点 finish 后子节点或调度任务的输出）
          - intermediate_reply 事件，非入口节点的中间回复
        对应 bot_adapter.py L1375-1434（outbound fallback）和 L1467-1520（intermediate fallback）。

        SDK 已完成：
          - 确认不是 system.* 内部节点（已过滤跳过）
          - 通过 session_conv_map 解析出 conversation_key
          - 清理内部协议标记

        适配器需要：
          - 从 conversation_key 解析平台频道标识（如 "discord:123" → channel 123）
          - 非入口节点的消息可加节点显示名前缀以区分来源
          - 发送文本和附件到对应频道

        Args:
            conversation_key: 目标会话键（如 "discord:123456789"）。
            text: 清理过 SDK 协议标记后的文本。仍含 Bot 自定义标记。
            attachments: 附件列表。
            node_id: 发送方节点 ID。空字符串表示未知。
                     适配器可据此为非入口节点消息添加来源标识前缀。
        """
        ...

    # ================================================================
    #  状态消息管理
    #  对应 trigger 生命周期中的 status_msg 编辑/删除
    # ================================================================

    async def delete_status_message(
        self,
        trigger: TriggerInfo,
    ) -> None:
        """删除 trigger 关联的「处理中」状态消息。

        触发时机：
          - trigger 超时清理后（SessionState.cleanup_stale_triggers）
        对应 bot_adapter.py L1256-1260 的超时 status_msg 编辑，
        以及 L1261-1266 的超时 log_msg 删除。

        SDK 已完成：
          - trigger 已从 triggers 中移除
          - 关联的 MainTaskState 已移除
          - MainTaskState 附带在 trigger.platform_data["_stale_main_state"] 中

        适配器需要：
          - 编辑 status_msg 为超时提示（如 "⚠️ Agent 请求超时。"）
          - 删除关联的 log_msg（通过 _stale_main_state.platform_data）

        Args:
            trigger: 被清理的触发信息。platform_data 中包含 status_msg 引用
                     和 _stale_main_state（如有）。
        """
        ...

    async def edit_status_message(
        self,
        trigger: TriggerInfo,
        content: str,
    ) -> None:
        """编辑 trigger 关联的状态消息为指定内容。

        触发时机（content 按场景不同）：
          - 任务取消（task_cancelled / cancel_requested）："⚠️ 任务已取消。"
            对应 bot_adapter.py L1796 和 L1844。
          - 任务打断（task_preempted）："⚡ 已被新消息打断。"
            对应 bot_adapter.py L1871。
          - 上下文重置（context_reset, clear）："🔄 上下文已重置。"
            对应 bot_adapter.py L1972。

        SDK 已完成：
          - 相关的状态清理（trigger 消费/移除、MainTaskState 清理等）

        适配器需要：
          - 从 trigger.platform_data 获取 status_msg 引用
          - 调用平台 API 编辑消息内容
          - 移除交互组件（如 Discord view=None）

        Args:
            trigger: 关联的触发信息。
            content: 要设置的新消息内容文本。
        """
        ...

    # ================================================================
    #  进度更新
    #  对应 node_started/completed, handoff_progress, compact_*, sweep
    # ================================================================

    async def update_progress(
        self,
        trigger: TriggerInfo,
        state: MainTaskState,
    ) -> None:
        """主节点进度记录更新后，通知适配器刷新显示。

        触发时机：
          - node_started / node_completed 事件（入口节点）
            对应 bot_adapter.py L1539-1571。
          - handoff_progress 事件（入口节点）
            对应 bot_adapter.py L1595-1632。
          - compact_start / compact_done / compact_failed 事件
            对应 bot_adapter.py L1889-1948。
        SDK 已完成：
          - 向 state.progress_records 追加新记录
          - 刷新 trigger.created_at 防止超时

        适配器需要：
          - 将 progress_records 格式化为进度日志文本
          - DM 场景 → edit trigger 的 status_msg
          - 群聊场景 → edit 日志频道的 log_msg（首次时 send 创建）
          - 将 log_msg 引用存入 state.platform_data["log_msg"]
          - SDK 不做节流，适配器自行决定是否 edit（如 2 秒间隔）
          - **重要：SDK 的 sweep 阶段每轮 poll 都会调用此回调，适配器必须自行实现节流（如 2 秒间隔），否则会导致高频率平台 API 调用。**

        Args:
            trigger: 关联的触发信息。
            state: 当前主任务状态（progress_records 已是最新）。
        """
        ...

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: TriggerInfo | None = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """子节点首次进度消息到达，通知适配器创建显示消息。

        触发时机：handoff_progress 事件，task_key 为新创建
                  （SessionState.get_or_create_child_state 返回 is_new=True）。
        对应 bot_adapter.py L1672-1691 子节点日志首次创建。

        SDK 已完成：
          - 创建 ChildTaskState（含 prefix, lines=[首条消息]）
          - 超量清理（trim_child_states）

        适配器需要：
          - 确定展示频道：
              * DM 场景 → trigger 所在频道
              * 群聊场景 → 日志频道（如 AGENT_LOG_CHANNEL_ID）
              * trigger 已消费 → 通过 session_dm_channels 或 conversation_key fallback
          - 格式化并发送初始进度消息
          - 将平台消息引用存入 state.platform_data（后续 update/finalize 使用）

        Args:
            task_key: 子任务标识（task_id 或 "{node_id}:{session_id}"）。
            state: 新创建的子任务状态。
            trigger: 关联的触发信息。
                     可能为 None（trigger 已被消费，如主节点 finish 后的子任务）。
            conversation_key: 所属会话键，供频道定位 fallback。
            session_id: 所属 session ID，供 DM 频道 fallback 查找。
        """
        ...

    async def update_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
    ) -> None:
        """子节点后续进度更新，通知适配器刷新已有显示消息。

        触发时机：
          - handoff_progress 事件追加新行后（非首次）
            对应 bot_adapter.py L1692-1707。
          - node_started / node_completed 事件（非入口节点）
            对应 bot_adapter.py L1573-1587。
          - intermediate_reply 事件在子节点日志中追加「↳ 已发送中间回复」后
            对应 bot_adapter.py L1492-1500。

        SDK 已完成：
          - 向 state.lines 追加新记录

        适配器需要：
          - 格式化 state.prefix + state.lines 为显示文本
          - 从 state.platform_data 获取消息引用并 edit
          - SDK 不做节流，适配器自行决定是否 edit（如 2 秒间隔）
          - **重要：SDK 的 sweep 阶段每轮 poll 都会调用此回调，适配器必须自行实现节流（如 2 秒间隔），否则会导致高频率平台 API 调用。**

        Args:
            task_key: 子任务标识。
            state: 当前子任务状态（lines 已是最新）。
        """
        ...

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        """子节点任务结束，通知适配器做最终更新或清理。

        触发时机：
          - task_completed 事件（非入口节点）：status = "✓ 任务完成"
            对应 bot_adapter.py L1810-1831。
          - task_cancelled 事件（非入口节点）：status = "✗ 任务已取消"
            对应同上。
          - task_preempted 事件清理子节点：status = "⚡ 被打断"
            对应 bot_adapter.py L1875-1884。

        SDK 已完成：
          - 从 child_task_states 中移除此 task_key
          - 被移除的 state 通过参数传出

        适配器需要：
          - DM 场景（is_dm=True）→ 删除子节点进度消息
          - 群聊场景（is_dm=False）→ edit 消息为最终状态（附加 status 标记）
          - 释放 state.platform_data 中的资源引用

        Args:
            task_key: 子任务标识。
            state: 被移除的子任务状态（已从 child_task_states 中弹出）。
                   platform_data 中仍持有平台消息引用。
            status: 终态文本（如 "✓ 任务完成" / "✗ 任务已取消" / "⚡ 被打断"）。
            is_dm: 是否 DM 会话。True 时适配器应删除消息而非 edit。
        """
        ...

    # ================================================================
    #  审批 UI
    #  对应 approval_requested 事件中外部操作的审批按钮展示
    # ================================================================

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """外部操作需要人工审批，通知适配器展示审批 UI。

        触发时机：approval_requested 事件，经 is_external_operation 判定为
                  外部操作（操作目标在工作区外部）。
        对应 bot_adapter.py L993-1006 _process_approval_event 中的外部操作分支。

        SDK 已完成：
          - 全局级去重（ApprovalTracker.mark_handled）
          - per-trigger 级去重（MainTaskState.handled_approvals）
          - 刷新 trigger.created_at
          - 向 progress_records 追加审批等待记录

        适配器需要：
          - 确定目标频道（查找链：活跃 trigger → session_conv_map → DM → 日志频道）
          - 从 details 提取操作信息并格式化描述文本
          - 构造并发送审批 UI（如 Discord ApprovalView 按钮）

        Args:
            approval_id: 审批请求唯一 ID。用户做出决策后传给 ClonothClient.approve()。
            operation: 操作名称（details["tool_name"] 或 payload["operation"]）。
            details: 审批详情 dict（来自 payload.details），
                     典型字段：tool_name, path, reason, args。
            conversation_key: 所属会话键，供目标频道定位使用。
            session_id: 所属 session ID，供频道查找 fallback。
        """
        ...

    # ================================================================
    #  Typing 刷新
    #  对应每轮 poll 结束后 sweep 阶段的 typing indicator 维持
    # ================================================================

    async def refresh_typing(
        self,
        trigger: TriggerInfo,
    ) -> None:
        """定期刷新平台 typing 指示器。

        触发时机：每轮 poll 结束后的 sweep 阶段，对所有活跃 trigger
                  检查距上次 typing 是否超过阈值（约 8 秒）。
        对应 bot_adapter.py L1993-2001 的 typing 刷新循环。

        适配器需要：
          - 在 trigger 关联的频道触发 typing 指示
            （如 Discord channel.typing()）
          - 更新 trigger.platform_data 中的 last_typing_time

        Args:
            trigger: 需要刷新 typing 的活跃触发信息。
        """
        ...

    # ================================================================
    #  反应（Bot 自定义约定，SDK 不解析内容，仅透传）
    # ================================================================

    async def add_reactions(
        self,
        trigger: TriggerInfo,
        reactions: list[str],
    ) -> None:
        """向触发消息添加平台反应。

        说明：[REACT:...] 标记属于 Bot 自定义约定（参见 sdk_refactor_plan_final.md
        1.1 节），不在 SDK 协议层内。SDK 不解析此标记的具体内容。

        此回调作为适配器的可选扩展点存在。适配器可以选择：
          方案 A — 在 send_reply / send_intermediate_reply 内部自行提取并处理反应
          方案 B — 将提取逻辑注册到 on_raw_event 钩子中，解析后调用此回调

        EventRouter 默认不调用此方法。只在适配器主动在 on_raw_event 等钩子中
        提取 reaction 列表后，才会通过此回调通知平台执行添加反应操作。

        Args:
            trigger: 目标触发消息。platform_data 中包含原始平台消息引用。
            reactions: 反应标识列表（如 emoji 字符串、Discord 自定义表情格式等）。
                       格式由 Bot 自定义约定决定，SDK 不解读内容。
        """
        ...

    # ================================================================
    #  任务生命周期
    #  对应 task_created, [BOT_RESTART], context_reset 等事件
    # ================================================================

    async def on_task_created(
        self,
        trigger: TriggerInfo,
        task_id: str,
    ) -> None:
        """根任务创建完成，通知适配器更新 UI。

        触发时机：task_created 事件，且为根任务（无 caller_task_id），
                  source_inbound_seq 命中 trigger。
        对应 bot_adapter.py L1284-1295 的 CancelView task_id 回填。

        SDK 已完成：
          - 将 task_id 回填到 trigger.task_id

        适配器需要：
          - 更新 UI 组件中的 task_id（如 Discord CancelView），
            使取消按钮能精准取消单个任务而非整个 session

        Args:
            trigger: 关联的触发信息，trigger.task_id 已更新为新值。
            task_id: 创建的根任务 ID。
        """
        ...

    async def on_restart_signal(
        self,
        conversation_key: str,
    ) -> None:
        """检测到重启信号，通知适配器发起安全重启。

        说明：[BOT_RESTART] 信号属于 Bot 自定义约定（参见 sdk_refactor_plan_final.md
        1.1 节），不在 SDK 协议层内。与 add_reactions 类似，此回调为可选扩展点。

        更常见的做法是适配器在 send_reply 回调中自行检测并处理重启信号，
        不经由此回调。此方法保留用于适配器选择将信号检测委托给 SDK 的场景。

        适配器需要：
          - 发起安全重启流程（延迟重启，等待当前操作完成）

        Args:
            conversation_key: 触发重启信号的会话键。
        """
        ...

    async def on_context_reset(
        self,
        conversation_key: str,
        reason: str,
        cleaned_triggers: list[TriggerInfo],
    ) -> None:
        """会话上下文被重置，通知适配器执行平台侧清理。

        触发时机：context_reset 事件。
        对应 bot_adapter.py L1950-1982。

        SDK 已根据 reason 区分处理（SessionState 方法）：
          - reason="compact" → 仅重置水位标记（reset_channel_watermark），
            不清理 trigger / session 状态。cleaned_triggers 为空列表。
          - 其他 reason（如 "clear"）→ 完整清理（cleanup_for_context_reset）：
              * 清理 conversation_key 匹配的所有 trigger
              * 删除 session 双向映射
              * 重置水位标记
              * 清理 DM 频道映射
              * 被清理的 trigger 通过 cleaned_triggers 传出

        适配器需要：
          - 清理平台侧历史缓存（如 Discord 频道消息历史队列）
          - 遍历 cleaned_triggers，编辑其 status_msg（如 "🔄 上下文已重置。"）
          - 清理关联的 MainTaskState 中的 log_msg
            （通过 trigger.platform_data["_stale_main_state"].platform_data["log_msg"]）

        Args:
            conversation_key: 被重置的会话键（如 "discord:123456789"）。
            reason: 重置原因。"compact" 表示上下文压缩，其他值表示完整清除。
            cleaned_triggers: 被清理的 trigger 列表（仅非 compact 时有内容）。
                              每个 trigger 的 platform_data["_stale_main_state"]
                              中附带关联的 MainTaskState（如有）。
        """
        ...

    async def on_engine_restarted(
        self,
        payload: dict[str, Any],
    ) -> None:
        """Engine worker restarted (generation changed).

        Triggered when engine_registered event has a non-empty
        previous_generation_id, indicating a restart rather than first boot.

        Adapter should notify relevant channels that the engine has restarted.

        Args:
            payload: engine_registered event payload containing:
                worker_id, generation_id, previous_generation_id,
                orphans_cancelled, ts.
        """
        ...
