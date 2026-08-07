"""EventRouter — SDK 事件轮询主循环与协议路由。

Phase 3 step 2 (2026-04-17): 初始创建。

替代 bot_adapter.py _outbound_poller() (L1206-2040)，将 834 行协议逻辑
提取到 SDK。EventRouter 轮询 Supervisor 事件流，执行协议状态管理，
然后通过 AdapterCallbacks 通知适配器执行平台操作。

核心设计约束（参见 data/sdk_refactor_plan_final.md）：
  1. 展示层逻辑不进 SDK：dot_state、点阵动画、节流(should_edit/mark_edited)、
     thinking_preview 等全部不处理。适配器通过 on_raw_event hook 拿到
     stream_delta 自行管理。
  2. 协议标记清理进 SDK：[CLONOTH_TOOL_TRACE v...]...[/CLONOTH_TOOL_TRACE]
     由 strip_protocol_markers() 统一处理。
  3. Bot 自定义标记不碰：[SPLIT]、[REACT:...]、[BOT_RESTART] 留在 text 中
     不处理，由适配器的 callback 实现自行解析。
  4. sweep 阶段：SDK 只做 typing refresh 通知和状态传递
     （调用 update_progress / update_child_progress），不做节流判断。
     适配器在 callback 实现中自行决定是否 edit。
  5. 审批：SDK 做全局去重(ApprovalTracker) + per-trigger 去重
     (MainTaskState.handled_approvals) + 内外分类 + 自动放行。
     外部操作通过 show_approval_ui 回调让适配器展示审批 UI。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .approval import ApprovalTracker, auto_approve, is_external_operation
from .callbacks import AdapterCallbacks
from .client import ClonothClient
from .config import BotConfig
from .state import ChildTaskState, MainTaskState, SessionState, TriggerInfo
from .types import Event

logger = logging.getLogger("clonoth_sdk.event_router")

# --------------------------------------------------------------------------
#  协议标记清理
# --------------------------------------------------------------------------

# 匹配 SDK 内部协议标记 [CLONOTH_TOOL_TRACE v...]...[/CLONOTH_TOOL_TRACE]
# 这些标记用于 engine 内部调试，不应暴露给 Bot 用户
_TOOL_TRACE_RE = re.compile(
    r"\[CLONOTH_TOOL_TRACE v\d+\].*?\[/CLONOTH_TOOL_TRACE\]",
    re.DOTALL,
)


def strip_protocol_markers(text: str) -> str:
    """移除 SDK 协议标记。Bot 自定义标记（[SPLIT]、[REACT] 等）不受影响。

    提取自 bot_adapter.py 中散落的 TOOL_TRACE 清理逻辑，
    整合为单一入口，由 EventRouter 在所有文本输出点统一调用。
    """
    if not text:
        return text
    return _TOOL_TRACE_RE.sub("", text).strip()


# ==========================================================================
#  EventRouter
# ==========================================================================


class EventRouter:
    """WebSocket 事件消费主循环 + 协议状态管理 + 适配器分发。

    [2026-06-03] WS-only mode. HTTP poll fallback has been removed.
    /v1/events is now purely a log/audit endpoint.

    通过全局 WebSocket /v1/ws 消费实时事件，处理协议状态（trigger 匹配、
    session 映射、watermark 推进、审批去重），然后通过 AdapterCallbacks
    通知适配器执行平台操作（发送消息、刷新 typing、编辑进度日志等）。
    断线时指数退避重连（2s → 4s → ... → 30s cap），永不降级。

    双层钩子架构：
      Layer 1 — on_raw_event hook：适配器注册的原始事件拦截器，
               在 SDK 默认处理之前调用。返回 'handled' 跳过默认处理。
      Layer 2 — AdapterCallbacks：SDK 完成协议处理后的通知回调，
               适配器实现平台操作。

    用法::

        router = EventRouter(client, state, callbacks, config)
        # 可选：注册 Layer 1 钩子
        router.set_raw_event_hook(my_hook)
        # 启动事件循环（阻塞，直到被取消或调用 stop()）
        await router.run()
    """

    # [2026-06-03] WS is the sole transport. No HTTP poll fallback.
    _WS_RETRY_DELAY = 2.0        # initial retry delay (seconds)
    _WS_MAX_RETRY_DELAY = 30.0   # exponential backoff cap
    _WS_SWEEP_INTERVAL = 3.0

    def __init__(
        self,
        client: ClonothClient,
        state: SessionState,
        callbacks: AdapterCallbacks,
        config: BotConfig,
        *,
        approval_tracker: ApprovalTracker | None = None,
        entry_node_id: str = "",
        poll_interval: float = 3.0,  # deprecated, kept for backward compat
    ):
        """
        Args:
            client: Supervisor HTTP API 客户端。
            state: 集中状态管理器。
            callbacks: 适配器回调接口实现。
            config: Bot 配置。
            approval_tracker: 审批去重追踪器（可选，默认新建）。
            entry_node_id: 入口节点 ID（可选，默认取 config.entry_node_id）。
            poll_interval: [deprecated] 已废弃，仅保留签名向后兼容。
        """
        self._client = client
        self._state = state
        self._cb = callbacks
        self._config = config
        self._approval = approval_tracker or ApprovalTracker()
        self._entry_node_id = entry_node_id or config.entry_node_id
        self._after_seq = 0
        self._running = False
        # [2026-05-29 restart detection fix] Why: request_restart(target=engine)
        # actually triggers a FULL restart (supervisor+engine), wiping supervisor's
        # in-memory _engine_generations dict. So engine_registered arrives with an
        # EMPTY previous_generation_id, and the old `if not prev_gen: return` guard
        # mistook every restart for a first boot — skipping on_engine_restarted and
        # leaving stale progress logs open. How: the bot tracks the last generation
        # it has seen itself, independent of supervisor's prev field. Purpose: detect
        # restarts reliably by comparing the new generation against our own record.
        self._last_seen_generation: str | None = None

        # [AutoC 2026-06-09] Why: output chain tasks should be displayed as
        # part of the main progress card, not as separate child entries.
        # How: build a task_id → caller_task_id mapping from task_started
        # events, then use it in _handle_node_event to decide merging.
        # Purpose: native task-tree grouping without engine-side changes.
        self._task_callers: dict[str, str] = {}

        # Layer 1 hook: 原始事件拦截器
        # 返回 None → SDK 继续默认处理；返回 'handled' → SDK 跳过此事件
        self._on_raw_event: Callable[[Event], Awaitable[str | None]] | None = None

    # ------------------------------------------------------------------
    #  公共接口
    # ------------------------------------------------------------------

    def set_raw_event_hook(
        self,
        hook: Callable[[Event], Awaitable[str | None]] | None,
    ) -> None:
        """注册 Layer 1 原始事件拦截器。

        钩子在 SDK 默认协议处理之前调用。
        返回 None → SDK 继续默认处理。
        返回 'handled' → SDK 跳过此事件。
        传入 None 可注销钩子。
        """
        self._on_raw_event = hook

    @property
    def after_seq(self) -> int:
        """当前事件流游标（最近处理的 seq）。"""
        return self._after_seq

    async def run(self) -> None:
        """事件主循环。纯 WS，断线指数退避重连，永不降级 HTTP poll。"""
        self._running = True
        logger.info("EventRouter started (WS-only mode)")

        retry_delay = self._WS_RETRY_DELAY
        while self._running:
            try:
                await self._run_ws()
                retry_delay = self._WS_RETRY_DELAY  # reset on clean close
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    "WS connection failed: %s; retrying in %.1fs",
                    e, retry_delay,
                    exc_info=True,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self._WS_MAX_RETRY_DELAY)

        logger.info("EventRouter stopped")

    def stop(self) -> None:
        """通知主循环停止。下一轮 poll 结束后退出。"""
        self._running = False

    async def _run_ws(self) -> None:
        """WebSocket 模式主循环。断线或连接结束时抛异常交给 run()。"""
        # [SDK WS 2026-05-19] Why: WS receive can block for a long time when no
        # events arrive, but typing refresh and progress updates must continue.
        # How: run a small periodic sweep task beside the socket iterator and
        # cancel it in finally. Purpose: keep adapter UI behavior equivalent to
        # the old poll loop.
        sweep_task = asyncio.create_task(self._run_ws_sweep_loop())
        try:
            async for raw_event in self._client.ws_connect(last_seq=self._after_seq):
                if not self._running:
                    break
                if raw_event.get("type") == "ping":
                    continue
                event = Event.from_dict(raw_event)
                self._after_seq = max(self._after_seq, event.seq)
                await self._dispatch(event)
            if self._running:
                raise ConnectionError("WebSocket stream ended")
        finally:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task


    async def _run_ws_sweep_loop(self) -> None:
        """WS 模式的定时 sweep；每 3 秒刷新 typing 和进度状态。"""
        while self._running:
            await asyncio.sleep(self._WS_SWEEP_INTERVAL)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """执行一次完整 sweep，用于 WS 定时器和后续共享调用。"""
        await self._cleanup_stale_triggers()
        await self._refresh_active_states()

    async def _cleanup_stale_triggers(self) -> None:
        """清理超时 trigger，并通知适配器删除对应状态消息。"""
        # [SDK WS 2026-05-19] Why: stale-trigger cleanup used to be tied to poll
        # ticks. How: isolate it so both HTTP poll and WS sweep can call it.
        # Purpose: switching transports does not leave old status messages behind.
        stale = self._state.cleanup_stale_triggers(timeout=600.0)
        for trigger in stale:
            try:
                await self._cb.delete_status_message(trigger)
            except Exception:
                pass

    async def _refresh_active_states(self) -> None:
        """刷新所有活跃 trigger 和子任务进度显示。"""
        # [SDK WS 2026-05-19] Why: typing refresh and progress edits are adapter
        # responsibilities, but the SDK decides when to ask for them. How: keep the
        # old sweep body in one helper. Purpose: WS mode and HTTP fallback share
        # identical progress-refresh semantics.
        for seq, trigger in list(self._state.triggers.items()):
            try:
                await self._cb.refresh_typing(trigger)
            except Exception:
                pass

            main_state = self._state.get_main_state(seq)
            if main_state:
                try:
                    await self._cb.update_progress(trigger, main_state)
                except Exception:
                    pass

        for task_key, child_state in list(self._state.child_task_states.items()):
            try:
                await self._cb.update_child_progress(task_key, child_state)
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  分发
    # ------------------------------------------------------------------

    async def _dispatch(self, event: Event) -> None:
        """将单个事件路由到 Layer 1 钩子，然后到协议处理器。"""
        # Layer 1: 原始事件拦截
        if self._on_raw_event:
            try:
                result = await self._on_raw_event(event)
                if result == "handled":
                    return
            except Exception as e:
                logger.error("on_raw_event hook error: %s", e)

        # Layer 2: 协议分发
        handler = self._HANDLERS.get(event.type)
        if handler:
            try:
                await handler(self, event)
            except Exception as e:
                logger.error(
                    "Handler error for %s: %s", event.type, e, exc_info=True,
                )

    # ==================================================================
    #  事件处理器
    #  每个方法处理一种或一组事件类型，注册在类末尾的 _HANDLERS dict 中。
    # ==================================================================

    # ------------------------------------------------------------------
    #  inbound_message — 注册 conversation_key ↔ session_id 映射
    # ------------------------------------------------------------------

    async def _handle_inbound_message(self, event: Event) -> None:
        """处理 inbound_message 事件：维护会话双向映射。

        对应 bot_adapter.py L1280-1283 中 inbound_message 事件的
        session 映射注册。
        """
        conv_key = event.payload.get("conversation_key", "")
        if conv_key and event.session_id:
            self._state.register_session(conv_key, event.session_id)

    # ------------------------------------------------------------------
    #  task_created — 回填 task_id 到 trigger
    # ------------------------------------------------------------------

    async def _handle_task_created(self, event: Event) -> None:
        """处理 task_created 事件：将 task_id 回填到 trigger。

        仅处理根任务（无 caller_task_id）且有 source_inbound_seq 匹配的情况。
        回填后适配器可通过 trigger.task_id 精准取消单个任务。
        对应 bot_adapter.py L1284-1295。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        task_id = p.get("task_id", "")
        caller = p.get("caller_task_id") or ""
        # 仅根任务（无 caller）且有 src_seq 时回填
        if src_seq and task_id and not caller:
            trigger = self._state.get_trigger(src_seq)
            if trigger:
                trigger.task_id = task_id
                branch_session_id = str(p.get("session_id") or event.session_id or "")
                if branch_session_id:
                    # [2026-05-19 approval ownership fix]
                    # Why: several platform adapters can poll the same global event stream,
                    # and approval events may later reference the branch session instead of
                    # the parent session. Without this mapping, an owned branch approval can
                    # look unowned, while another adapter may accidentally act on it.
                    # How: when a root task is created from this adapter's trigger, map the
                    # branch session back to the same conversation_key in this router's local
                    # SessionState. Purpose: keep approval handling adapter-local and preserve
                    # branch-session approval routing.
                    self._state.session_conv_map.setdefault(branch_session_id, trigger.conversation_key)
                try:
                    await self._cb.on_task_created(trigger, task_id)
                except Exception:
                    pass

        # [2026-05-29 方案C第一步] 为什么：async dispatch 子节点运行在独立
        # session 上，后续审批和进度事件只带子 session。怎么改：task_created
        # 到达时用结构化 dispatch_origin.parent_conversation_key 注册子 session
        # 到父频道；只有旧任务缺少结构化字段时才走兼容解析。目的：不再依赖
        # agent: 字符串模糊反推父频道。
        self._register_dispatch_child_session(event, p)

    def _payload_task_input_and_context(self, payload: dict) -> tuple[dict, dict]:
        """Return task.input and task.input.task_context from an event payload."""
        # [2026-05-29 方案C第一步] 为什么：task_created、审批、进度处理都需要
        # 使用同一套 route 元数据读取规则。怎么改：集中取出 payload.input 与
        # task_context，缺失时返回空 dict。目的：后续 resolver 复用同一入口。
        task_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        task_context = task_input.get("task_context") if isinstance(task_input.get("task_context"), dict) else {}
        return task_input, task_context

    def _adapter_owns_conversation_key(self, conv_key: str) -> bool:
        """Return whether a conversation key belongs to this adapter prefix."""
        # [2026-05-29 方案C第一步] 为什么：多个 adapter 共享全局事件流，不能在
        # 去重或创建进度前接管其他平台的事件。怎么改：集中检查 conversation_key
        # 是否匹配当前 adapter 的 prefix。目的：审批和子进度都先判归属再处理。
        conv_key = str(conv_key or "").strip()
        if not conv_key:
            return False
        prefix = self._config.conversation_key_prefix or ""
        return not prefix or conv_key.startswith(prefix + ":")

    def _structured_parent_conversation_key(self, payload: dict) -> str:
        """Read structured parent route key from dispatch metadata if present."""
        task_input, task_context = self._payload_task_input_and_context(payload)
        origins: list[dict] = []
        for raw in (
            task_input.get("_dispatch_origin"),
            task_input.get("dispatch_origin"),
            payload.get("dispatch_origin"),
            payload.get("_dispatch_origin"),
        ):
            if isinstance(raw, dict):
                origins.append(raw)
        # [2026-05-29 方案C第一步] 为什么：审计要求优先使用
        # dispatch_origin.parent_conversation_key，这是唯一不会受 agent: 存储
        # key 变形影响的路由可见性字段。怎么改：先扫 origin，再扫 task_context
        # 的兼容字段。目的：嵌套 dispatch 仍能稳定回到根父频道。
        for origin in origins:
            conv_key = str(
                origin.get("parent_conversation_key")
                or origin.get("route_conversation_key")
                or ""
            ).strip()
            if conv_key:
                return conv_key
        for key in ("parent_conversation_key", "route_conversation_key"):
            conv_key = str(task_context.get(key) or payload.get(key) or "").strip()
            if conv_key:
                return conv_key
        return ""

    def _legacy_parent_conversation_key_from_child_key(self, child_conv_key: str) -> str:
        """Best-effort fallback for old agent:-prefixed dispatch conversation keys."""
        child_conv_key = str(child_conv_key or "").strip()
        if not child_conv_key.startswith("agent:"):
            return ""
        rest = child_conv_key[len("agent:"):]
        if ":" not in rest:
            return ""
        _node_id, remainder = rest.split(":", 1)
        known_keys = [
            key for key in self._state.conversation_sessions.keys()
            if key and self._adapter_owns_conversation_key(key)
        ]
        known_keys.sort(key=len, reverse=True)
        # [2026-05-29 方案C第一步] 为什么：旧代码用 `(:known) in remainder`
        # 模糊匹配，可能把无关子串误判成父频道。怎么改：兼容路径只允许三种
        # 有边界的旧格式：父 key 等于剩余串、位于 fresh/fork 的开头、位于
        # context_key accumulate 的结尾。目的：保留旧任务可见性，同时删除脆弱
        # 的任意子串匹配。
        for known in known_keys:
            if remainder == known:
                return known
            if remainder.startswith(known + ":"):
                return known
            if remainder.endswith(":" + known):
                return known
        return ""

    def _resolve_route_conversation_key(
        self,
        event: Event,
        payload: dict,
        *,
        trigger_result: tuple[int, TriggerInfo] | None = None,
    ) -> str:
        """Resolve the user-visible conversation key for an SDK event."""
        structured = self._structured_parent_conversation_key(payload)
        if structured:
            return structured

        if trigger_result:
            # [2026-05-29 方案C第一步] 为什么：source_inbound_seq 是 SDK
            # 对入口请求的精确绑定，比可能尚未修正的子 session 映射更可靠。
            # 怎么改：结构化 dispatch route 缺失时，先使用已解析 trigger 的
            # conversation_key，再退回 session_conv_map。目的：避免旧 agent: 或
            # 其他 adapter 的临时映射覆盖本 adapter 的精确触发归属。
            conv_key = str(trigger_result[1].conversation_key or "").strip()
            if conv_key:
                return conv_key

        task_input, task_context = self._payload_task_input_and_context(payload)
        session_ids: list[str] = []
        for raw in (
            payload.get("session_id"),
            event.session_id,
            payload.get("parent_session_id"),
            payload.get("branch_session_id"),
            task_input.get("parent_session_id"),
            task_input.get("branch_session_id"),
        ):
            sid = str(raw or "").strip()
            if sid and sid not in session_ids:
                session_ids.append(sid)
        for sid in session_ids:
            conv_key = str(self._state.get_conversation_key(sid) or "").strip()
            if not conv_key:
                continue
            legacy = self._legacy_parent_conversation_key_from_child_key(conv_key)
            return legacy or conv_key

        child_conv_key = str(
            task_context.get("conversation_key")
            or payload.get("conversation_key")
            or ""
        ).strip()
        if child_conv_key:
            legacy = self._legacy_parent_conversation_key_from_child_key(child_conv_key)
            return legacy or child_conv_key
        return ""

    def _resolve_owned_route_conversation_key(
        self,
        event: Event,
        payload: dict,
        *,
        trigger_result: tuple[int, TriggerInfo] | None = None,
    ) -> str:
        """Resolve route key and return it only when this adapter owns it."""
        # [2026-05-29 方案C第一步] 为什么：审批和子进度如果先写去重或状态，
        # 非本 adapter 的事件会被永久吞掉或错误发到日志频道。怎么改：所有
        # 需要创建状态的路径先调用此 resolver。目的：未确认归属时直接跳过，
        # 让其他 adapter 继续处理。
        conv_key = self._resolve_route_conversation_key(
            event, payload, trigger_result=trigger_result,
        )
        if self._adapter_owns_conversation_key(conv_key):
            return conv_key
        return ""

    def _register_dispatch_child_session(self, event: Event, payload: dict) -> None:
        """Map dispatch child sessions to their parent conversation key."""
        try:
            task_input, _task_context = self._payload_task_input_and_context(payload)
            if not task_input:
                return
            dispatch_origin = task_input.get("_dispatch_origin") if isinstance(task_input.get("_dispatch_origin"), dict) else {}
            child_conv_key = str(_task_context.get("conversation_key") or payload.get("conversation_key") or "").strip()
            if not dispatch_origin and not child_conv_key.startswith("agent:"):
                # [2026-05-29 方案C第一步] 为什么：普通入口 task 也会进入
                # task_created 处理器，但不应该被当成 dispatch 子 session 重新映射。
                # 怎么改：只有存在结构化 dispatch_origin，或旧任务使用 agent:
                # conversation_key 时才继续。目的：避免主入口分支被误注册为子任务。
                return
            parent_conv_key = self._resolve_owned_route_conversation_key(event, payload)
            if not parent_conv_key:
                logger.debug(
                    "dispatch child session ignored: no owned parent route for task=%s session=%s",
                    str(payload.get("task_id") or "")[:12], event.session_id[:12],
                )
                return

            parent_sid = self._state.get_session_id(parent_conv_key) or ""
            parent_dm = self._state.get_dm_channel(parent_sid) if parent_sid else None
            child_sids: set[str] = set()
            for key in ("parent_session_id", "branch_session_id"):
                sid = str(task_input.get(key) or payload.get(key) or "").strip()
                if sid:
                    child_sids.add(sid)
            origin = dispatch_origin
            origin_parent_sid = str(origin.get("parent_session_id") or "").strip() if isinstance(origin, dict) else ""
            if origin_parent_sid:
                child_sids.add(origin_parent_sid)
            payload_sid = str(payload.get("session_id") or event.session_id or "").strip()
            if payload_sid:
                child_sids.add(payload_sid)
            if parent_sid:
                child_sids.discard(parent_sid)
            if not child_sids:
                return
            # [2026-05-29 方案C第一步] 为什么：inbound_message 已经把 dispatch
            # session 映射为 agent: 存储 key，使用 setdefault 会保留错误可见性。
            # 怎么改：用结构化 parent_conversation_key 强制覆盖子 session 的反向映射。
            # 目的：后续审批和进度按父频道归属，而不是被 agent: 前缀过滤。
            for child_sid in child_sids:
                previous = self._state.session_conv_map.get(child_sid)
                self._state.session_conv_map[child_sid] = parent_conv_key
                if previous and previous != parent_conv_key:
                    logger.info(
                        "dispatch child session %s conv_key remapped %s -> %s",
                        child_sid[:12], previous, parent_conv_key,
                    )
                if parent_dm and self._state.get_dm_channel(child_sid) is None:
                    try:
                        self._state.register_dm_channel(child_sid, parent_dm)
                    except Exception:
                        pass
            logger.info(
                "dispatch child sessions %s -> parent conv_key %s (dm=%s)",
                [sid[:12] for sid in child_sids], parent_conv_key, bool(parent_dm),
            )
        except Exception as e:
            logger.error("_register_dispatch_child_session error: %s", e)

    # ------------------------------------------------------------------
    #  outbound_message — 主节点回复 / fallback 发送
    # ------------------------------------------------------------------

    async def _handle_outbound_message(self, event: Event) -> None:
        """处理 outbound_message 事件。

        两条路径：
          Path 1（主节点回复）：src_seq 命中 trigger → 消费 trigger，
              移除 MainTaskState，清理协议标记，调用 send_reply。
              对应 bot_adapter.py L1297-1373。
          Path 2（Fallback）：无 trigger 匹配 → 过滤 system.* 节点，
              通过 session_conv_map 解析 conv_key，调用 send_to_channel。
              对应 bot_adapter.py L1375-1434。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)

        # Path 1: 主节点回复（src_seq 在 triggers 中）
        if src_seq and src_seq in self._state.triggers:
            trigger = self._state.consume_trigger(src_seq)
            main_state = self._state.remove_main_state(src_seq)
            text = strip_protocol_markers((p.get("text") or "").strip())
            attachments = p.get("attachments") or []
            try:
                await self._cb.send_reply(
                    trigger, text, attachments, main_state=main_state,
                )
            except Exception as e:
                logger.error("send_reply failed: %s", e)
            return

        # Path 2: Fallback（子节点 / 调度任务输出）
        node_id = p.get("node_id", "") or ""
        # 过滤系统内部节点，不向平台发送
        if node_id.startswith("system."):
            logger.debug("Skip system node outbound: %s", node_id)
            return

        conv_key = self._state.get_conversation_key(event.session_id) or ""
        text = strip_protocol_markers((p.get("text") or "").strip())
        attachments = p.get("attachments") or []
        if not text and not attachments:
            return

        try:
            await self._cb.send_to_channel(
                conv_key, text, attachments, node_id=node_id,
            )
        except Exception as e:
            logger.error("send_to_channel failed: %s", e)

    # ------------------------------------------------------------------
    #  intermediate_reply — 中间回复（入口节点 / 子节点）
    # ------------------------------------------------------------------

    async def _handle_intermediate_reply(self, event: Event) -> None:
        """处理 intermediate_reply 事件。

        两条路径：
          Path 1（入口节点中间回复）：src_seq 命中 trigger 且为入口节点 →
              刷新 trigger、清空 stream buffer、调用 send_intermediate_reply。
              对应 bot_adapter.py L1435-1465。
          Path 2（子节点/Fallback）：非入口节点 → 更新子任务日志，
              通过 session_conv_map fallback 调用 send_to_channel。
              对应 bot_adapter.py L1467-1520。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        node_id = p.get("node_id", "")

        # Path 1: 入口节点中间回复
        if (
            src_seq
            and src_seq in self._state.triggers
            and node_id == self._entry_node_id
        ):
            trigger = self._state.get_trigger(src_seq)
            trigger.refresh()
            text = strip_protocol_markers((p.get("text") or "").strip())
            attachments = p.get("attachments") or []
            # 清空流式 buffer（中间回复已包含完整内容）
            main_state = self._state.get_main_state(src_seq)
            if main_state:
                main_state.stream_parts.clear()
            if text or attachments:
                try:
                    await self._cb.send_intermediate_reply(trigger, text, attachments)
                except Exception as e:
                    logger.error("send_intermediate_reply failed: %s", e)
            return

        # 过滤系统内部节点
        if (node_id or "").startswith("system."):
            return

        # 更新子任务日志（如存在）
        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state = self._state.get_child_state(task_key)
        if child_state:
            child_state.lines.append("↳ 已发送中间回复")
            try:
                await self._cb.update_child_progress(task_key, child_state)
            except Exception:
                pass

        # Fallback: 通过 session_conv_map 发送到频道
        conv_key = self._state.get_conversation_key(event.session_id) or ""
        text = strip_protocol_markers((p.get("text") or "").strip())
        if text:
            try:
                await self._cb.send_to_channel(
                    conv_key, text, [], node_id=node_id,
                )
            except Exception as e:
                logger.error(
                    "intermediate_reply send_to_channel failed: %s", e,
                )

    # ------------------------------------------------------------------
    #  assistant_text — hybrid 模式 free prose 伴随文本
    # ------------------------------------------------------------------

    async def _handle_assistant_text(self, event: Event) -> None:
        """处理 assistant_text 事件。

        hybrid 模式下，模型输出 tool calls 同时伴随的 free prose 文本。
        匹配 trigger 后转发给适配器，用于在进度消息中滚动显示。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        node_id = p.get("node_id", "")

        if (
            src_seq
            and src_seq in self._state.triggers
            and node_id == self._entry_node_id
        ):
            trigger = self._state.get_trigger(src_seq)
            trigger.refresh()
            text = strip_protocol_markers((p.get("text") or "").strip())
            if text:
                try:
                    await self._cb.on_assistant_text(trigger, text)
                except Exception as e:
                    logger.error("on_assistant_text failed: %s", e)

    # ------------------------------------------------------------------
    #  task_started — task 树映射构建
    # ------------------------------------------------------------------

    async def _handle_task_started(self, event: Event) -> None:
        """从 task_started snapshot 提取 caller_task_id，构建 task 树映射。"""
        p = event.payload
        task_id = str(p.get("task_id") or "").strip()
        caller_id = str(p.get("caller_task_id") or "").strip()
        if task_id and caller_id:
            self._task_callers[task_id] = caller_id

    # ------------------------------------------------------------------
    #  node_started / node_completed — 节点生命周期
    # ------------------------------------------------------------------

    async def _handle_node_event(self, event: Event) -> None:
        """处理 node_started 和 node_completed 事件。

        入口节点 → 更新 MainTaskState 进度记录 + 通知适配器。
        非入口节点 → 更新 ChildTaskState 日志行 + 通知适配器。
        对应 bot_adapter.py L1539-1587。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        node_name = p.get("node_name", "") or node_id
        src_seq = int(p.get("source_inbound_seq") or 0)
        label = "▶ 节点启动" if event.type == "node_started" else "✓ 节点完成"
        msg = f"{label}: {node_name}"

        # 隔离系统任务：system.* 节点严禁回退到主触发，必须作为独立子任务日志处理
        is_system = (node_id or "").startswith("system.")

        # [Fork/Merge 2026-05-12] 查找关联 trigger 时只使用 source_inbound_seq。
        # Why: entry branches share the parent session, and session fallback can attach a node event
        # to another active inbound. How: resolve_trigger now ignores session_id while preserving the
        # call signature. Purpose: keep progress records scoped to the exact triggering message.
        result = None
        if not is_system:
            result = self._state.resolve_trigger(src_seq, event.session_id)

        # 入口节点 → 主任务进度
        if result and node_id == self._entry_node_id:
            trigger_seq, trigger = result
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(trigger_seq)
            main_state.progress_records.append(msg)
            try:
                await self._cb.update_progress(trigger, main_state)
            except Exception:
                pass
        # 非入口节点 → 子任务日志
        elif node_id and node_id != self._entry_node_id:
            # [AutoC 2026-06-09] Why: output chain tasks (quality check etc.)
            # are synchronous children of the main task. How: check _task_callers
            # mapping built from task_started events; if caller exists and trigger
            # resolves, merge into main progress card. Purpose: task-tree native
            # grouping without engine changes.
            task_id = str(p.get("task_id") or "").strip()
            caller_id = self._task_callers.get(task_id, "")
            if caller_id and result:
                trigger_seq, trigger = result
                trigger.refresh()
                main_state = self._state.get_or_create_main_state(trigger_seq)
                main_state.progress_records.append(msg)
                try:
                    await self._cb.update_progress(trigger, main_state)
                except Exception:
                    pass
                return

            # [2026-05-29 方案C第一步] 为什么：子节点生命周期事件可能来自其他
            # adapter 的子 session，旧逻辑会在确认归属前创建 ChildTaskState，并把
            # 无归属事件回退到日志频道。怎么改：创建状态前先走共享 route resolver。
            # 目的：非本 adapter 的事件直接跳过，不污染本地状态。
            conv_key = self._resolve_owned_route_conversation_key(
                event, p, trigger_result=result,
            )
            if not conv_key:
                logger.debug(
                    "node event ignored (unowned route): type=%s node=%s session=%s",
                    event.type, node_id, event.session_id,
                )
                return
            task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
            child_state, is_new = self._state.get_or_create_child_state(
                task_key, prefix=node_id,
            )
            child_state.lines.append(msg)

            if is_new:
                try:
                    await self._cb.create_child_progress(
                        task_key, child_state,
                        trigger=result[1] if result else None,
                        conversation_key=conv_key,
                        session_id=event.session_id,
                    )
                except Exception:
                    pass
            else:
                try:
                    await self._cb.update_child_progress(task_key, child_state)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    #  handoff_progress — 节点进度汇报（主节点 / 子节点）
    # ------------------------------------------------------------------

    async def _handle_handoff_progress(self, event: Event) -> None:
        """处理 handoff_progress 事件。

        入口节点 → 追加到 MainTaskState.progress_records + 通知更新。
        非入口节点 → 创建或更新 ChildTaskState + 通知适配器。
        子节点首次出现时调用 create_child_progress 让适配器创建显示消息。
        对应 bot_adapter.py L1595-1707。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        # 过滤系统内部节点（如 turn_summarizer），不推送进度
        if (node_id or "").startswith("system."):
            return
        src_seq = int(p.get("source_inbound_seq") or 0)
        hp_msg = (p.get("message") or "").strip()
        is_system = (node_id or "").startswith("system.")

        # 主节点进度
        if (
            not is_system
            and src_seq
            and src_seq in self._state.triggers
            and node_id == self._entry_node_id
        ):
            trigger = self._state.get_trigger(src_seq)
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(src_seq)
            if hp_msg:
                main_state.progress_records.append(hp_msg)
                try:
                    await self._cb.update_progress(trigger, main_state)
                except Exception:
                    pass
            return

        # 子节点进度
        if not node_id or node_id == self._entry_node_id or not hp_msg:
            return

        # [Fork/Merge 2026-05-12] Use only source_inbound_seq for trigger attachment.
        # Why: session fallback is ambiguous when several branch tasks share one parent session.
        # How: resolve_trigger keeps the old signature but ignores event.session_id. Purpose:
        # child progress is attached only to the inbound that created this task.
        result = self._state.resolve_trigger(src_seq, event.session_id)
        # [2026-05-29 方案C第一步] 为什么：子进度事件可能属于其他 adapter，旧逻辑
        # 在解析 conversation_key 前就创建状态，并在无匹配时回退到日志频道。怎么改：
        # 使用与 dispatch 子 session 相同的 route resolver，只有确认归属后才创建
        # ChildTaskState。目的：非本 adapter 的子事件直接跳过，让正确 adapter 处理。
        conv_key = self._resolve_owned_route_conversation_key(
            event, p, trigger_result=result,
        )
        if not conv_key:
            logger.debug(
                "handoff_progress ignored (unowned route): task=%s node=%s session=%s",
                str(p.get("task_id") or "")[:12], node_id, event.session_id,
            )
            return

        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state, is_new = self._state.get_or_create_child_state(
            task_key, prefix=node_id,
        )

        if is_new:
            child_state.lines.append(hp_msg)
            try:
                await self._cb.create_child_progress(
                    task_key, child_state,
                    trigger=result[1] if result else None,
                    conversation_key=conv_key,
                    session_id=event.session_id,
                )
            except Exception as e:
                logger.error("create_child_progress failed: %s", e)
        else:
            child_state.lines.append(hp_msg)
            try:
                await self._cb.update_child_progress(task_key, child_state)
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  stream_delta — 流式输出片段缓存
    # ------------------------------------------------------------------

    async def _handle_stream_delta(self, event: Event) -> None:
        """处理 stream_delta 事件：将文本片段缓存到 MainTaskState。

        SDK 仅做 buffer 累积。dot_state / thinking_preview / 动画等
        展示层逻辑由适配器通过 on_raw_event hook 自行管理。
        对应 bot_adapter.py L1716-1760 的流式处理。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        content = p.get("content", "")
        stream_type = p.get("type", "text")

        if src_seq and src_seq in self._state.triggers:
            self._state.get_trigger(src_seq).refresh()
            main_state = self._state.get_or_create_main_state(src_seq)
            if content and stream_type == "text":
                main_state.stream_parts.append(content)
        # 注意：dot_state / thinking preview / animation 是适配器责任。
        # 适配器可通过 set_raw_event_hook 拦截 stream_delta 事件来实现。

    # ------------------------------------------------------------------
    #  approval_requested — 审批请求处理
    # ------------------------------------------------------------------

    async def _handle_approval_requested(self, event: Event) -> None:
        """处理 approval_requested 事件。

        处理流程：
          1. 全局去重（ApprovalTracker）
          2. per-trigger 去重（MainTaskState.handled_approvals）
          3. 分类：外部操作 → 通知适配器展示审批 UI；
                   内部操作 → 自动放行
          4. 向 progress_records 追加审批状态记录
        对应 bot_adapter.py L993-1006 _process_approval_event。
        """
        p = event.payload
        appr_id = p.get("approval_id", "")
        if not appr_id:
            return

        # [Fork/Merge 2026-05-12] per-trigger 去重只能按 source_inbound_seq 精确挂载。
        # Why: approval events may be routed through a shared parent session while multiple branch
        # tasks are active. How: do not call find_trigger_by_session; if the payload has no source
        # sequence, still show the approval UI by session mapping but skip main-progress attachment.
        # Purpose: approval handling remains visible without corrupting another trigger's progress.
        ap_session = str(p.get("session_id") or event.session_id)
        src_seq = int(p.get("source_inbound_seq") or 0)
        result = self._state.resolve_trigger(src_seq, ap_session)
        conv_key = self._resolve_owned_route_conversation_key(
            event, p, trigger_result=result,
        )
        # [2026-05-29 方案C第一步] 为什么：旧审批逻辑先 mark_handled 再判归属，
        # 子 session 映射未建立时会把审批永久丢掉，其他 adapter 也无法处理。
        # 怎么改：先解析 route key 并验证 prefix ownership；无法确认归属时直接返回。
        # 目的：只有本 adapter 确认拥有的审批才进入全局去重。
        if not conv_key:
            logger.debug(
                "approval_requested ignored before de-dup (unowned route): id=%s session=%s",
                appr_id, ap_session,
            )
            return

        # 全局去重必须发生在归属确认之后。
        if self._approval.is_handled(appr_id):
            return
        self._approval.mark_handled(appr_id)

        if result:
            trigger_seq, trigger = result
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(trigger_seq)
            if appr_id in main_state.handled_approvals:
                return
            main_state.handled_approvals.add(appr_id)

        # 分类审批操作：外部 vs 内部，结合 bot 侧 auto_approve_internal 配置决定放行策略。
        # fix: 原逻辑在 workspace_root 未配置时默认自动放行所有操作，不安全。
        # 现改为：auto_approve 行为由 bot 侧 config.auto_approve_internal 显式控制，
        # SDK 默认不自动放行。
        details = p.get("details") or {}
        operation = details.get("tool_name") or p.get("operation", "")

        # 判断是否为外部操作
        is_external = False
        if self._config.workspace_root:
            is_external = is_external_operation(
                details, self._config.workspace_root, self._config.extra_roots,
            )
        else:
            logger.warning("workspace_root not configured, treating all approvals as requiring manual review")
            is_external = True  # 没配置 workspace_root 视为全部需要人工审批

        logger.info(
            "approval_requested owned: id=%s session=%s route_conv_key=%s src_seq=%s",
            appr_id, ap_session, conv_key, src_seq,
        )

        # Operations that must never be auto-approved regardless of
        # auto_approve_internal setting (they are not file operations).
        _NEVER_AUTO_APPROVE_OPS = frozenset({"restart"})
        force_manual = operation in _NEVER_AUTO_APPROVE_OPS

        # 决定是否自动放行
        if force_manual or is_external or not self._config.auto_approve_internal:
            # 外部操作 或 bot 未开启自动放行 → 通知适配器展示审批 UI
            try:
                await self._cb.show_approval_ui(
                    appr_id, operation, details,
                    conversation_key=conv_key,
                    session_id=ap_session,
                )
            except Exception as e:
                logger.error("show_approval_ui failed: %s", e)
            status = f"⏳ 等待审批: {operation}"
        else:
            # 内部操作 且 bot 开启了自动放行 → 自动放行
            ok = await auto_approve(self._client, appr_id)
            status = (
                f"✅ 自动放行: {operation}"
                if ok
                else f"❌ 自动放行失败: {operation}"
            )

        # 将审批状态追加到主任务进度记录
        if result and status:
            main_state = self._state.get_main_state(result[0])
            if main_state:
                main_state.progress_records.append(status)

    # ------------------------------------------------------------------
    #  task_completed / task_cancelled — 任务生命周期终结
    # ------------------------------------------------------------------

    async def _handle_task_lifecycle(self, event: Event) -> None:
        """处理 task_completed 和 task_cancelled 事件。

        入口节点 task_cancelled → 清理 trigger + 编辑状态消息。
        非入口节点 → 移除子任务状态 + 通知适配器做最终更新。
        对应 bot_adapter.py L1790-1831。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        src_seq = int(p.get("source_inbound_seq") or 0)

        # [Fork/Merge 2026-05-12] 入口节点取消只按 source_inbound_seq 清理 trigger。
        # Why: cancelling one branch must not consume another trigger on the same parent session.
        # How: resolve_trigger performs exact source lookup only. Purpose: lifecycle cleanup follows
        # the task that actually ended.
        if node_id == self._entry_node_id and event.type == "task_cancelled":
            result = self._state.resolve_trigger(src_seq, event.session_id)
            if result:
                trigger_seq, trigger = result
                self._state.consume_trigger(trigger_seq)
                self._state.remove_main_state(trigger_seq)
                try:
                    await self._cb.edit_status_message(
                        trigger, "⚠️ 任务已取消。",
                    )
                except Exception:
                    pass
            return

        # 清理 task 树映射
        task_id = str(p.get("task_id") or "").strip()
        self._task_callers.pop(task_id, None)

        # 非入口节点 → 移除子任务状态并通知最终更新
        if not node_id or node_id == self._entry_node_id:
            return
        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state = self._state.remove_child_state(task_key)
        if child_state:
            status = (
                "✓ 任务完成"
                if event.type == "task_completed"
                else "✗ 任务已取消"
            )
            # [Fork/Merge 2026-05-12] DM 判断优先从精确 trigger 取得。
            # [2026-05-15] Fallback: system.* 节点没有 source_inbound_seq，
            # resolve_trigger 总是返回 None。此时用 session_dm_channels 判断，
            # 否则 DM 下的 system 节点日志永远不会被删除。
            result = self._state.resolve_trigger(src_seq, event.session_id)
            if result:
                is_dm = result[1].is_dm
            else:
                is_dm = bool(self._state.get_dm_channel(event.session_id))
            try:
                await self._cb.finalize_child_progress(
                    task_key, child_state, status, is_dm=is_dm,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  cancel_requested — 会话级取消请求
    # ------------------------------------------------------------------

    async def _handle_cancel_requested(self, event: Event) -> None:
        """处理 cancel_requested 事件：清理 trigger 并编辑状态消息。

        对应 bot_adapter.py L1838-1850。
        """
        p = event.payload
        # [Fork/Merge 2026-05-12] Do not clear triggers by session-level cancel events.
        # Why: a parent session can own several running branch triggers. How: only clear when a
        # source_inbound_seq is present and still maps to a trigger; otherwise task_cancelled events
        # will perform exact cleanup. Purpose: session cancel no longer deletes unrelated status UI.
        src_seq = int(p.get("source_inbound_seq") or 0)
        result = self._state.resolve_trigger(src_seq, event.session_id)
        if result:
            trigger_seq, trigger = result
            self._state.consume_trigger(trigger_seq)
            self._state.remove_main_state(trigger_seq)
            try:
                await self._cb.edit_status_message(
                    trigger, "⚠️ 任务已取消。",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  task_preempted — 任务被新消息打断
    # ------------------------------------------------------------------

    async def _handle_task_preempted(self, event: Event) -> None:
        """处理 task_preempted 事件：清理被打断任务的所有关联状态。

        通过 SessionState.cleanup_for_task_preempted 一次性完成
        trigger 消费、MainTaskState 移除、子任务状态移除。
        然后通知适配器编辑状态消息和最终化子任务进度。
        对应 bot_adapter.py L1852-1887。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        task_id = str(p.get("task_id") or "")

        # [Fork/Merge 2026-05-12] Preempt cleanup is exact by source_inbound_seq and task_id.
        # Why: session fallback can clear a different branch's trigger or child progress. How:
        # cleanup_for_task_preempted consumes only the matching source trigger and removes only the
        # exact child task key when present. Purpose: a preempt event cannot affect sibling branches.
        trigger, child_states = self._state.cleanup_for_task_preempted(
            src_seq, event.session_id, task_id=task_id,
        )

        if trigger:
            try:
                await self._cb.edit_status_message(
                    trigger, "⚡ 已被新消息打断。",
                )
            except Exception:
                pass
        # 最终化所有精确关联的子任务状态。无 trigger 时按非 DM 处理，避免 session fallback。
        is_dm = trigger.is_dm if trigger else False
        for task_key, child_state in child_states:
            try:
                await self._cb.finalize_child_progress(
                    task_key, child_state, "⚡ 被打断", is_dm=is_dm,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  compact_start / compact_done / compact_failed — 上下文压缩
    # ------------------------------------------------------------------

    async def _handle_compact(self, event: Event) -> None:
        """处理上下文压缩生命周期事件。

        将压缩状态消息追加到 MainTaskState.progress_records，
        然后通知适配器刷新进度显示。
        对应 bot_adapter.py L1889-1948。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)

        # [Fork/Merge 2026-05-12] Compact progress attaches only to the exact inbound sequence.
        # Why: compact events from branch tasks may share a parent session. How: resolve_trigger
        # ignores session fallback and returns None when source_inbound_seq is missing. Purpose:
        # compression progress cannot appear on another active request.
        result = self._state.resolve_trigger(src_seq, event.session_id)
        if not result:
            return
        trigger_seq, trigger = result
        trigger.refresh()
        main_state = self._state.get_or_create_main_state(trigger_seq)

        # 根据事件类型生成状态消息
        if event.type == "snip_compact":
            snipped = p.get("snipped_tasks", 0)
            msg = f"✂️ 轮摘要替换：压缩了 {snipped} 个旧任务"
        elif event.type == "compact_start":
            msg = "🗜️ 上下文压缩中…"
        elif event.type == "compact_done":
            before = p.get("before", 0)
            after = p.get("after", 0)
            if p.get("success", True):
                compressed_segs = p.get("compressed_segments")
                kept_segs = p.get("kept_segments")
                if compressed_segs is not None and kept_segs is not None:
                    msg = f"✅ 上下文已压缩：压缩 {compressed_segs} 个旧 task，保留 {kept_segs} 个（{before} → {after} 条消息）"
                else:
                    msg = f"✅ 上下文已压缩：{before} → {after} 条消息"
            else:
                msg = "⚠️ 上下文压缩失败（静默恢复）"
        else:  # compact_failed
            err = p.get("error", "")
            msg = (
                f"⚠️ 上下文压缩失败：{err[:80]}"
                if err
                else "⚠️ 上下文压缩失败"
            )

        main_state.progress_records.append(msg)
        try:
            await self._cb.update_progress(trigger, main_state)
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  context_reset — 会话上下文重置
    # ------------------------------------------------------------------

    async def _handle_context_reset(self, event: Event) -> None:
        """处理 context_reset 事件。

        按 reason 区分：
          reason='compact' → 仅重置水位标记，不清理 trigger/session 状态。
          其他 reason → 通过 cleanup_for_context_reset 完整清理。
        然后通知适配器执行平台侧清理。
        对应 bot_adapter.py L1950-1982。
        """
        p = event.payload
        conv_key = p.get("conversation_key", "")
        reason = p.get("reason", "")

        if reason == "compact":
            # Compact：仅重置水位标记
            # 约定：conv_key 格式为 "prefix:channel_id"（如 "discord:123456"）
            parts = conv_key.split(":", 1)
            if len(parts) == 2:
                try:
                    ch_id = int(parts[1])
                    self._state.reset_channel_watermark(ch_id)
                except ValueError:
                    pass
            cleaned_triggers: list[TriggerInfo] = []
        else:
            # 完整清理
            # 约定：conv_key 格式为 "prefix:channel_id"（如 "discord:123456"）
            # 若其他平台 conv_key 含多个冒号，int(parts[1]) 会失败并被 except 捕获
            parts = conv_key.split(":", 1)
            ch_id = None
            if len(parts) == 2:
                try:
                    ch_id = int(parts[1])
                except ValueError:
                    pass
            cleaned_triggers = self._state.cleanup_for_context_reset(
                conv_key, channel_id=ch_id,
            )

        try:
            await self._cb.on_context_reset(conv_key, reason, cleaned_triggers)
        except Exception as e:
            logger.error("on_context_reset callback failed: %s", e)

    # ------------------------------------------------------------------
    #  inbound_accepted — 水位确认
    # ------------------------------------------------------------------

    async def _handle_inbound_accepted(self, event: Event) -> None:
        """处理 inbound_accepted 事件：推进频道高水位。

        inbound 消息被 Supervisor 正式接受后，才将 pending_watermark
        推进为 last_ctx_seq，防止未接受的消息错误地认为历史已发送。
        对应 bot_adapter.py L1984-1989。
        """
        p = event.payload
        ia_seq = int(p.get("inbound_seq") or 0)
        if ia_seq:
            self._state.accept_watermark(ia_seq)

    # ------------------------------------------------------------------
    #  engine_registered — Engine 重启检测
    # ------------------------------------------------------------------

    async def _handle_engine_registered(self, event: Event) -> None:
        """Handle engine_registered: notify adapter when engine restarts.

        [2026-05-29 restart detection fix] The old logic trusted supervisor's
        previous_generation_id, but a full restart (the only restart path now,
        since target=engine is disabled in api.py) wipes supervisor's in-memory
        generation map, so that field is empty and every restart looked like a
        first boot. We now track the generation ourselves and compare.
        """
        p = event.payload
        cur_gen = (p.get("generation_id") or "").strip()
        prev_gen = (p.get("previous_generation_id") or "").strip()

        # Determine whether this is a real restart:
        #  - supervisor reports a non-empty previous_generation_id (in-process
        #    engine restart, generation map survived) → trust it; OR
        #  - we've seen a different generation before (full restart wiped the
        #    supervisor field, but our own record proves the generation changed).
        is_restart = bool(prev_gen) or (
            self._last_seen_generation is not None
            and cur_gen != ""
            and cur_gen != self._last_seen_generation
        )

        # Record current generation for the next comparison regardless of outcome.
        if cur_gen:
            self._last_seen_generation = cur_gen

        if not is_restart:
            # Genuine first boot (we had no prior generation on record) — skip
            # cleanup, just remember the generation above.
            return
        try:
            await self._cb.on_engine_restarted(p)
        except Exception as e:
            logger.error("on_engine_restarted callback error: %s", e)

    async def _handle_engine_restarting(self, event: Event) -> None:
        """Handle engine_restarting: supervisor is about to restart, clean up now.

        [2026-06-11] Why: full restart kills the supervisor process, WS drops
        with 1012 and bot misses the post-restart engine_registered event.
        How: supervisor emits engine_restarting while WS is still alive.
        Purpose: bot cleans up typing/logs/status_msg before disconnect.
        """
        try:
            await self._cb.on_engine_restarted(event.payload)
        except Exception as e:
            logger.error("on_engine_restarted (pre-restart) callback error: %s", e)

    async def _handle_raw_only_event(self, event: Event) -> None:
        """处理仅面向 on_raw_event hook 的事件类型。"""
        # [SDK WS 2026-05-19] Why: tool_call_* / stream_end /
        # approval_decided are display-layer signals and do not require SDK state
        # mutation yet. How: register a no-op handler after the raw-event hook has
        # already run. Purpose: make these event types explicit in the dispatch
        # table while preserving AdapterCallbacks compatibility.
        return None

    # ==================================================================
    #  Handler 分发表
    #  类变量，映射 event.type → 处理器方法。
    #  _dispatch() 通过此表路由事件到对应处理器。
    # ==================================================================

    _HANDLERS: dict[
        str, Callable[["EventRouter", Event], Awaitable[None]]
    ] = {
        "inbound_message":   _handle_inbound_message,
        "task_created":      _handle_task_created,
        "outbound_message":  _handle_outbound_message,
        "intermediate_reply": _handle_intermediate_reply,
        "task_started":      _handle_task_started,
        "node_started":      _handle_node_event,
        "node_completed":    _handle_node_event,
        "handoff_progress":  _handle_handoff_progress,
        "stream_delta":      _handle_stream_delta,
        "assistant_text":    _handle_assistant_text,
        "approval_requested": _handle_approval_requested,
        "task_completed":    _handle_task_lifecycle,
        "task_cancelled":    _handle_task_lifecycle,
        "cancel_requested":  _handle_cancel_requested,
        "task_preempted":    _handle_task_preempted,
        "compact_start":     _handle_compact,
        "compact_done":      _handle_compact,
        "compact_failed":    _handle_compact,
        "snip_compact":      _handle_compact,
        "context_reset":     _handle_context_reset,
        "inbound_accepted":  _handle_inbound_accepted,
        "engine_registered": _handle_engine_registered,
        "engine_restarting": _handle_engine_restarting,
        # [SDK WS 2026-05-19] Why: these new events should reach raw-event hooks
        # but do not need SDK-owned state changes. How: route them to a no-op
        # handler after Layer 1 dispatch. Purpose: unknown-handler silence remains
        # available for future events while these supported events are explicit.
        "tool_call_start":  _handle_raw_only_event,
        "tool_call_end":    _handle_raw_only_event,
        "tool_call_delta":  _handle_raw_only_event,
        "stream_end":       _handle_raw_only_event,
        "approval_decided": _handle_raw_only_event,
    }
