from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry

from .pseudo_tools import (
    _dispatch_delegate_specs,
    _is_pseudo_tool_name,
    _finish_spec,
    _ask_spec,
    _reply_spec,
    _compact_context_spec,
    _preempt_task_spec,
    _switch_node_spec,
    _to_openai_tools,
    _filter_tool_specs,
)
from .resume_builder import _build_resume_messages
from .loop_state import _LoopState, _persist_ctx, _short
from .llm_call import _call_llm_with_retry, _build_failure_action, _is_retryable_error, _RETRYABLE_STATUS_CODES
from .pseudo_handlers import _handle_pseudo_tool
# 异步工具生命周期（启动、跟踪、结果投递）已抽入独立模块。
# _async_tool_tasks 与本模块共享同一字典引用，原地修改对两侧均可见。
from .async_tools import (
    _async_tool_tasks,
    _cleanup_async_tracker,
    _snapshot_tool_context,
    _execute_command_async_upgrade_threshold,
    _execute_registry_tool_with_span,
    _deliver_started_async_task,
    _run_async_tool,
)

from ..context_store import load_context_snapshot
from ..attachments import build_multimodal_content
# Phase 1 (Session Conversation Store): 导入 Message 模型用于影子写入。
# ai_step 在每次 append assistant/tool_result 消息后，best-effort 写入 ConversationStore。
from ..conversation_store import ConversationStore, Message, MessageType
from ..node import Node
from .message_assembly import assemble_initial_messages
from ..protocol import (
    TaskAction,
    ACTION_DISPATCH,
    ACTION_FINISH,
    ACTION_ASK,
    ACTION_FAIL,
    ACTION_CANCELLED,
    ACTION_PREEMPTED,
)
from ..tool_step import (
    # [AutoC 2026-08-19] artifact_enabled / get_tool_step_inline_budget / write_artifact
    # 随 spill 策略迁入 engine/builtin/spill_policy.py；此处仅保留结构化
    # 格式化与兑底截断所需的最小导入。
    get_tool_inline_limit,
    result_to_raw,
    summarize_result,
    truncate_tool_result,
)
# [2026-04-24] P1.5 熔断器：压缩成功后重置熔断计数。
# 压缩决策逻辑已整体迁入 engine/builtin/compact.py（CompactChecker 插件），
# ai_step 仅保留 record_compact_success 用于 compact 恢复路径。
from ..compact import record_compact_success
from clonoth_runtime import get_int, get_float, load_runtime_config
from toolbox.context import ToolContext
# build_llm_messages: 反序列化方向的格式转换，在 llm_call.py 中实际调用。
# 此处导入供外部通过 ai_step 模块访问（如测试、调试）。
from .tool_format import (
    ParsedToolCall,
    create_tool_formatter,
    build_llm_messages,
)
from .message_model import MessageMeta, set_message_meta
from providers.base import BaseProvider, ToolCall, ProviderResponse
# Phase 2 Signal System: 导入信号总线，用于发射 tool.call 和 task.error 信号。
# get_bus() 返回全局单例 SignalBus，Signal 是不可变事件数据类。
from ..signals import Signal, get_bus
# Phase 3 Hook System：引入 hook registry 与上下文对象。
# 原因：before_tool_call 的业务检查要从 ai_step.py 的硬编码分支迁出。
# 做法：ai_step 只负责构造 HookContext 并触发 registry；具体规则由 handler 实现。
# 目的：后续内核逻辑可以插件化注册，同时保持当前推理循环行为不变。
from ..hooks import HookContext, hook_registry
from ..builtin.loader import auto_discover_and_register
from ..hooks.loader import load_external_plugins

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..context import RunContext


# ---------------------------------------------------------------------------
#  Phase 1: 影子写入辅助函数
#  将 ls.messages.append() 产生的消息同步写入 ConversationStore（JSONL）。
#  best-effort：任何异常静默忽略，绝不影响主推理流程。
#  仅处理 assistant 和 tool_result 消息；_dynamic/_ephemeral 消息跳过。
# ---------------------------------------------------------------------------

def _shadow_write(ls: _LoopState, msg_dict: dict, message_type: str = "") -> None:
    """Best-effort shadow write to ConversationStore. Never breaks main flow.

    Phase 3: 写入成功后将 Message.id 记录到 ls.last_shadow_message_id，
    供 _persist_ctx 写入 snapshot 的 last_message_id 字段。

    Child Session 隔离（Phase B）：写入目标优先使用 rctx.child_session_id，
    使子节点的消息写入自己的 JSONL 而非父 session。
    """
    try:
        store = getattr(ls.rctx, 'conversation_store', None)
        if store is None:
            return
        # 跳过 dynamic context 和 ephemeral 消息（如 retry hint）
        if msg_dict.get('_dynamic') or msg_dict.get('_ephemeral'):
            return
        # [2026-05-07] 不再按 control_tool_name 跳过 finish。
        # 原因：finish 已恢复为真实 API 工具，正常结果必须像普通工具一样进入 ConversationStore。
        # 做法：这里只保留 dynamic/ephemeral 两类运行期消息过滤，普通 tool_result 全部写入。
        # 目的：长期历史能够保存 assistant.tool_call 与 tool_result 的完整配对。
        from uuid import uuid4
        from datetime import datetime, timezone
        msg_id = str(uuid4())
        # [AutoC 2026-06-01] Why: converting non-string content with str()
        # destroyed multimodal user image blocks before same-task history reload.
        # How: pass provider-style list content through unchanged and only fall
        # back to text for other unexpected content shapes. Purpose: shadow
        # writes can preserve images for the active task while keeping legacy
        # string behavior for ordinary messages.
        _raw_content = msg_dict.get('content', '')
        _content = _raw_content if isinstance(_raw_content, (str, list)) else str(_raw_content)
        msg = Message(
            id=msg_id,
            role=msg_dict.get('role', 'user'),
            content=_content,
            message_type=message_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=msg_dict.get('_meta', {}),
            source_node_id=getattr(ls.node, 'id', ''),
            source_task_id=getattr(ls.rctx, 'task_id', ''),
            tool_calls=msg_dict.get('tool_calls', []),
            # [2026-05-01] 影子写入时保留原生 role=tool 的配对字段。
            # 原因：ConversationStore 是下一轮历史来源；丢失 tool_call_id 会破坏 true native。
            tool_call_id=str(msg_dict.get('tool_call_id') or ''),
            name=str(msg_dict.get('name') or ''),
        )
        # [Fork/Merge 2026-05-12] Child sessions still win, otherwise write to the runtime session.
        # Why: rctx.session_id may now be an entry branch, not the user-facing parent session. How:
        # keep child_session_id for delegated nodes and use rctx.session_id for main branch tasks.
        # Purpose: ConversationStore writes stay isolated until supervisor merges the branch.
        target_session = getattr(ls.rctx, 'child_session_id', '') or ls.rctx.session_id
        store.append(target_session, msg)
        # Phase 3: 记录最后一次影子写入的 message id，供 snapshot 持久化使用
        ls.last_shadow_message_id = msg_id
        # P0 Task 内核化：追踪 first/last message ID 到 RunContext
        if not ls.rctx.first_shadow_message_id:
            ls.rctx.first_shadow_message_id = msg_id
        ls.rctx.last_shadow_message_id = msg_id
    except Exception:
        pass  # best-effort, never break main flow


# ---------------------------------------------------------------------------
#  推理循环子函数
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  工具调用处理
# ---------------------------------------------------------------------------

async def _handle_tool_calls(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理 tool_calls（伪工具 + 真工具）。

    返回 TaskAction 则退出循环；返回 None 则 continue 到下一轮。
    """
    # 【方案 A 重构】伪工具改为列表按序执行，不再是 last-wins
    # 原本单标量 `pseudo_call = tc` 会导致同轮多伪工具只有最后一个生效，
    # 在 Fix 2（JSON 自由正文反向包装为 reply）后尤其危险——
    # 例如“委派工具 + 自由正文”会被吞掉委派，只留 reply。
    # 现改为按 LLM 输出顺序收集所有伪工具，后续按序处理，
    # 遇到返回 TaskAction 的（finish / switch_node / dispatch 等）立即退出循环，
    # 返回 None 的（reply / compact_context / preempt_task）继续执行下一个。
    pseudo_calls: list = []
    real_tool_calls: list[dict[str, Any]] = []
    _unauthorized_errors: list[dict[str, Any]] = []  # [fix 2026-06-24] collect, don't append yet
    for tc in resp.tool_calls:
        # [2026-05-04] Dynamic per-target dispatch tools are pseudo tools too.
        # Why: names like dispatch:child_coder are generated from delegate_targets
        # and must bypass real-tool authorization. How: use the prefix-aware helper
        # instead of a fixed name-only set. Purpose: route fixed-target dispatches
        # to pseudo_handlers without accepting removed aggregate dispatch tools.
        if _is_pseudo_tool_name(tc.name):
            pseudo_calls.append(tc)
        else:
            # 【Fix】真工具权限校验：工具必须在节点的授权列表内才能执行
            if tc.name not in ls.allowed_real_tools:
                logger.warning("node %s attempted unauthorized tool call: %s (allowed: %s)",
                               ls.node.id, tc.name, ls.allowed_real_tools)
                _err_msg = ls.formatter.format_tool_result(
                    tc,
                    f"Error: Tool '{tc.name}' is not in this node's allowed tool list. "
                    f"Use finish() to provide your output directly.",
                )
                # [2026-05-01] 工具结果必须带当前 tool_mode。
                # 目的：真 native 的 role=tool 消息在下一轮仍由 NativeToolFormatter 透传。
                set_message_meta(_err_msg, MessageMeta(
                    tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                    message_type="tool_result",
                ))
                # [fix 2026-06-24] 不在此处立即追加到 ls.messages。
                # 原因：assistant 消息在 for 循环之后才追加（行 541），如果在此处先
                # 追加 tool_result，消息列表会变成 [..., tool_result, assistant]，
                # 导致 _prepare_messages prefill guard 检测到末尾是 assistant 并注入
                # "请继续。"，引发模型反复重试同一个 unauthorized tool 的无限循环。
                # 做法：暂存到 _unauthorized_errors，在 assistant 消息之后统一追加。
                _unauthorized_errors.append(_err_msg)
                continue

            real_tool_calls.append({
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            })
            # P0 Task 内核化：记录工具调用摘要
            _args_str = str(tc.arguments or {})[:200]
            ls.rctx.tool_call_log.append({"name": tc.name, "args_summary": _args_str})

    # 将 LLM 的工具调用决策追加到对话历史
    _assistant_msg = ls.formatter.build_assistant_message(resp, resp.text or "", resp.tool_calls)
    # [refactor 2026-04-18] raw_parts → metadata, thinking_text → reasoning, has_thinking → has_reasoning
    # provider_meta 由 ProviderResponse 透传；engine 只搬运不解读
    # [fix 2026-04-18] provider 名称改为动态获取，不再硬编码 "openai"。
    # ls.provider.name 由 BaseProvider.name 提供，各 provider 子类在初始化时传入。
    _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
    _tc_meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        metadata={_provider_name: resp.provider_meta} if resp.provider_meta else {},
        tool_call_ids=[tc.id for tc in (resp.tool_calls or [])],
        reasoning=resp.reasoning or "",
        has_reasoning=bool(resp.reasoning),
        inline_data=resp.inline_data or [],
        usage=dict(ls.last_usage) if ls.last_usage else {},
        # [thinking-time 2026-06-01] Persist precise reasoning timing from stream.
        reasoning_started_at=getattr(ls, '_reasoning_started_iso', '') or '',
        reasoning_ended_at=getattr(ls, '_reasoning_ended_iso', '') or '',
    )
    set_message_meta(_assistant_msg, _tc_meta)
    # [2026-05-07] Store reasoning_content at top level for API round-trip.
    # DeepSeek V4 and similar models require this field in the message dict.
    # _meta.reasoning is kept for internal use but top-level survives L2 stripping.
    if resp.reasoning:
        _assistant_msg["reasoning_content"] = resp.reasoning
    ls.messages.append(_assistant_msg)
    # Phase 1: 影子写入 assistant 消息到 ConversationStore
    # [2026-05-07] 含 finish 的 assistant 消息也直接持久化。
    # 原因：finish 是真实 API tool_call，删除或拆分它会使后续 tool_result 失去配对来源。
    # 做法：不再调用 sanitize_assistant_control_tools，而是保存原始 assistant.tool_calls。
    # 目的：ConversationStore、snapshot 与 provider replay 的工具轮结构一致。
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

    # [fix 2026-06-24] 在 assistant 消息之后追加 unauthorized tool 的错误结果。
    # 保证消息顺序为 assistant → tool_result，避免 prefill guard 误触发。
    for _unerr in _unauthorized_errors:
        ls.messages.append(_unerr)
        _shadow_write(ls, _unerr, message_type="tool_result")

    # 正文处理策略（JSON / Fake Native / Native 模式统一）：
    # 工具调用伴随的自由正文通过 build_assistant_message 保留在 assistant 消息中。
    # [AutoC 2026-08-06] hybrid 模式下，伴随文本通过 assistant_text 事件推送给前端，
    # 前端可以 edit 消息来显示。tool_only 模式下行为不变（不推送）。
    _companion_text = (resp.text or "").strip()
    if _companion_text and getattr(ls.node, 'output_mode', 'hybrid') == 'hybrid':
        await ls.rctx.emit_event("assistant_text", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "text": _companion_text,
        })

    # ---- before_tool_call hook：本轮工具调用级检查 ----
    # Phase 3 Hook System：先触发 round-level hook，再进入伪工具和真实工具处理。
    # 原因：finish 并列检测这类业务规则不应继续硬编码在 ai_step.py。
    # 做法：把本轮所有 tool_calls 以及 legacy 过滤后的 pseudo/real 列表放进 HookContext。
    # 目的：handler 能复刻旧判断，同时后续可以继续迁移其他 before_tool_call 规则。
    _before_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step,
        response=resp,
        tool_calls=list(resp.tool_calls or []),
        extra={"pseudo_calls": pseudo_calls, "real_tool_calls": real_tool_calls},
    )
    _before_result = await hook_registry.afire("before_tool_call", _before_ctx)
    if _before_result.action is not None:
        return _before_result.action
    if _before_result.block:
        _reject_msg = (
            _before_result.error_message
            or _before_result.reason
            or "Tool call blocked by before_tool_call hook."
        )
        for tc in resp.tool_calls:
            _err = ls.formatter.format_tool_result(tc, _reject_msg)
            # [2026-05-07] 拒绝路径也按普通工具结果持久化。
            # 原因：finish_guard 产生的是对模型可见的错误 tool_result，若标记为 ephemeral，
            # 下一轮 provider 会看到 assistant.tool_call 缺少对应结果。
            # 做法：不再给 finish 错误结果设置 control_tool_name 或 _ephemeral。
            # 目的：被拒绝的 finish 与同轮其他工具一样保持完整配对历史。
            _rejection_code = str(_before_result.reason or "before_tool_call_blocked")
            set_message_meta(_err, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                tool_rejected=True,
                tool_rejection_code=_rejection_code,
                tool_result_visibility="hidden" if _rejection_code.endswith("_colocated") else "",
            ))
            ls.messages.append(_err)
            # [2026-05-07] before_tool_call 拒绝结果也要写入 ConversationStore。
            # 原因：这些结果是 assistant.tool_call 的真实回复，不是运行期占位消息。
            # 做法：与普通工具错误结果共用 _shadow_write。
            # 目的：模型重试时能看到完整的拒绝原因和工具配对。
            _shadow_write(ls, _err, message_type="tool_result")
        return None  # 不执行任何工具，回到主循环让 AI 重试
    if _before_result.skip_step:
        return None

    # LEGACY: replaced by hook FinishGuardHandler in engine.builtin.finish_guard.
    # 原因：保留原始硬编码判断，便于下一轮清理前核对行为。
    # 做法：只注释旧逻辑，不再执行；hook 使用 pseudo_calls/real_tool_calls 复刻同一判断。
    # 目的：迁移期间可快速回溯，不破坏当前 finish 并列拒绝语义。
    # _has_finish = any(_pc.name == "finish" for _pc in pseudo_calls)
    # _has_non_reply_others = bool(real_tool_calls) or any(
    #     _pc.name not in ("finish", "reply") for _pc in pseudo_calls
    # )
    # if _has_finish and _has_non_reply_others:
    #     _reject_msg = (
    #         "\u274c REJECTED: finish() cannot be called alongside other tools "
    #         "(except reply). Execute your other tools first, wait for their "
    #         "results, then call finish() alone in a separate turn."
    #     )
    #     logger.warning(
    #         "Rejected finish + other tools in same turn (node=%s, step=%d, tools=%s)",
    #         ls.node.id, step, [tc.name for tc in resp.tool_calls],
    #     )
    #     for tc in resp.tool_calls:
    #         _err = ls.formatter.format_tool_result(tc, _reject_msg)
    #         set_message_meta(_err, MessageMeta(
    #             tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
    #             message_type="tool_result",
    #         ))
    #         ls.messages.append(_err)
    #         _shadow_write(ls, _err, message_type="tool_result")
    #     return None  # 不执行任何工具，回到主循环让 AI 重试

    # 处理伪工具（finish/ask 延后到真实工具之后，确保同轮真实工具不被跳过）
    # [AutoC 2026-05-31] Why: ask terminates the task just like finish in Phase 0,
    # so it must share finish's delayed execution and preempt-intercept path. How:
    # store either terminal call in one slot and execute it after real tools.
    # Purpose: preserve existing finish semantics while introducing action="ask".
    _terminal_call = None
    if pseudo_calls:
        for _pc in pseudo_calls:
            if _pc.name in ("finish", "ask"):
                _terminal_call = _pc
                continue  # finish/ask 延后执行
            action = await _handle_pseudo_tool(ls, _pc, step)
            if action is not None:
                # 其他终止型伪工具（如 switch_node）仍然立刻退出。
                return action
            # 非终止型（reply / compact_context / preempt_task）继续

    # 处理真实工具
    if real_tool_calls:
        action = await _execute_real_tools(ls, real_tool_calls, step)
        if action is not None:
            return action
        ls.use_stream = ls.streaming

        if ls.preempt_after_step:
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )

    # finish/ask 最后执行（同轮真实工具已完成）
    if _terminal_call:
        # ---------------------------------------------------------------
        # Preempt V3 需求2: finish/ask 拦截
        # 在执行 finish/ask 之前再次检查 preempt 状态。如果有待注入的 preempt
        # 消息（用户在 LLM 推理/工具执行期间发了新消息），拦截 finish/ask：
        # 不产生 TaskAction(FINISH/ASK)，改为塞一个假 tool_result 维持 native
        # 模式下 tool_use/tool_result 的配对完整性（Claude API 强校验），
        # 然后让主循环继续，下一轮由 PreemptChecker 注入新用户消息。
        #
        # 同时补全 V2 遗漏：preempt_after_step（无消息 preempt）在只有 finish/ask
        # 没有真工具的场景下也需要被检查，此前会跳过导致终止工具照常执行。
        # ---------------------------------------------------------------
        if ls.preempt_inject_info is None and not ls.preempt_after_step:
            _pi_finish = await ls.rctx.check_preempted()
            if _pi_finish.get("preempted"):
                if _pi_finish.get("message"):
                    ls.preempt_inject_info = _pi_finish
                else:
                    ls.preempt_after_step = True

        if ls.preempt_inject_info is not None:
            # 有消息的 preempt：拦截 finish/ask，塞假 tool_result，任务继续
            from .tool_format import ParsedToolCall as _FinishPTC
            _terminal_name = str(getattr(_terminal_call, "name", "") or "finish").strip() or "finish"
            _finish_parsed = _FinishPTC(
                id=getattr(_terminal_call, "id", "") or "",
                name=_terminal_name,
                arguments=dict(_terminal_call.arguments or {}),
            )
            _intercept_msg = ls.formatter.format_tool_result(
                _finish_parsed,
                "\u26a0\ufe0f Preempted: new user input received. Task continues.",
            )
            # [2026-05-01] 写入当前 tool_mode，避免真 native 的拦截结果被当作旧 fake-native。
            # [2026-05-07] preempt 拦截 ACK 只服务当前运行期配对。
            # 原因：该终止工具未交付，不能让 fake-native/json 的文本结果在恢复后压制未来正常 finish/ask。
            # 做法：补齐 ephemeral、tool_call_id 和 name，让清洗函数按调用 ID 精确移除。
            # 目的：任务继续时不会向下一轮 provider 回放被拦截的终止工具。
            _intercept_msg["_ephemeral"] = True
            if _finish_parsed.id:
                _intercept_msg.setdefault("tool_call_id", _finish_parsed.id)
            _intercept_msg.setdefault("name", _terminal_name)
            set_message_meta(_intercept_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name=_terminal_name,
                control_tool_status="preempt_intercepted",
            ))
            ls.messages.append(_intercept_msg)
            # [2026-05-07] 被新用户输入拦截的 finish 未交付，不能写入长期历史。
            # [AutoC 2026-05-31] Why: ask has the same terminal semantics, so an
            # intercepted ask is also not a delivered clarification request. How:
            # keep the result only in runtime memory and do not call _shadow_write.
            # Purpose: next prompt is driven by real user input, not by replaying an
            # intercepted terminal tool.
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": step,
            })
            # 不 return TaskAction — 函数返回 None，主循环 continue 到下一轮
        elif ls.preempt_after_step:
            # 无消息的 preempt：与真工具后的 preempt_after_step 路径对齐，
            # 保存上下文后退出任务
            # [2026-05-01] 补写 finish/ask 的 tool_result，确保 native 模式下
            # functionCall/functionResponse 严格 1:1 配对（Gemini 强校验）
            from .tool_format import ParsedToolCall as _FinishPTC2
            _terminal_name2 = str(getattr(_terminal_call, "name", "") or "finish").strip() or "finish"
            _finish_parsed2 = _FinishPTC2(
                id=getattr(_terminal_call, "id", "") or "",
                name=_terminal_name2,
                arguments=dict(_terminal_call.arguments or {}),
            )
            _preempt_result = ls.formatter.format_tool_result(
                _finish_parsed2, "preempted",
            )
            # [2026-05-07] 无消息 preempt 的 finish ACK 同样只保留在运行期。
            # 原因：保存上下文后恢复时不应看到 finish tool_call/tool_result；但本轮内存仍需满足 provider 配对。
            # 做法：设置 ephemeral，并补齐 tool_call_id/name 供 snapshot 清洗精确匹配。
            # 目的：preempt 快照只恢复真实对话，不恢复控制流占位结果。
            _preempt_result["_ephemeral"] = True
            if _finish_parsed2.id:
                _preempt_result.setdefault("tool_call_id", _finish_parsed2.id)
            _preempt_result.setdefault("name", _terminal_name2)
            set_message_meta(_preempt_result, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name=_terminal_name2,
                control_tool_status="preempted",
            ))
            ls.messages.append(_preempt_result)
            # [2026-05-07] 无消息 preempt 也不能持久化未交付的 finish/ask 结果。
            # 原因：任务会带上下文退出，恢复后不应看到已经终止的工具轮。
            # 做法：只把结果留在运行期消息中，并依赖 ephemeral 过滤快照。
            # 目的：恢复后的历史保持待继续执行的状态。
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )
        else:
            action = await _handle_pseudo_tool(ls, _terminal_call, step)
            if action is not None:
                return action

    # 无终止型动作 → 继续下一轮推理
    if pseudo_calls or real_tool_calls:
        ls.use_stream = ls.streaming
    return None


# ---------------------------------------------------------------------------
#  真实工具执行
# ---------------------------------------------------------------------------

async def _execute_real_tools(
    ls: _LoopState, real_tool_calls: list[dict[str, Any]], step: int,
) -> TaskAction | None:
    """批量执行真实工具调用，将结果追加到 messages。"""
    # [AutoC 2026-06-08] Why: a round may contain several large tool outputs. How:
    # keep a shared inline character budget and shrink later tool results once earlier
    # ones have consumed it. Purpose: the combined tool-result messages remain bounded
    # while every truncated result still points to a task-scoped artifact.
    # [AutoC 2026-08-19] Why: per-step inline budget arithmetic now lives in the
    # spill policy plugin. How: keep one mutable state dict that the handler reads
    # to shrink each call's limit and that this loop updates after applying the
    # override (single writer). Purpose: the loop owns one number, the policy owns
    # every decision that produces it.
    _step_inline_state: dict[str, int] = {"used": 0}

    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] 执行 {len(real_tool_calls)} 个工具",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })

    _tool_ctx = ToolContext(
        supervisor_url=ls.rctx.supervisor_url,
        session_id=ls.rctx.session_id,
        run_id=ls.rctx.task_id or ls.run_id or ls.node.id,
        worker_id=ls.rctx.worker_id,
        workspace_root=ls.rctx.workspace_root,
        http=ls.rctx.http,
        registry=ls.registry,
        task_id=ls.rctx.task_id,
        session_generation=ls.rctx.session_generation,
        workspace=getattr(ls.rctx, 'workspace', None),
        workspace_name=getattr(ls.rctx, 'workspace_name', '') or '',
        # [Fork/Merge 2026-05-17] Why: real tools may call supervisor APIs while
        # their node is running on a branch session. How: pass RunContext's parent
        # route session into ToolContext. Purpose: tool events, approvals, and
        # session-scoped built-ins stay attached to the durable user session.
        parent_session_id=getattr(ls.rctx, "parent_session_id", "") or "",
        conversation_key=str((getattr(ls.rctx, "task_context", None) or {}).get("conversation_key", "")).strip(),
        # [AutoC 2026-05-31] Why: the context object is created before iterating
        # individual real tool calls. How: initialize node_id here and set
        # tool_call_id inside each loop iteration below. Purpose: approval requests
        # made by built-in guards can carry the correct active tool identity.
        node_id=ls.node.id,
    )
    # [2026-05-27 refactor] 传递通用节点上下文给插件层。
    # 为什么：插件（如 save_memory）需要读取节点级配置（如 memory_book），
    # 但不应让插件每次重新读取 yaml 文件。
    # 怎么改：传递 node_id 和 node.extra dict，插件零 IO 从 extra 读取业务配置。
    # 目的：引擎核心只提供通用上下文，不知道具体插件字段的存在。
    _tool_ctx._node_id = ls.node.id  # type: ignore[attr-defined]
    _tool_ctx._node_extra = ls.node.extra  # type: ignore[attr-defined]

    _tool_entries: list[dict[str, Any]] = []
    _tool_atts: list[dict[str, Any]] = []
    # Phase 3 Hook System：预构造当前批次的真实工具调用对象。
    # 原因：before_tool_call 的审批类 handler 需要看到“当前工具”和“本轮工具集合”。
    # 做法：把 legacy dict 形状转换为 ParsedToolCall，避免 handler 直接依赖 ai_step 内部字典。
    # 目的：在不改变工具执行结果格式的前提下，为真实工具执行前检查提供统一输入。
    _hook_real_tool_calls = [
        ParsedToolCall(
            id=str(_call.get("id") or ""),
            name=str(_call.get("name") or ""),
            arguments=dict(_call.get("arguments") or {}),
        )
        for _call in real_tool_calls
    ]

    for _rtc in real_tool_calls:
        if await ls.rctx.check_cancelled():
            break
        _t_name = _rtc["name"]
        _t_args = _rtc["arguments"]
        # [AutoC 2026-05-31] Why: one ToolContext instance is reused for the whole
        # batch, but approval guards run during each individual tool execution. How:
        # refresh the active provider tool_call_id before hooks and registry.execute.
        # Purpose: approvals can be merged into the exact ToolCallCard for this call.
        _tool_ctx.tool_call_id = str(_rtc.get("id") or "")
        _tool_ctx.node_id = ls.node.id

        # Phase 3 Hook System：触发单个真实工具的 before_tool_call hook。
        # 原因：审批类 handler 以当前 tool_call 为粒度，不能只看整轮工具列表。
        # 做法：在实际执行工具前构造 HookContext；block/skip 时写入一个 tool_result 保持
        # native 工具调用配对完整。目的：新增 hook 不破坏后续 LLM 消息格式。
        _current_tool_call = ParsedToolCall(
            id=str(_rtc.get("id") or ""),
            name=str(_t_name),
            arguments=dict(_t_args or {}),
        )
        _tool_hook_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            tool_calls=_hook_real_tool_calls,
            extra={"real_tool_calls": real_tool_calls},
        )
        _tool_hook_result = await hook_registry.afire("before_tool_call", _tool_hook_ctx)
        if _tool_hook_result.action is not None:
            return _tool_hook_result.action
        if _tool_hook_result.block or _tool_hook_result.skip_step:
            _blocked_msg = (
                _tool_hook_result.error_message
                or _tool_hook_result.reason
                or "Tool call blocked by before_tool_call hook."
            )
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": "text",
                "raw_inline": _blocked_msg,
                "truncated": False,
                "ref": "",
                "summary": _blocked_msg[:200],
            })
            continue

        # [2026-04-23] 异步工具分流：查询 spec 判断该工具是否为 async_mode。
        # 若是，则在后台 asyncio.Task 中执行，不阻塞当前推理循环，
        # 结果通过 preempt API 异步回传。从 commit 7d10197 恢复。
        _spec = ls.registry.get_spec(_t_name)
        _is_async = _spec.get("async_mode", False) if _spec else False

        if _is_async:
            # [WS tool result fields 2026-05-19] Why: tool_call_end now exposes
            # elapsed_ms for both synchronous and async-started tools. How: capture
            # a monotonic timestamp before the lifecycle start event is emitted.
            # Purpose: downstream WebSocket consumers can show one consistent
            # duration field without depending on SignalBus internals.
            _tool_t0 = time.monotonic()
            # [WS tool events 2026-05-17] Why: WebSocket clients need structured
            # tool lifecycle events in the durable EventLog, not only localized
            # handoff_progress text. How: emit a non-transient start event before
            # the async tool is scheduled. Purpose: reconnecting clients can replay
            # the tool start through the existing EventLog catch-up path.
            await ls.rctx.emit_event("tool_call_start", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "arguments": _t_args,
            })
            _cleanup_async_tracker()
            _async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[_async_id] = {
                "tool_name": _t_name,
                "status": "running",
                "started_at": _tool_t0,
                "task_id": ls.rctx.task_id,
            }
            # [AutoC 2026-06-27] Why: the loop reuses _tool_ctx and rewrites
            # tool_call_id for later calls. How: pass a snapshot to the background
            # async task. Purpose: callback artifacts and approvals keep the current
            # tool call identity.
            _async_tool_ctx = _snapshot_tool_context(_tool_ctx)
            asyncio.create_task(
                _run_async_tool(
                    registry=ls.registry,
                    http=ls.rctx.http,
                    supervisor_url=ls.rctx.supervisor_url,
                    task_id=ls.rctx.task_id,
                    # [Fork/Merge 2026-05-12] Route async callbacks through the parent session.
                    # Why: ls.rctx.session_id may be an entry branch used only for runtime history.
                    # How: prefer parent_session_id and fall back to session_id for old tasks.
                    # Purpose: async tool results create follow-up inbound messages in the SDK-visible session.
                    session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                    tool_name=_t_name,
                    tool_args=_t_args,
                    tool_ctx=_async_tool_ctx,
                    async_tool_id=_async_id,
                    runtime_cfg=ls.runtime_cfg,
                    step=step,
                    index=len(_tool_entries),
                    tool_call_id=str(_rtc.get("id") or _async_id),
                ),
                name=f"async_tool_{_t_name}_{_async_id}",
            )
            _async_summary = f"异步执行已启动 (id: {_async_id})，结果将通过 preempt 自动回传"
            # [WS tool result fields 2026-05-19] Why: async-started calls have no
            # final tool result yet, but clients still need the same result schema.
            # How: define the same local variables used by the synchronous branch,
            # with result=None and the immediate placeholder text as raw_inline.
            # Purpose: tool_call_end consumers can parse sync and async lifecycle
            # events without special-casing missing keys.
            _t_result = None
            _t_fmt = "text"
            _t_raw_inline = f'\u23f3 Async tool "{_t_name}" started (id: {_async_id}). Result will be delivered via preempt when ready.'
            _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1) if "_tool_t0" in dir() else None
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": _t_fmt,
                "raw_inline": _t_raw_inline,
                "truncated": False,
                "ref": "",
                "summary": _async_summary,
                "elapsed_ms": _t_elapsed_ms,
                "attachments": [],
            })
            # [WS tool events 2026-05-17] Why: async tools return control before
            # the real result exists, so clients still need a lifecycle closure for
            # this immediate call. How: emit tool_call_end with async_started rather
            # than success or error. Purpose: UIs can show that the background task
            # was accepted while waiting for the later preempt-delivered result.
            await ls.rctx.emit_event("tool_call_end", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "status": "async_started",
                "summary": _async_summary,
                "result": _t_result,
                "raw_inline": _t_raw_inline,
                "format": _t_fmt,
                "elapsed_ms": _t_elapsed_ms,
            })
            await ls.rctx.emit_event("handoff_progress", {
                "message": f"[{ls.node.id}] {_t_name}: 异步执行已启动",
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            continue

        # ---- 同步工具：阻塞等待执行完成（原有逻辑）----
        # [WS tool result fields 2026-05-19] Why: tool_call_end should include the
        # tool execution duration. How: capture a monotonic start timestamp before
        # emitting the structured start event and running the registry call. Purpose:
        # expose elapsed_ms without changing SignalBus span behavior.
        _tool_t0 = time.monotonic()
        # [WS tool events 2026-05-17] Why: handoff_progress remains for legacy
        # consumers, but the web UI needs structured lifecycle data. How: emit a
        # durable tool_call_start immediately before the SignalBus span. Purpose:
        # the EventLog can replay the exact tool name, call id, and arguments.
        await ls.rctx.emit_event("tool_call_start", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "arguments": _t_args,
        })
        # Phase 2 Signal: tool.call span 包裹每个工具的执行过程。
        # 自动发射 tool.call.start（含工具名和参数摘要）和 tool.call.end（含 elapsed_ms 和 error）。
        # span 是同步 contextmanager，在 async 函数中直接 with 即可。
        _adaptive_threshold = _execute_command_async_upgrade_threshold(_t_name, _t_args, ls.runtime_cfg)
        if _adaptive_threshold is not None:
            # [AutoC 2026-06-27] Why: execute_command may run longer than the model
            # should wait, but its own timeout_sec must remain the hard kill limit.
            # How: start the normal registry execution with a ToolContext snapshot,
            # wait only until the effective threshold, then deliver the same task in
            # the background if it is still pending. Purpose: the model receives an
            # immediate tool result placeholder while the subprocess keeps running.
            _exec_tool_ctx = _snapshot_tool_context(_tool_ctx)
            _exec_task = asyncio.create_task(
                _execute_registry_tool_with_span(ls.registry, _t_name, _t_args, _exec_tool_ctx),
                name=f"execute_command_adaptive_{str(_rtc.get('id') or '')[:24] or 'call'}",
            )
            _done, _pending = await asyncio.wait({_exec_task}, timeout=float(_adaptive_threshold))
            if _exec_task not in _done:
                _cleanup_async_tracker()
                _async_id = uuid.uuid4().hex[:8]
                _async_tool_tasks[_async_id] = {
                    "tool_name": _t_name,
                    "status": "running",
                    "started_at": _tool_t0,
                    "task_id": ls.rctx.task_id,
                    "upgraded_from": "sync_timeout",
                }
                asyncio.create_task(
                    _deliver_started_async_task(
                        _exec_task,
                        registry=ls.registry,
                        http=ls.rctx.http,
                        supervisor_url=ls.rctx.supervisor_url,
                        task_id=ls.rctx.task_id,
                        session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                        tool_name=_t_name,
                        tool_args=_t_args,
                        tool_ctx=_exec_tool_ctx,
                        async_tool_id=_async_id,
                        started_at=_tool_t0,
                        runtime_cfg=ls.runtime_cfg,
                        step=step,
                        index=len(_tool_entries),
                        tool_call_id=str(_rtc.get("id") or _async_id),
                    ),
                    name=f"async_upgrade_{_t_name}_{_async_id}",
                )
                _async_summary = f"执行超过 {_adaptive_threshold:.1f}s，已自动转为异步 (id: {_async_id})，结果将通过 preempt 自动回传"
                _t_result = None
                _t_fmt = "text"
                _t_raw_inline = (
                    f'⏳ Tool "{_t_name}" exceeded {_adaptive_threshold:.1f}s and was '
                    f'auto-upgraded to async (id: {_async_id}). Result will be delivered via preempt when ready.'
                )
                _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1) if "_tool_t0" in dir() else None
                _tool_entries.append({
                    "id": _rtc.get("id", ""),
                    "name": _t_name,
                    "args": _t_args,
                    "format": _t_fmt,
                    "raw_inline": _t_raw_inline,
                    "truncated": False,
                    "ref": "",
                    "summary": _async_summary,
                    "elapsed_ms": _t_elapsed_ms,
                    "attachments": [],
                })
                await ls.rctx.emit_event("tool_call_end", {
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                    "tool_call_id": _rtc.get("id", ""),
                    "tool_name": _t_name,
                    "status": "async_started",
                    "summary": _async_summary,
                    "result": _t_result,
                    "raw_inline": _t_raw_inline,
                    "format": _t_fmt,
                    "elapsed_ms": _t_elapsed_ms,
                })
                await ls.rctx.emit_event("handoff_progress", {
                    "message": f"[{ls.node.id}] {_t_name}: 已自动转为异步执行",
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                })
                continue
            _t_result = _exec_task.result()
        else:
            _t_result = await _execute_registry_tool_with_span(ls.registry, _t_name, _t_args, _tool_ctx)
        # [硬取消-场景1] 工具返回 cancelled 时，仍将结果存入 _tool_entries 再 break。
        # 确保 assistant 的 tool_use 有对应 tool_result 配对，
        # 模型下次看到的是「我调了工具但被用户取消了」而非 tool_use 悬空无响应。
        _t_cancelled = isinstance(_t_result, dict) and _t_result.get("cancelled")

        # [summary-args 2026-05-19] Why: handoff_progress keeps the legacy
        # "[node] tool: summary" format, so argument detail must come from the
        # summary itself. How: pass the parsed tool arguments alongside the result.
        # Purpose: show commands, queries, and target paths without changing the
        # event payload shape.
        _t_summary = summarize_result(_t_name, _t_result, args=_t_args)
        # [AutoC 2026-05-31] Why: structural result routing also supports an
        # optional spec-level result_format override. How: read the spec from the
        # active registry immediately after execution and pass it to result_to_raw.
        # Purpose: keep external and built-in synchronous tool results formatted by
        # metadata or by structure without hard-coded tool-name checks.
        _t_spec = ls.registry.get_spec(_t_name)
        _t_fmt, _t_raw = result_to_raw(_t_name, _t_result, tool_spec=_t_spec)
        _t_raw_inline = _t_raw
        _t_truncated = False
        _t_ref = ""

        # [AutoC 2026-08-19] Why: how large a tool result may enter the prompt is
        # policy, not loop mechanics. How: fire after_tool_call once per synchronous
        # call — the spill policy handler bounds the inline text (per-tool limit,
        # per-step budget, artifact spill) and the attachment collector normalizes
        # attachments in the same chain — then apply the merged result_override.
        # Purpose: the bounding strategy lives in a plugin and can be replaced or
        # unloaded without touching the inference loop.
        _tool_atts_start = len(_tool_atts)
        _after_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            tool_calls=_hook_real_tool_calls,
            extra={
                "loop_state": ls,
                "tool_name": _t_name,
                "tool_args": _t_args,
                "tool_result": _t_result,
                "raw_inline": _t_raw,
                "tool_attachments": _tool_atts,
                "step_inline_state": _step_inline_state,
                "call_index": len(_tool_entries),
            },
        )
        _after_result = await hook_registry.afire("after_tool_call", _after_ctx)
        if _after_result.action is not None:
            return _after_result.action
        if isinstance(_after_result.result_override, dict):
            _ov = _after_result.result_override
            if "raw_inline" in _ov:
                _t_raw_inline = _ov["raw_inline"]
            _t_truncated = bool(_ov.get("truncated", _t_truncated))
            _t_ref = str(_ov.get("ref") or "")
        elif isinstance(_t_raw, str):
            # 兑底：spill 策略插件未干预（未加载或被卸载）时保留最小逐工具截断，
            # 防止超大结果无界进入 prompt。
            _fallback_limit = get_tool_inline_limit(_t_name, ls.runtime_cfg)
            _t_raw_inline, _t_truncated = truncate_tool_result(
                _t_name, _t_raw, _fallback_limit, "", config=ls.runtime_cfg,
            )
        if isinstance(_t_raw_inline, str):
            _step_inline_state["used"] = _step_inline_state.get("used", 0) + len(_t_raw_inline)

        _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1) if "_tool_t0" in dir() else None
        _tool_entries.append({
            "id": _rtc.get("id", ""),
            "name": _t_name,
            "args": _t_args,
            "format": _t_fmt,
            "raw_inline": _t_raw_inline,
            "truncated": _t_truncated,
            "ref": _t_ref,
            "summary": _t_summary,
            "elapsed_ms": _t_elapsed_ms,
            "attachments": list(_tool_atts[_tool_atts_start:]),
            # [AutoC 2026-06-15] Why: shadow write only persists raw_inline text,
            # losing structured fields (appliedCount, returncode, etc.) that the
            # frontend needs for result suffix rendering. How: carry the original
            # tool result object so shadow write can store it in message meta.
            # Purpose: history reconstruction renders the same result suffixes as
            # live WebSocket events.
            "result": _t_result,
        })

        # [WS tool events 2026-05-17] Why: clients should receive a structured
        # completion event even when a tool reports an error dict or cancellation
        # stops later progress messages. How: derive status from the tool result's
        # error field and emit before any legacy handoff_progress path. Purpose:
        # reconnecting clients can reconstruct completed tool calls from EventLog.
        await ls.rctx.emit_event("tool_call_end", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "status": "cancelled" if _t_cancelled else ("error" if (isinstance(_t_result, dict) and _t_result.get("error")) else "success"),
            "summary": _t_summary,
            # [WS tool result fields 2026-05-19] Why: SDKs and adapters need the
            # complete returned object, not only a short summary. How: carry the
            # original result plus the same formatted inline representation that is
            # appended to the model transcript. Purpose: leave truncation decisions
            # to consuming adapters while preserving the raw engine result here.
            "result": _t_result,
            "raw_inline": _t_raw_inline,
            "format": _t_fmt,
            "elapsed_ms": _t_elapsed_ms,
        })

        # [AutoC 2026-08-10] Propagate workspace changes from set_workspace tool
        # back to RunContext so subsequent LLM rounds inherit the new cwd.
        if _tool_ctx.workspace is not None and _tool_ctx.workspace != getattr(ls.rctx, 'workspace', None):
            ls.rctx.workspace = _tool_ctx.workspace
        if _tool_ctx.workspace_name and _tool_ctx.workspace_name != getattr(ls.rctx, 'workspace_name', ''):
            ls.rctx.workspace_name = _tool_ctx.workspace_name

        # [硬取消-场景1] 已取消的工具结果已存入 entries（上方 append），附件收集已在
        # after_tool_call 同点完成，此处仅跳过进度事件并退出循环。未执行的后续工具被
        # 跳过（循环顶部 check_cancelled），不产生 tool_result。
        if _t_cancelled:
            break

        await ls.rctx.emit_event("handoff_progress", {
            "message": f"[{ls.node.id}] {_t_name}: {_t_summary}",
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        })

    if _tool_entries:
        for _entry in _tool_entries:
            _result_body = _entry["raw_inline"]
            # [AutoC 2026-06-08] Why: truncation now happens immediately after
            # result_to_raw so both WebSocket metadata and ConversationStore receive
            # the same bounded content. How: this formatter stage only forwards the
            # prepared body. Purpose: remove the old hard-coded 32k loss point.
            # [2026-05-01] 真实工具结果统一走 formatter.format_tool_result。
            # 原因：真 native 需要 role=tool + tool_call_id，而旧代码在这里手写 user 文本，
            # 会绕过新 NativeToolFormatter。fake-native/json 仍由各自 formatter 生成旧文本。
            _tool_msg = ls.formatter.format_tool_result(
                ParsedToolCall(
                    id=str(_entry.get("id") or ""),
                    name=str(_entry["name"]),
                    arguments=dict(_entry.get("args") or {}),
                ),
                _result_body,
            )
            set_message_meta(_tool_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
            ))
            # [AutoC 2026-06-15] Why: ConversationStore only persists content as
            # text (raw_inline), but the frontend result suffix logic (getDataField)
            # needs the original structured dict with fields like appliedCount,
            # returncode, totalFiles, etc. How: store the original tool result in
            # _meta so session_history_structured can expose it. Purpose: refreshed
            # pages render the same "→ 3/3 通过 +10 -1" suffixes as live events.
            _tool_meta = _tool_msg.setdefault("_meta", {})
            _structured = _entry.get("result")
            if isinstance(_structured, dict):
                _tool_meta["tool_result_structured"] = _structured
            _tool_meta["tool_result_raw_inline"] = str(_entry.get("raw_inline") or "")
            _tool_meta["tool_result_format"] = str(_entry.get("format") or "")
            _tool_meta["tool_result_summary"] = str(_entry.get("summary") or "")
            if _entry.get("elapsed_ms") is not None:
                _tool_meta["tool_result_elapsed_ms"] = _entry.get("elapsed_ms")
            _entry_attachments = _entry.get("attachments")
            if isinstance(_entry_attachments, list):
                _tool_meta["attachments"] = list(_entry_attachments)
                _tool_meta["tool_result_attachments"] = list(_entry_attachments)
            ls.messages.append(_tool_msg)
            # Phase 1: 影子写入 tool_result 消息到 ConversationStore
            _shadow_write(ls, _tool_msg, MessageType.TOOL_RESULT)
        if _tool_atts:
            # [AutoC 2026-06-01] Why: tool-generated image attachments were only
            # appended to runtime memory, so a later task could not reload them
            # from ConversationStore. How: keep the exact multimodal message in
            # a variable, append it, then shadow-write it. Purpose: persist tool
            # image results across task boundaries without changing prompt text.
            _tool_att_msg = {
                "role": "user",
                "content": build_multimodal_content(
                    "以上工具执行产生了以下图片结果：", _tool_atts, workspace_root=ls.rctx.workspace_root,
                ),
                "_meta": {"attachments": list(_tool_atts)},
            }
            ls.messages.append(_tool_att_msg)
            _shadow_write(ls, _tool_att_msg, message_type="tool_result_attachment")

    return None


# ---------------------------------------------------------------------------
#  纯文本响应处理
# ---------------------------------------------------------------------------

def _handle_plaintext_response(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理纯文本响应（无 tool_calls）。"""
    text = (resp.text or "").strip()
    if not text:
        return None

    if ls.preempt_after_step:
        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
        )

    # ---- hybrid 模式：纯文本视为隐式 finish，直接投递给用户 ----
    # 不 reject、不重试，将裸文本包装为 ACTION_FINISH 返回。
    # result 中标记 implicit_finish=True，供事件日志/管理界面区分显式与隐式 finish。
    # 参见 RFC: data/rfc_hybrid_output_mode.md
    if getattr(ls.node, 'output_mode', 'tool_only') == 'hybrid':
        # 写入 assistant 消息到对话历史 + ConversationStore，与 _handle_tool_calls 对齐
        _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
        # [refactor 2026-04-18] 与 _handle_tool_calls 对齐：动态 provider 名、metadata/reasoning 新字段
        _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
        _implicit_meta = MessageMeta(
            provider=_provider_name,
            tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
            message_type="assistant",
            timestamp=datetime.now(timezone.utc).isoformat(),
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
            metadata={_provider_name: resp.provider_meta} if getattr(resp, "provider_meta", None) else {},
            tool_call_ids=[],
            reasoning=getattr(resp, "reasoning", "") or "",
            has_reasoning=bool(getattr(resp, "reasoning", "") or ""),
            inline_data=getattr(resp, "inline_data", None) or [],
            usage=dict(ls.last_usage) if ls.last_usage else {},
            reasoning_started_at=getattr(ls, '_reasoning_started_iso', '') or '',
            reasoning_ended_at=getattr(ls, '_reasoning_ended_iso', '') or '',
        )
        set_message_meta(_assistant_msg, _implicit_meta)
        ls.messages.append(_assistant_msg)
        _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": text,
                "attachments": [],  # [AutoC 2026-08-06] 隐式 finish 不自动带附件
                "implicit_finish": True,
            },
            context_ref=ctx_ref,
            summary=_short(text, 240),
        )

    # ---- tool_only 模式：现有行为，reject 纯文本并重试 ----
    ls.plaintext_retry_count += 1
    if ls.plaintext_retry_count <= ls.plaintext_retry_max:
        _retry_hint = ls.formatter.build_retry_hint()
        ls.messages.append({
            "role": "user",
            "content": _retry_hint,
            "_retry_hint": True,
        })
        ls.use_stream = ls.streaming
        return None

    # 重试耗尽后：返回 FAIL 而非 FINISH
    # 引擎内核不认可裸正文作为合法结束，只有 finish 工具才能产生 ACTION_FINISH。
    # 将原先的 ACTION_FINISH 改为 ACTION_FAIL，error 中附带截断原始文本用于调试。
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=f"模型未使用 finish 工具，裸文本不被内核认可为合法结束。原始文本: {_short(text, 200)}",
        context_ref=ctx_ref,
        summary="plaintext_without_finish",
    )


# ---------------------------------------------------------------------------
#  AI 节点主执行函数
# ---------------------------------------------------------------------------

async def _fire_task_end_hook_if_finish(ls: _LoopState, action: TaskAction, step_count: int) -> TaskAction:
    """Fire on_task_end for successful finish/ask actions and keep the action updated.

    Why: most normal AI-node exits are produced inside finish, ask, or hybrid
    plaintext branches before run_ai_node reaches its outer max_steps fallback.
    How: route ACTION_FINISH and ACTION_ASK through the registered on_task_end
    handlers and copy the snapshot context_ref back when the handler reports that
    persistence ran. Purpose: connect ContextSnapshotSaver to safe normal-end
    paths without changing dispatch, fail, cancel, or preempt terminal semantics.
    """
    if action.action not in (ACTION_FINISH, ACTION_ASK):
        return action

    # Phase 3 Hook System：普通完成路径也触发 on_task_end。
    # 原因：finish/ask 可能从多个内部 helper 提前返回，外层没有统一的“成功结束”落点。
    # 做法：只在 ACTION_FINISH/ACTION_ASK 返回前构造 HookContext，并传入 loop_state 与正确步数。
    # 目的：先覆盖低风险成功路径，后续再逐步迁移 fail/preempt/dispatch 的快照保存。
    _end_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step_count,
        extra={"loop_state": ls, "step_count": step_count, "task_action": action},
    )
    _end_result = await hook_registry.afire("on_task_end", _end_ctx)
    if _end_result.action is not None:
        return _end_result.action
    if _end_ctx.extra.get("snapshot_saved"):
        action.context_ref = str(_end_ctx.extra.get("context_ref") or "")
    return action


async def run_ai_node(
    *,
    rctx: "RunContext",
    streaming: bool = False,
    # [provider-registry 2026-05-03] 推理循环只依赖 BaseProvider 接口。
    # 原因：provider 由 registry 创建后不一定是 OpenAI；做法：类型标注改为 BaseProvider；
    # 目的：删除不必要的具体 OpenAI 类型引用。
    provider: BaseProvider,
    registry: ToolRegistry,
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    run_id: str = "",
    context_ref: str = "",
    resume_data: dict[str, Any] | None = None,
    downstream_info: list[dict[str, str]] | None = None,
    switch_info: list[dict[str, str]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> TaskAction:
    # Phase 3 Hook System：每次进入 AI 节点都注册内置 handler。
    # 原因：内置 handler 已统一迁入 engine.builtin，并通过 PLUGIN_META 声明
    # 自己的 hook point。做法：自动扫描内置目录并注册到共享 hook_registry；
    # HookRegistry 会按名称替换旧实例。目的：删除集中硬编码注册，同时保持
    # finish_guard、approval、prompt 注入等内置规则始终可用。
    # Why: PLUGIN_META can now also declare builtin tools. How: pass the active
    # ToolRegistry into discovery so plugin-owned tools are registered before the
    # model-visible tool list is built. Purpose: keep hook and tool registration
    # in one plugin discovery pass.
    from ..context import EngineContext
    from providers import registry as _provider_registry
    from engine.faces.prompt_sections import prompt_section_registry as _prompt_sections

    _engine_ctx = EngineContext(
        providers=_provider_registry,
        hooks=hook_registry,
    )
    # Why: declarative faces (prompt sections, tools) live behind
    # ctx.contributions, mounted by name. How: mount before plugin discovery so
    # the shared disposal ledger is wired into each face before plugins register
    # on them. Purpose: plugin registrations on any surface are attributed and
    # unloadable through one ledger.
    _engine_ctx.contributions.mount("prompt_sections", _prompt_sections)
    _engine_ctx.contributions.mount("tools", registry)
    auto_discover_and_register(hook_registry, tool_registry=registry, context=_engine_ctx)
    # Phase 3 External Hook Plugins：每次进入 AI 节点时扫描工作区 plugins/。
    # 原因：用户需要在不修改 engine 源码的情况下添加自定义 handler。
    # 做法：调用幂等的外部插件加载器；HookRegistry 会按 handler.name 替换旧实例。
    # 目的：启动时自动发现插件，同时避免重复注册和单个插件失败影响引擎启动。
    load_external_plugins(hook_registry, rctx.workspace_root / "plugins", context=_engine_ctx)

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)

    # ---- 收集附件 ----
    collected_attachments: list[dict[str, Any]] = []
    _tool_produced_attachments: list[dict[str, Any]] = []
    if attachments:
        collected_attachments.extend(attachments)
    if resume_data and isinstance(resume_data, dict):
        for e in (resume_data.get("tool_results") or resume_data.get("entries") or []):
            if isinstance(e, dict):
                # [AutoC 2026-05-31] Why: resumed tool entries may carry either the
                # old top-level attachments list or the new data.attachments list.
                # How: inspect the nested data dict before the legacy field.
                # Purpose: avoid dropping generated files when resuming sessions.
                e_data = e.get("data") if isinstance(e.get("data"), dict) else {}
                e_atts = e_data.get("attachments") if isinstance(e_data.get("attachments"), list) else e.get("attachments")
                if isinstance(e_atts, list):
                    collected_attachments.extend(e_atts)
        if isinstance(resume_data.get("attachments"), list):
            collected_attachments.extend(resume_data["attachments"])
        rd = resume_data.get("result")
        if isinstance(rd, dict):
            # [AutoC 2026-05-31] Why: final result payloads can also be migrated to
            # data.attachments. How: prefer nested attachments and fall back to the
            # old result.attachments list. Purpose: keep finish-time attachments
            # selectable after a resume.
            rd_data = rd.get("data") if isinstance(rd.get("data"), dict) else {}
            rd_atts = rd_data.get("attachments") if isinstance(rd_data.get("attachments"), list) else rd.get("attachments")
            if isinstance(rd_atts, list):
                collected_attachments.extend(rd_atts)

    # ---- 恢复或新建消息历史 ----
    step_count = 0
    _is_block_mode = False
    system_prompt: list[dict[str, Any]] = []
    # Phase 3 Hook System：初始组装只生成不含知识注入的 prompt 骨架。
    # Why: inference core should know only the hook point, not concrete knowledge
    # injection handlers. How: always fire before_prompt_build after fresh assembly
    # and let registered handlers rebuild messages in place. Purpose: keep prompt
    # ownership behind hooks while preserving the final prompt layout.
    _assembled_fresh = False
    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if snapshot and isinstance(snapshot.get("messages"), list):
        messages = list(snapshot.get("messages") or [])
        try:
            step_count = int(snapshot.get("step_count") or 0)
        except Exception:
            step_count = 0
    else:
        messages, _is_block_mode, system_prompt = assemble_initial_messages(
            workspace_root=rctx.workspace_root,
            runtime_cfg=runtime_cfg,
            node=node,
            instruction=instruction,
            history=history,
            task_context=rctx.task_context,
            session_id=rctx.session_id,
            attachments=attachments,
            workspace_name=getattr(rctx, 'workspace_name', '') or '',
        )
        _assembled_fresh = True

    if _assembled_fresh:
        # Phase 3 Hook System：初始 messages 完成后始终触发 before_prompt_build。
        # Why: knowledge injection is now declarative (prompt sections rendered
        # during assembly), so this hook only serves procedural handlers. How:
        # pass the rendered system prompt, history, instruction, and attachments
        # through HookContext.extra. Purpose: keep the interception point while
        # prompt content comes from registered sections.
        _prompt_ctx = HookContext(
            messages=messages,
            tools=[],
            node=node,
            provider=provider,
            rctx=rctx,
            step=step_count,
            extra={
                "runtime_cfg": runtime_cfg,
                "instruction_text": instruction,
                "history": history,
                "attachments": attachments,
                "system_prompt": system_prompt,
            },
        )
        _prompt_result = await hook_registry.afire("before_prompt_build", _prompt_ctx)
        if _prompt_result.action is not None:
            return _prompt_result.action

    # ---- 追加恢复消息 ----
    formatter = create_tool_formatter(node.tool_mode)
    if resume_data:
        messages.extend(_build_resume_messages(resume_data))
        # [AutoC 2026-06-10] output_rejected: inject reject as a properly formatted
        # finish tool result through the node's formatter pipeline.
        if str(resume_data.get("type") or "") == "output_rejected":
            _rej_result = resume_data.get("result") if isinstance(resume_data.get("result"), dict) else {}
            _rej_from = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
            _rej_reason = str(resume_data.get("reject_reason") or _rej_result.get("text") or "").strip()
            _rej_line = f"reject by {_rej_from}: {_rej_reason}" if _rej_from else f"reject: {_rej_reason}"
            _finish_call_id = str(resume_data.get("_finish_tool_call_id") or "").strip()
            _finish_result_idx = -1

            # [AutoC 2026-08-06] 判断是否为隐式 finish（hybrid free prose）。
            # hybrid 模式下 tool_call_log 可能残留被 guard reject 的 finish 调用 id，
            # 但实际 JSONL/messages 里没有 finish tool_call，不能走显式分支。
            # 检测方式：在 messages 里找不到对应的 finish assistant tool_call。
            _is_implicit_finish = True
            if _finish_call_id:
                for _m in reversed(messages):
                    if _m.get("role") != "assistant":
                        continue
                    for _tc in (_m.get("tool_calls") or []):
                        if not isinstance(_tc, dict):
                            continue
                        _fn = _tc.get("function") if isinstance(_tc.get("function"), dict) else {}
                        _tc_id = str(_tc.get("id") or "").strip()
                        _tc_name = str(_tc.get("name") or _fn.get("name") or "")
                        if _tc_name == "finish" and _tc_id == _finish_call_id:
                            _is_implicit_finish = False
                            break
                    if not _is_implicit_finish:
                        break

            logger.info("output_rejected: from=%s reason=%s fcid=%r implicit=%s msg_count=%d",
                     _rej_from, _rej_reason[:80], _finish_call_id, _is_implicit_finish, len(messages))

            if _is_implicit_finish:
                # Hybrid 模式隐式 finish 或无 finish call_id：
                # 以 user 角色注入 reject，追加到 messages 末尾。
                # reject 作为当前指令应该在动态上下文（背景信息）之后。
                _rej_user_msg = {"role": "user", "content": _rej_line}
                set_message_meta(_rej_user_msg, MessageMeta(
                    message_type="output_rejected",
                ))
                messages.append(_rej_user_msg)
                logger.info("output_rejected(implicit): appended user reject msg at idx=%d",
                            len(messages) - 1)
                _store = getattr(rctx, 'conversation_store', None)
                _target_sid = getattr(rctx, 'child_session_id', '') or rctx.session_id
                if _store:
                    try:
                        from engine.conversation_store import Message as _StoreMsg
                        from datetime import datetime, timezone
                        from uuid import uuid4
                        _store.append(_target_sid, _StoreMsg(
                            id=str(uuid4()),
                            role="user",
                            content=_rej_line,
                            message_type="output_rejected",
                            created_at=datetime.now(timezone.utc).isoformat(),
                        ))
                    except Exception as _wr_exc:
                        logger.warning("output_rejected(implicit): append failed sid=%s: %s",
                                    _target_sid, _wr_exc)
            else:
                # 显式 finish：找到对应的 tool_result 并替换内容。
                if _finish_call_id:
                    for _idx in range(len(messages) - 1, -1, -1):
                        _m = messages[_idx]
                        if _m.get("role") != "tool":
                            continue
                        if str(_m.get("tool_call_id") or "").strip() != _finish_call_id:
                            continue
                        if str(_m.get("name") or "").strip() != "finish":
                            continue
                        _finish_result_idx = _idx
                        break
                _rej_parsed = ParsedToolCall(
                    id=_finish_call_id,
                    name="finish",
                    arguments={},
                )
                _rej_msg = formatter.format_tool_result(_rej_parsed, _rej_line)
                set_message_meta(_rej_msg, MessageMeta(
                    tool_mode=getattr(node, 'tool_mode', 'fake-native'),
                    message_type="tool_result",
                ))
                if _finish_result_idx >= 0:
                    _old_msg_id = str(messages[_finish_result_idx].get("id") or "").strip()
                    messages[_finish_result_idx] = _rej_msg
                    logger.info("output_rejected(explicit): replaced tool_result at idx=%d fcid=%s",
                             _finish_result_idx, _finish_call_id)
                    _store = getattr(rctx, 'conversation_store', None)
                    _target_sid = getattr(rctx, 'child_session_id', '') or rctx.session_id
                    if _old_msg_id and _store:
                        try:
                            _store.update_message(_target_sid, _old_msg_id, _rej_msg)
                        except Exception as _upd_exc:
                            logger.warning("output_rejected: update_message failed sid=%s msg=%s: %s",
                                        _target_sid, _old_msg_id, _upd_exc)
                    elif not _old_msg_id:
                        logger.warning("output_rejected: msg id missing, cannot persist reject to JSONL")
                else:
                    messages.append(_rej_msg)
                    logger.info("output_rejected(explicit): appended orphan tool_result fcid=%s",
                             _finish_call_id)
        if str(resume_data.get("type") or "") == "compact_done":
            # [2026-04-24] P1.5 熔断器：压缩成功时重置失败计数
            # [AutoC 2026-05-13] Why: compaction may have targeted the parent
            # session while the task resumed on a branch. How: reset the breaker
            # on parent_session_id when present. Purpose: success accounting stays
            # consistent with parent-first compact targeting.
            record_compact_success(rctx.parent_session_id or rctx.session_id)
            # Phase 2 Signal: compact.done 信号，通过 SignalBus 发射供监控使用
            _cd_payload = {
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            }
            # task 粒度信息（ConvStore 路径产生）
            for _k in ("total_segments", "kept_segments", "compressed_segments"):
                if _k in resume_data:
                    _cd_payload[_k] = resume_data[_k]
            get_bus().emit(Signal(name="compact.done", payload=_cd_payload))
            await rctx.emit_event("compact_done", _cd_payload)

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    _allowed_real_tools = {s.get("name") for s in tool_specs if s.get("name")}
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        # [2026-05-04] Register one dynamic dispatch tool per delegate target.
        # Why: target selection should happen through tool choice, not through an
        # aggregate dispatch schema. How: expand node.delegate_targets into only
        # dispatch:{target_id} specs. Purpose: keep dynamic dispatch intact while
        # removing the old aggregate dispatch tools from the model-visible list.
        openai_tools.extend(_dispatch_delegate_specs(delegate_targets, downstream_info))

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
    _is_system_task = bool((rctx.task_context or {}).get("is_system_task"))
    if not _is_system_task:
        _sw_targets = [info["id"] for info in (switch_info or [])]
        openai_tools.append(_switch_node_spec(_sw_targets, switch_info, current_node_id=node.id, current_node_name=node.name))

    # [AutoC 2026-08-06] hybrid 模式下不注入 finish 工具：free prose 即隐式 finish，
    # 模型不需要也不应该看到 finish 工具。tool_only 模式下保留原行为。
    _output_mode = getattr(node, 'output_mode', 'hybrid')
    if _output_mode == 'tool_only':
        openai_tools.append(_finish_spec())
    # [AutoC 2026-05-31] ask 在所有模式下都可用：节点需要向上游请求信息时使用。
    openai_tools.append(_ask_spec())
    openai_tools.append(_reply_spec())
    openai_tools.append(_compact_context_spec())
    openai_tools.append(_preempt_task_spec())

    # ---- 工具定义注入（formatter 统一处理 native/json 差异）----
    if openai_tools:
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"], _api_tools = formatter.inject_tool_definitions(
                    openai_tools, msg.get("content", ""),
                )
                openai_tools = _api_tools or []
                break

    # ---- 构造循环状态 ----
    ls = _LoopState(
        rctx=rctx,
        node=node,
        provider=provider,
        registry=registry,
        run_id=run_id,
        context_ref=context_ref,
        runtime_cfg=runtime_cfg,
        streaming=streaming,
        messages=messages,
        system_prompt=system_prompt,
        is_block_mode=_is_block_mode,
        openai_tools=openai_tools,
        history=history,
        collected_attachments=collected_attachments,
        tool_produced_attachments=_tool_produced_attachments,
        formatter=formatter,
        allowed_real_tools=_allowed_real_tools,
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
        compact_keep_recent=get_int(runtime_cfg, "engine.compact.keep_recent", 6, min_value=2, max_value=50),
        compacted=False,
        last_prompt_tokens=None,
        retry_max=get_int(runtime_cfg, "engine.retry.max_retries", 3, min_value=0, max_value=10),
        retry_initial_delay=get_float(runtime_cfg, "engine.retry.initial_delay_sec", 1.0, min_value=0.1, max_value=60.0),
        retry_max_delay=get_float(runtime_cfg, "engine.retry.max_delay_sec", 30.0, min_value=1.0, max_value=300.0),
        retry_backoff=get_float(runtime_cfg, "engine.retry.backoff_multiplier", 2.0, min_value=1.0, max_value=10.0),
        plaintext_retry_count=0,
        # 改动：plaintext retry 默认值从 2 → 3，与 retry_max（LLM 报错重试）对齐，
        # 给模型更多机会自行修正未调 finish 的问题。
        plaintext_retry_max=get_int(runtime_cfg, "engine.plaintext_retry_max", 3, min_value=0, max_value=10),
        preempt_after_step=False,
        preempt_inject_info=None,
        use_stream=streaming,
    )

    # ---- 推理循环 ----
    for step in range(step_count, max_steps):
        # Phase 3 Hook System：循环顶部统一触发 before_step。
        # 原因：取消、preempt 注入、microcompact、proactive snip 和自动压缩都属于
        # prompt 生成前的可插拔检查。做法：把完整 loop state 放入 HookContext.extra，
        # 由 PreemptChecker 与 CompactChecker 按优先级执行。目的：减少 ai_step.py
        # 中的硬编码控制流，同时保持旧行为顺序不变。
        _step_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            extra={"loop_state": ls, "step_count": step_count},
        )
        _step_result = await hook_registry.afire("before_step", _step_ctx)
        if _step_result.action is not None:
            return _step_result.action
        if _step_result.skip_step:
            continue

        result = await _call_llm_with_retry(ls, step)
        if isinstance(result, TaskAction):
            return result
        # ---------------------------------------------------------------
        # Preempt V3 需求1: _call_llm_with_retry 返回 None 表示流式输出
        # 在思考阶段被 preempt 截断。partial assistant message 已丢弃（不存
        # 历史），preempt 消息已存储在 ls.preempt_inject_info 中。
        # 跳到下一轮循环顶部，由 PreemptChecker 注入新用户消息后
        # 重新推理。与 cancel 的区别：不终止 task，继续循环。
        # ---------------------------------------------------------------
        if result is None:
            continue
        resp = result

        # P0 Task 内核化：记录实际完成的步数
        ls.rctx.completed_steps = step + 1

        # Phase 3 Hook System：LLM 调用后的 usage 统计交给 UsageTracker。
        # 原因：token 累加是 after_llm_call 的典型横切逻辑。做法：传入响应和
        # loop state，由 handler 更新 rctx.total_usage。目的：保持 TaskRecord
        # 用量统计不变，同时从 ai_step.py 中抽出 bookkeeping。
        _usage_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _usage_result = await hook_registry.afire("after_llm_call", _usage_ctx)
        if _usage_result.action is not None:
            return _usage_result.action
        # [2026-05-24] Allow after_llm_call hooks (e.g. fallback_provider) to
        # replace the response. Without this, a hook that sets ctx.response to
        # a successful fallback result would be ignored because the local `resp`
        # variable still points to the original failed response.
        if _usage_ctx.response is not resp:
            resp = _usage_ctx.response

        import sys as _ds2; _ds2.stderr.write(f"[DIAG-AISTEP] post-hook: ok={resp.ok} err={repr((resp.error or '')[:150])} text={repr((resp.text or '')[:80])} st={resp.status_code}\n"); _ds2.stderr.flush()
        if not resp.ok:
            return _build_failure_action(ls, resp, step)

        # ---- 从文本中解析工具调用（formatter 统一处理）----
        if not resp.tool_calls:
            _parsed = formatter.parse_tool_calls(resp)
            if _parsed:
                _clean_text = formatter.get_plain_text(resp)
                resp = ProviderResponse(
                    ok=True,
                    text=_clean_text,
                    tool_calls=[
                        ToolCall(id=p.id, name=p.name, arguments=p.arguments)
                        for p in _parsed
                    ],
                    # [refactor 2026-04-18] thinking → reasoning
                    reasoning=resp.reasoning,
                    status_code=resp.status_code,
                    usage=resp.usage,
                )
                # [stream-clean 2026-05-31] Why: JSON tool mode mixes protocol
                # markers (<<<TOOL_CALL>>>) into stream_delta output. The markers
                # are needed for parse_tool_calls but should not remain visible
                # in the frontend. How: after parsing, emit the cleaned plain text
                # so the frontend can replace dirty stream blocks. Purpose: the
                # user sees only the final authoritative text, not raw protocol.
                if _clean_text and _clean_text.strip() and ls.use_stream:
                    await ls.rctx.emit_event("stream_text_final", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
                        "text": _clean_text,
                    })

        if resp.tool_calls:
            action = await _handle_tool_calls(ls, resp, step)
            if action is not None:
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            continue

        # Phase 3 Hook System：纯文本响应交给 PlaintextRetryHandler。
        # 原因：hybrid 隐式 finish 与 tool_only 重试是 before_response 决策。
        # 做法：handler 根据 output_mode 返回 TaskAction 或追加 retry hint。
        # 目的：保留原行为，同时让响应策略可注册。
        _plaintext_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _plaintext_result = await hook_registry.afire("before_response", _plaintext_ctx)
        if _plaintext_result.action is not None:
            return await _fire_task_end_hook_if_finish(ls, _plaintext_result.action, step + 1)
        if _plaintext_result.modified:
            continue  # hook injected retry hint, loop back to LLM

        # LEGACY: replaced by hook PlaintextRetryHandler.
        # action = _handle_plaintext_response(ls, resp, step)
        # if action is not None:
        #     return action

    # ---- 达到最大步数 ----
    # Phase 3 Hook System：max_steps 是任务错误结束路径，先交给 on_task_error
    # 保存上下文。原因：ContextSnapshotSaver 应成为后续终止路径的统一入口；
    # 做法：传入正确 step_count=max_steps，并从 ctx.extra 读取 context_ref。
    # 目的：先安全覆盖此处单一错误路径，其他复杂终止路径保留旧逻辑。
    _error_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=max_steps,
        extra={"loop_state": ls, "step_count": max_steps},
    )
    _error_result = await hook_registry.afire("on_task_error", _error_ctx)
    if _error_result.action is not None:
        return _error_result.action
    ctx_ref = str(_error_ctx.extra.get("context_ref") or "")
    if not _error_ctx.extra.get("snapshot_saved"):
        # LEGACY fallback: replaced by hook ContextSnapshotSaver for max_steps.
        ctx_ref = _persist_ctx(ls, max_steps)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
