from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry

from .pseudo_tools import _is_pseudo_tool_name, build_node_tool_specs, allowed_pseudo_names, refresh_dispatch_to_spec, resolve_effective_action, is_tool_action_allowed
from .resume_builder import apply_resume_messages, collect_resume_attachments
from .loop_state import _LoopState, _build_loop_state, _persist_ctx as _persist_ctx_base, _short
from .llm_call import _call_llm_with_retry, _build_failure_action, _is_retryable_error, _RETRYABLE_STATUS_CODES, call_provider_once
from .pseudo_handlers import _handle_pseudo_tool
# [AutoC 2026-08-19] 执行策略（async_mode 分流、execute_command 自适应升级）已迁入
# engine/builtin/async_scheduler.py；循环仅保留默认同步执行入口与 execute_tool 触发。
from .async_tools import _execute_registry_tool_with_span, _snapshot_tool_context

from ..context_store import load_context_snapshot
from ..attachments import build_multimodal_content
# Phase 1 (Session Conversation Store): 导入 Message 模型用于影子写入。
# ai_step 在每次 append assistant/tool_result 消息后，best-effort 写入 ConversationStore。
from ..conversation_store import Message, MessageType, is_dynamic_context
from ..node import Node
from .message_assembly import assemble_initial_messages, rebuild_dynamic_context
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
    truncate_tool_result,
)
from clonoth_runtime import get_int, load_runtime_config
from toolbox.context import ToolContext
from providers.tool_format import ParsedToolCall, create_tool_formatter
from .message_model import MessageMeta, set_message_meta
from providers.base import BaseProvider, ToolCall, ProviderResponse
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
    pending = getattr(ls, "_pending_shadow_writes", None)
    # Results and their image rows must share the same ordered flush. Otherwise
    # an image is persisted first and breaks native call/result pairing on reload.
    if pending is not None and message_type in ("tool_result", "tool_result_attachment"):
        pending.append((msg_dict, message_type))
        return
    try:
        store = getattr(ls.rctx, 'conversation_store', None)
        if store is None:
            return
        # 跳过 dynamic context 和 ephemeral 消息（如 retry hint）
        if msg_dict.get('_ephemeral'):
            return
        _is_dyn_row = message_type == MessageType.DYNAMIC_CONTEXT
        if msg_dict.get('_dynamic') and not _is_dyn_row:
            return
        if _is_dyn_row and (msg_dict.get('id') or msg_dict.get('_persisted_dynamic')):
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
            response_info=dict(getattr(ls.rctx, 'response_info', {}) or {})
                if msg_dict.get('role') == 'assistant' else {},
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
        if _is_dyn_row:
            msg_dict["id"] = msg_id
            msg_dict["_persisted_dynamic"] = True
            msg_dict.setdefault("_meta", {})["source_task_id"] = msg.source_task_id
        # Phase 3: 记录最后一次影子写入的 message id，供 snapshot 持久化使用
        ls.last_shadow_message_id = msg_id
        # P0 Task 内核化：追踪 first/last message ID 到 RunContext
        if not ls.rctx.first_shadow_message_id:
            ls.rctx.first_shadow_message_id = msg_id
        ls.rctx.last_shadow_message_id = msg_id
    except Exception:
        # A dynamic row must reach disk before subsequent replies: otherwise a
        # resume would move its insertion point and invalidate the frozen prefix.
        if message_type == MessageType.DYNAMIC_CONTEXT:
            raise
        logger.debug("Shadow write failed", exc_info=True)


def _persist_dynamic_messages(ls: _LoopState) -> None:
    """Persist every not-yet-persisted dynamic_context message in ls.messages.

    [Persistent Dynamic Context] Why: fresh assembly, before_prompt_build hooks
    and compact_done re-injection append dynamic_context dicts to ls.messages
    without writing them, so the JSONL lost [User_N, Dynamic_N, Assistant_1]
    ordering and later turns re-injected the same skills. How: walk ls.messages
    in order and shadow-write each dynamic row lacking an id / _persisted_dynamic
    marker (hydrated history rows carry ids and are skipped). Purpose: one call
    site before the step loop (and after compact reloads) keeps disk == memory.
    """
    # Cache-Warm Fork workers reuse the parent's frozen prefix; those dynamic
    # rows belong to (and are already stored in) the parent session.
    if getattr(ls, "_cache_warm_fork", False):
        return
    for message in list(getattr(ls, "messages", None) or []):
        if isinstance(message, dict) and is_dynamic_context(message):
            _shadow_write(ls, message, MessageType.DYNAMIC_CONTEXT)


def _close_execution(execution: Any) -> None:
    """Close an un-awaited execution from a terminated execute_tool chain.

    Why: a handler may supply an execution awaitable and a later handler may end
    the chain with an action, leaving the coroutine un-awaited. How: close
    coroutine-like values best-effort. Purpose: avoid un-awaited coroutine
    warnings without changing terminal semantics.
    """
    close = getattr(execution, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  推理循环子函数
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  工具调用处理
# ---------------------------------------------------------------------------

def _flush_step_shadow_writes(ls: _LoopState) -> None:
    pending = getattr(ls, "_pending_shadow_writes", None)
    if pending is None:
        return
    ls._pending_shadow_writes = None
    _align_step_tool_results_order(
        ls.messages,
        getattr(ls, "_step_tool_msg_start", len(ls.messages)),
        getattr(ls, "_step_tool_calls", None),
    )
    if not pending:
        return
    positions = {id(message): index for index, message in enumerate(ls.messages)}
    for message, message_type in sorted(pending, key=lambda item: positions.get(id(item[0]), len(positions))):
        _shadow_write(ls, message, message_type)


def _persist_ctx(ls: _LoopState, step: int) -> str:
    _flush_step_shadow_writes(ls)
    return _persist_ctx_base(ls, step)


async def _handle_tool_calls(ls: _LoopState, resp, step: int) -> TaskAction | None:
    ls._step_dispatch_conv_keys = {}
    ls._pending_shadow_writes = []
    ls._step_tool_calls = resp.tool_calls
    ls._step_tool_msg_start = len(ls.messages) + 1
    _prev_end_idx = getattr(ls, "_after_prev_round_msg_idx", None)
    if isinstance(_prev_end_idx, int) and len(ls.messages) > _prev_end_idx:
        if any(
            isinstance(_m, dict)
            and _m.get("role") == "user"
            and (_m.get("_meta") or {}).get("message_type") != "tool_result_attachment"
            for _m in ls.messages[_prev_end_idx:]
        ):
            ls._last_round_tool_sig = ""
            ls._consecutive_round_tool_count = 0
    try:
        return await _handle_tool_calls_inner(ls, resp, step)
    finally:
        _flush_step_shadow_writes(ls)
        ls._after_prev_round_msg_idx = len(ls.messages)


def _tool_spec_for(ls: _LoopState, tool_name: str) -> dict[str, Any] | None:
    """Return the registry schema for a tool call, when the registry exposes one.

    Why: action gating must read the same selector the tool itself reads, and the
    registry is the only schema source (no second copy of tool schemas). How: reuse
    the registry's existing per-name lookup. Purpose: registries without it, or a
    failing lookup, keep the name/argument-only gate instead of breaking the call.
    """
    getter = getattr(getattr(ls, "registry", None), "get_spec", None)
    if not callable(getter):
        return None
    try:
        return getter(tool_name)
    except Exception:
        return None


async def _handle_tool_calls_inner(ls: _LoopState, resp, step: int) -> TaskAction | None:
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
    _ordered_calls: list[tuple[str, Any]] = []
    _unauthorized_errors: list[dict[str, Any]] = []  # [fix 2026-06-24] collect, don't append yet

    def _reject_unauthorized(tc) -> None:
        # [AutoC 2026-08-31] 最简拒绝：只告知工具不存在，不给任何
        # 替代指引。Why: 旧文案提示调用 finish()，但 finish 可能也不在
        # 授权列表里，反而误导模型反复重试。
        logger.warning("node %s attempted unauthorized tool call: %s", ls.node.id, tc.name)
        _err_msg = ls.formatter.format_tool_result(
            tc,
            f"Error: Tool '{tc.name}' does not exist.",
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

    for tc in resp.tool_calls:
        # [2026-05-04] Dynamic per-target dispatch tools are pseudo tools too.
        # Why: names like dispatch:child_coder are generated from delegate_targets
        # and must bypass real-tool authorization. How: use the prefix-aware helper
        # instead of a fixed name-only set. Purpose: route fixed-target dispatches
        # to pseudo_handlers without accepting removed aggregate dispatch tools.
        if _is_pseudo_tool_name(tc.name):
            # [fix 2026-09-03] 伪工具同样要做授权校验：只放行本轮实际注入的伪工具。
            # Why: switch_node 此前仅按静态名称放行，未注入该工具的系统节点
            # （如 system.turn_summarizer 摘要的日志里含有 switch_node 调用）可能
            # 幻觉出同名调用并被直接执行，导致 session 入口节点被乱切换。
            if ls.allowed_pseudo_tools is not None and tc.name not in ls.allowed_pseudo_tools:
                _reject_unauthorized(tc)
                continue
            if not is_tool_action_allowed(ls.node, tc.name, tc.arguments or {}):
                _reject_unauthorized(tc)
                continue
            pseudo_calls.append(tc)
            _ordered_calls.append(("pseudo", tc))
        else:
            # Authorize the actual tool name and action, without also requiring
            # its retired alias in the tool list.
            # [fix 2026-10-02] Why: external/MCP tools may read their action from a
            # schema-declared 'mode' enum rather than 'action', so gating on 'action'
            # let a denied mode through next to a harmless extra action. How: hand the
            # registry schema to the gate (same source the tool itself reads).
            # Purpose: the effective action resolved here matches the tool's own.
            _tool_spec = _tool_spec_for(ls, tc.name)
            if (
                tc.name not in ls.allowed_real_tools
                or not is_tool_action_allowed(ls.node, tc.name, tc.arguments or {}, _tool_spec)
            ):
                _reject_unauthorized(tc)
                continue
            _rtc_item = {
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            }
            real_tool_calls.append(_rtc_item)
            _ordered_calls.append(("real", _rtc_item))
            # Runner forwards canonical actions to the memory-extraction gate.
            _args_str = str(tc.arguments or {})[:200]
            _cons_name, _eff_action = resolve_effective_action(tc.name, tc.arguments or {}, _tool_spec)
            ls.rctx.tool_call_log.append({
                "name": _cons_name,
                "action": _eff_action,
                "args_summary": _args_str,
            })

    # 将 LLM 的工具调用决策追加到对话历史
    _assistant_msg = ls.formatter.build_assistant_message(resp, resp.text or "", resp.tool_calls)
    # [refactor 2026-04-18] raw_parts → metadata, thinking_text → reasoning, has_thinking → has_reasoning
    # provider_meta 由 ProviderResponse 透传；engine 只搬运不解读
    # [fix 2026-04-18] provider 名称改为动态获取，不再硬编码 "openai"。
    # ls.provider.name 由 BaseProvider.name 提供，各 provider 子类在初始化时传入。
    response_provider = ls.response_provider or ls.provider
    _provider_name = getattr(response_provider, 'name', '') or 'unknown'
    _tc_meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        metadata={_provider_name: resp.provider_meta} if resp.provider_meta else {},
        tool_call_ids=[tc.id for tc in (resp.tool_calls or [])],
        reasoning=resp.reasoning or "",
        inline_data=resp.inline_data or [],
        usage=dict(ls.last_usage) if ls.last_usage else {},
        # [thinking-time 2026-06-01] Persist precise reasoning timing from stream.
        reasoning_started_at=getattr(ls, '_reasoning_started_iso', '') or '',
        reasoning_ended_at=getattr(ls, '_reasoning_ended_iso', '') or '',
    )
    set_message_meta(_assistant_msg, _tc_meta)
    ls.messages.append(_assistant_msg)
    # Phase 1: 影子写入 assistant 消息到 ConversationStore
    # [2026-05-07] 含 finish 的 assistant 消息也直接持久化。
    # 原因：finish 是真实 API tool_call，删除或拆分它会使后续 tool_result 失去配对来源。
    # 做法：不再调用 sanitize_assistant_control_tools，而是保存原始 assistant.tool_calls。
    # 目的：ConversationStore、snapshot 与 provider replay 的工具轮结构一致。
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

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
        provider=response_provider,
        rctx=ls.rctx,
        step=step,
        response=resp,
        tool_calls=list(resp.tool_calls or []),
        extra={
            "pseudo_calls": pseudo_calls,
            "real_tool_calls": real_tool_calls,
            "unauthorized_count": len(_unauthorized_errors),
        },
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

    # ---- 连续相同工具序列死循环拦截（最多允许连续 3 轮，第 4 轮起报错）----
    if resp.tool_calls and not all(getattr(tc, "name", "") in ("finish", "ask") for tc in resp.tool_calls):
        _round_sig = json.dumps(
            [
                (str(getattr(tc, "name", "") or ""), getattr(tc, "arguments", None) or {})
                for tc in resp.tool_calls
            ],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if _round_sig == getattr(ls, "_last_round_tool_sig", ""):
            ls._consecutive_round_tool_count = int(getattr(ls, "_consecutive_round_tool_count", 0)) + 1
        else:
            ls._last_round_tool_sig = _round_sig
            ls._consecutive_round_tool_count = 1

        if ls._consecutive_round_tool_count > 3:
            _tool_names_str = ", ".join(str(getattr(tc, "name", "") or "") for tc in resp.tool_calls)
            _dup_reject_msg = (
                f"DUPLICATE_CALL_BLOCKED: Identical tool call sequence [{_tool_names_str}] "
                f"has already been executed 3 times consecutively with the exact same arguments "
                f"(attempt #{ls._consecutive_round_tool_count}). "
                "Do NOT repeat the exact same tool calls in a loop — use different tools/arguments "
                "or proceed immediately with your final response."
            )
            logger.warning(
                "Blocked consecutive duplicate tool call loop (node=%s, step=%d, count=%d, tools=%s)",
                ls.node.id,
                step,
                ls._consecutive_round_tool_count,
                _tool_names_str,
            )
            for tc in resp.tool_calls:
                _err = ls.formatter.format_tool_result(tc, _dup_reject_msg)
                set_message_meta(_err, MessageMeta(
                    tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                    message_type="tool_result",
                    tool_rejected=True,
                    tool_rejection_code="duplicate_tool_loop_blocked",
                ))
                ls.messages.append(_err)
                _shadow_write(ls, _err, message_type="tool_result")
            return None

    _step_tool_msg_start = len(ls.messages)
    # [fix 2026-06-24] 在 assistant 消息之后且通过 round-level before_tool_call 检查后，
    # 追加 unauthorized tool 的错误结果，避免 block 路径重复写两条 tool_result。
    for _unerr in _unauthorized_errors:
        ls.messages.append(_unerr)
        _shadow_write(ls, _unerr, message_type="tool_result")

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

    # 按模型声明的先后顺序串行执行伪工具与真实工具（finish/ask 仍延后到本轮末尾）
    _terminal_call = None
    ls._step_tool_atts = []
    ls._step_inline_state = {"used": 0}
    _idx = 0
    _early_action: TaskAction | None = None
    try:
        while _idx < len(_ordered_calls):
            _ckind, _citem = _ordered_calls[_idx]
            if _ckind == "pseudo":
                _idx += 1
                if _citem.name in ("finish", "ask"):
                    _terminal_call = _citem
                    continue
                action = await _handle_pseudo_tool(ls, _citem, step)
                if action is not None:
                    _early_action = action
                    break
            else:
                _real_chunk: list[dict[str, Any]] = []
                while _idx < len(_ordered_calls) and _ordered_calls[_idx][0] == "real":
                    _real_chunk.append(_ordered_calls[_idx][1])
                    _idx += 1
                action = await _execute_real_tools(ls, _real_chunk, step)
                refresh_dispatch_to_spec(ls)
                if action is not None:
                    _early_action = action
                    break
                ls.use_stream = ls.streaming
    finally:
        # 若中途因 action/cancel 提前退出，为尚未执行的后续工具补齐配对 tool_result，防止 API 400
        if _idx < len(_ordered_calls):
            for _rem_kind, _rem_item in _ordered_calls[_idx:]:
                if _rem_kind == "pseudo":
                    if _rem_item.name in ("finish", "ask"):
                        continue
                    _rem_tc = ParsedToolCall(
                        id=str(getattr(_rem_item, "id", "") or ""),
                        name=str(_rem_item.name),
                        arguments=dict(_rem_item.arguments or {}),
                    )
                else:
                    _rem_tc = ParsedToolCall(
                        id=str(_rem_item.get("id") or ""),
                        name=str(_rem_item["name"]),
                        arguments=dict(_rem_item.get("arguments") or {}),
                    )
                _rem_msg = ls.formatter.format_tool_result(
                    _rem_tc,
                    "Skipped: tool batch interrupted or cancelled before execution.",
                )
                set_message_meta(_rem_msg, MessageMeta(
                    tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                    message_type="tool_result",
                ))
                ls.messages.append(_rem_msg)
                _shadow_write(ls, _rem_msg, MessageType.TOOL_RESULT)

        # 先对所有 role="tool" 结果按 assistant.tool_calls 原始顺序对齐，再追加图片附件 user 消息
        _align_step_tool_results_order(ls.messages, _step_tool_msg_start, resp.tool_calls)
        _collected_step_atts = getattr(ls, "_step_tool_atts", None) or []
        ls._step_tool_atts = []
        if _collected_step_atts:
            _tool_att_msg = {
                "role": "user",
                "content": build_multimodal_content(
                    "The tool executions above produced the following image results:",
                    _collected_step_atts,
                    workspace_root=ls.rctx.workspace_root,
                ),
                "_meta": {"message_type": "tool_result_attachment", "attachments": list(_collected_step_atts)},
            }
            ls.messages.append(_tool_att_msg)
            _shadow_write(ls, _tool_att_msg, message_type="tool_result_attachment")

    if _early_action is not None:
        return _early_action

    if real_tool_calls and ls.preempt_after_step:
        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=ctx_ref,
        )

    _flush_step_shadow_writes(ls)

    # finish/ask 最后执行（同轮真实工具已完成）
    if _terminal_call:
        # [AutoC 2026-08-20] Why: whether a terminal tool may actually run is a
        # policy decision (preempt interception), not loop mechanics. How: fire
        # terminal_tool before executing finish/ask; handlers may intercept
        # (write an ephemeral paired tool_result and keep the task running) or
        # return a terminal action (PREEMPTED). Purpose: the loop no longer
        # knows about preempt scheduling around terminal tools.
        _terminal_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_terminal_call,
            tool_calls=list(resp.tool_calls or []),
            extra={"loop_state": ls, "terminal_call": _terminal_call},
        )
        _terminal_result = await hook_registry.afire("terminal_tool", _terminal_ctx)
        if _terminal_result.action is not None:
            return _terminal_result.action
        if not _terminal_result.channels.get("intercepted"):
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

def _align_step_tool_results_order(
    messages: list[dict[str, Any]],
    start_idx: int,
    tool_calls: list[Any] | None,
) -> None:
    """将当前 step 写入的 role=tool 消息按 assistant.tool_calls 的 id 顺序对齐。"""
    if not tool_calls or start_idx >= len(messages):
        return
    tc_order = {
        str(getattr(tc, "id", "") or (tc.get("id", "") if isinstance(tc, dict) else "")): idx
        for idx, tc in enumerate(tool_calls)
    }
    if not tc_order:
        return
    end_idx = start_idx
    while end_idx < len(messages) and isinstance(messages[end_idx], dict) and messages[end_idx].get("role") == "tool":
        end_idx += 1
    if end_idx - start_idx <= 1:
        return
    tool_slice = messages[start_idx:end_idx]
    messages[start_idx:end_idx] = sorted(
        enumerate(tool_slice),
        key=lambda pair: (tc_order.get(str(pair[1].get("tool_call_id") or ""), 100000 + pair[0]), pair[0]),
    )
    messages[start_idx:end_idx] = [pair[1] for pair in messages[start_idx:end_idx]]


def _is_pure_tool_call(name: str, args: dict[str, Any], spec: dict[str, Any] | None) -> bool:
    """仅声明式后台异步工具（async_mode=True）允许合并批次并发启动，其余常规工具一律按声明顺序串行执行。"""
    s = spec if isinstance(spec, dict) else {}
    return bool(s.get("async_mode"))


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
    _step_inline_state: dict[str, int] = getattr(ls, "_step_inline_state", None) or {"used": 0}

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

    # 按连续 pure/async_mode 属性分组：连续的 pure 工具组合成并发批次，非 pure 工具作为单元素顺序屏障
    _batches: list[list[dict[str, Any]]] = []
    for _rtc in real_tool_calls:
        _spec_cand = ls.registry.get_spec(_rtc["name"])
        _is_p = _is_pure_tool_call(_rtc["name"], _rtc.get("arguments") or {}, _spec_cand)
        if _is_p and _batches and getattr(_batches[-1][0], "get", None) and _batches[-1][0].get("_batch_pure"):
            _rtc_copy = dict(_rtc, _batch_pure=True)
            _batches[-1].append(_rtc_copy)
        else:
            _rtc_copy = dict(_rtc, _batch_pure=_is_p)
            _batches.append([_rtc_copy])

    try:
        for _batch in _batches:
            if await ls.rctx.check_cancelled():
                break

            # 阶段 1：按顺序执行批次内各工具的 before_tool_call 与 execute_tool 预准备
            _prepared_batch: list[dict[str, Any]] = []
            for _rtc in _batch:
                if await ls.rctx.check_cancelled():
                    break
                _t_name = _rtc["name"]
                _t_args = _rtc["arguments"]
                _call_ctx = _snapshot_tool_context(_tool_ctx)
                _call_ctx.tool_call_id = str(_rtc.get("id") or "")
                _call_ctx.node_id = ls.node.id
                _tool_ctx.tool_call_id = _call_ctx.tool_call_id
                _tool_ctx.node_id = ls.node.id

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
                    extra={"real_tool_calls": real_tool_calls, "tool_ctx": _call_ctx, "tool_args": _t_args},
                )
                _tool_hook_result = await hook_registry.afire("before_tool_call", _tool_hook_ctx)
                if _tool_hook_result.action is not None:
                    for _p in _prepared_batch:
                        _close_execution(_p.get("execution"))
                    return _tool_hook_result.action
                if _tool_hook_result.block or _tool_hook_result.skip_step:
                    _blocked_msg = (
                        _tool_hook_result.error_message
                        or _tool_hook_result.reason
                        or "Tool call blocked by before_tool_call hook."
                    )
                    _prepared_batch.append({
                        "rtc": _rtc,
                        "status": "before_blocked",
                        "blocked_msg": _blocked_msg,
                    })
                    continue

                _spec = ls.registry.get_spec(_t_name)
                _tool_t0 = time.monotonic()
                await ls.rctx.emit_event("tool_call_start", {
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                    "tool_call_id": _rtc.get("id", ""),
                    "tool_name": _t_name,
                    "arguments": _t_args,
                })
                _exec_ctx = HookContext(
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
                        "tool_ctx": _call_ctx,
                        "spec": _spec if isinstance(_spec, dict) else {},
                        "call_index": len(_tool_entries) + len(_prepared_batch),
                        "t0": _tool_t0,
                    },
                )
                _exec_result = await hook_registry.afire("execute_tool", _exec_ctx)
                if _exec_result.action is not None:
                    _close_execution(_exec_result.channels.get("execution"))
                    for _p in _prepared_batch:
                        _close_execution(_p.get("execution"))
                    return _exec_result.action
                if _exec_result.block or _exec_result.skip_step:
                    _blocked_msg = (
                        _exec_result.error_message
                        or _exec_result.reason
                        or "Tool call blocked by execute_tool hook."
                    )
                    _prepared_batch.append({
                        "rtc": _rtc,
                        "status": "exec_blocked",
                        "blocked_msg": _blocked_msg,
                        "t0": _tool_t0,
                    })
                    continue

                _execution = _exec_result.channels.get("execution")
                _prepared_batch.append({
                    "rtc": _rtc,
                    "status": "run",
                    "execution": _execution,
                    "call_ctx": _call_ctx,
                    "current_tool_call": _current_tool_call,
                    "t0": _tool_t0,
                })

            # 阶段 2：并发或单条执行批次内的 runnable 工具
            async def _await_prepared(_item: dict[str, Any]) -> Any:
                _ex = _item.get("execution")
                if _ex is None:
                    return await _execute_registry_tool_with_span(
                        ls.registry,
                        _item["rtc"]["name"],
                        _item["rtc"]["arguments"],
                        _item["call_ctx"],
                    )
                if inspect.isawaitable(_ex):
                    return await _ex
                return _ex

            _runnable_items = [_it for _it in _prepared_batch if _it["status"] == "run"]
            if len(_runnable_items) > 1:
                _batch_results = await asyncio.gather(*(_await_prepared(_it) for _it in _runnable_items))
                for _it, _res in zip(_runnable_items, _batch_results):
                    _it["t_result"] = _res
            elif len(_runnable_items) == 1:
                _runnable_items[0]["t_result"] = await _await_prepared(_runnable_items[0])

            # 阶段 3：严格按原始顺序执行 after_tool_call、预算截断、事件发送与结果收集
            _batch_cancelled = False
            for _it in _prepared_batch:
                _rtc = _it["rtc"]
                _t_name = _rtc["name"]
                _t_args = _rtc["arguments"]
                if _it["status"] == "before_blocked":
                    _tool_entries.append({
                        "id": _rtc.get("id", ""),
                        "name": _t_name,
                        "args": _t_args,
                        "format": "text",
                        "raw_inline": _it["blocked_msg"],
                        "truncated": False,
                        "ref": "",
                    })
                    continue
                _tool_t0 = _it["t0"]
                if _it["status"] == "exec_blocked":
                    _blocked_msg = _it["blocked_msg"]
                    _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1)
                    _tool_entries.append({
                        "id": _rtc.get("id", ""),
                        "name": _t_name,
                        "args": _t_args,
                        "format": "text",
                        "raw_inline": _blocked_msg,
                        "truncated": False,
                        "ref": "",
                        "elapsed_ms": _t_elapsed_ms,
                        "attachments": [],
                    })
                    await ls.rctx.emit_event("tool_call_end", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "tool_call_id": _rtc.get("id", ""),
                        "tool_name": _t_name,
                        "status": "blocked",
                        "result": None,
                        "raw_inline": _blocked_msg,
                        "format": "text",
                        "elapsed_ms": _t_elapsed_ms,
                    })
                    continue

                _t_result = _it["t_result"]
                _call_ctx = _it["call_ctx"]
                _current_tool_call = _it["current_tool_call"]

                if isinstance(_t_result, dict) and _t_result.get("async_started"):
                    _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1)
                    _t_raw_inline = str(_t_result.get("raw_inline") or "")
                    _tool_entries.append({
                        "id": _rtc.get("id", ""),
                        "name": _t_name,
                        "args": _t_args,
                        "format": "text",
                        "raw_inline": _t_raw_inline,
                        "truncated": False,
                        "ref": "",
                        "elapsed_ms": _t_elapsed_ms,
                        "attachments": [],
                    })
                    await ls.rctx.emit_event("tool_call_end", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "tool_call_id": _rtc.get("id", ""),
                        "tool_name": _t_name,
                        "status": "async_started",
                        "result": None,
                        "raw_inline": _t_raw_inline,
                        "format": "text",
                        "elapsed_ms": _t_elapsed_ms,
                    })
                    await ls.rctx.emit_event("handoff_progress", {
                        "message": f"[{ls.node.id}] {_t_name}: {str(_t_result.get('handoff_message') or 'Async execution started')}",
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                    })
                    continue

                _t_cancelled = isinstance(_t_result, dict) and _t_result.get("cancelled")
                _t_spec = ls.registry.get_spec(_t_name)
                _t_fmt, _t_raw = result_to_raw(_t_name, _t_result, tool_spec=_t_spec)
                _t_raw_inline = _t_raw
                _t_truncated = False
                _t_ref = ""

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
                _ov = _after_result.channels.get("result_override")
                if isinstance(_ov, dict):
                    if "raw_inline" in _ov:
                        _t_raw_inline = _ov["raw_inline"]
                    _t_truncated = bool(_ov.get("truncated", _t_truncated))
                    _t_ref = str(_ov.get("ref") or "")
                elif isinstance(_t_raw, str):
                    _fallback_limit = get_tool_inline_limit(_t_name, ls.runtime_cfg)
                    _t_raw_inline, _t_truncated = truncate_tool_result(
                        _t_name, _t_raw, _fallback_limit, "", config=ls.runtime_cfg,
                    )
                if isinstance(_t_raw_inline, str):
                    _step_inline_state["used"] = _step_inline_state.get("used", 0) + len(_t_raw_inline)

                _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1)
                _tool_entries.append({
                    "id": _rtc.get("id", ""),
                    "name": _t_name,
                    "args": _t_args,
                    "format": _t_fmt,
                    "raw_inline": _t_raw_inline,
                    "truncated": _t_truncated,
                    "ref": _t_ref,
                    "elapsed_ms": _t_elapsed_ms,
                    "attachments": list(_tool_atts[_tool_atts_start:]),
                    "result": _t_result,
                })

                await ls.rctx.emit_event("tool_call_end", {
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                    "tool_call_id": _rtc.get("id", ""),
                    "tool_name": _t_name,
                    "status": "cancelled" if _t_cancelled else ("error" if (isinstance(_t_result, dict) and _t_result.get("error")) else "success"),
                    "result": _t_result,
                    "raw_inline": _t_raw_inline,
                    "format": _t_fmt,
                    "elapsed_ms": _t_elapsed_ms,
                })

                if _call_ctx.workspace is not None or (_t_name == 'set_workspace' and _call_ctx.workspace_name):
                    ls.rctx.workspace = _call_ctx.workspace
                    _tool_ctx.workspace = _call_ctx.workspace
                if _call_ctx.workspace_name and _call_ctx.workspace_name != getattr(ls.rctx, 'workspace_name', ''):
                    ls.rctx.workspace_name = _call_ctx.workspace_name
                    _tool_ctx.workspace_name = _call_ctx.workspace_name

                if _t_cancelled:
                    _batch_cancelled = True
                    break

                await ls.rctx.emit_event("handoff_progress", {
                    "message": f"[{ls.node.id}] {_t_name}",
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                })

            if _batch_cancelled:
                break
    finally:
        # 确保因取消或 hook 提前返回时，未执行的剩余 real_tool_calls 全部补齐配对占位结果并落盘
        # _tool_entries 严格按 real_tool_calls 顺序追加，直接从 len(_tool_entries) 起补齐剩余槽位，
        # 避免同轮出现重复/空 tool_call id 时因 set 去重而漏补。
        for _rtc_rem in real_tool_calls[len(_tool_entries):]:
            _rem_id = str(_rtc_rem.get("id") or "")
            _tool_entries.append({
                "id": _rem_id,
                "name": _rtc_rem["name"],
                "args": _rtc_rem.get("arguments") or {},
                "format": "text",
                "raw_inline": "Skipped: tool batch interrupted or cancelled before execution.",
                "truncated": False,
                "ref": "",
                "elapsed_ms": 0.0,
                "attachments": [],
            })
        if _tool_entries:
            _persist_tool_results(ls, _tool_entries, _tool_atts)

    return None


def _persist_tool_results(
    ls: _LoopState,
    entries: list[dict[str, Any]],
    tool_atts: list[dict[str, Any]],
) -> None:
    """Format, persist, and append tool result messages to the loop state.

    [AutoC 2026-08-22] Extracted from _execute_real_tools so that an
    after_tool_call handler can replace or extend the write-back logic
    by calling ctx.stop_chain() and performing its own persistence.
    Without interception this function produces the same output as before.
    """
    for _entry in entries:
        _result_body = _entry["raw_inline"]
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
        _tool_meta = _tool_msg.setdefault("_meta", {})
        _structured = _entry.get("result")
        if isinstance(_structured, dict):
            _tool_meta["tool_result_structured"] = _structured
        _tool_meta["tool_result_raw_inline"] = str(_entry.get("raw_inline") or "")
        _tool_meta["tool_result_format"] = str(_entry.get("format") or "")
        if _entry.get("elapsed_ms") is not None:
            _tool_meta["tool_result_elapsed_ms"] = _entry.get("elapsed_ms")
        _entry_attachments = _entry.get("attachments")
        if isinstance(_entry_attachments, list):
            _tool_meta["attachments"] = list(_entry_attachments)
            _tool_meta["tool_result_attachments"] = list(_entry_attachments)
        ls.messages.append(_tool_msg)
        _shadow_write(ls, _tool_msg, MessageType.TOOL_RESULT)
    if tool_atts:
        step_atts = getattr(ls, "_step_tool_atts", None)
        if isinstance(step_atts, list):
            step_atts.extend(tool_atts)
        else:
            _tool_att_msg = {
                "role": "user",
                "content": build_multimodal_content(
                    "The tool executions above produced the following image results:", tool_atts, workspace_root=ls.rctx.workspace_root,
                ),
                "_meta": {"message_type": "tool_result_attachment", "attachments": list(tool_atts)},
            }
            ls.messages.append(_tool_att_msg)
            _shadow_write(ls, _tool_att_msg, message_type="tool_result_attachment")


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
    # [AutoC 2026-08-30] nodes face: plugin-declared nodes (Paradox-style
    # override of file-sourced nodes). Storage is module-level, so mounting a
    # fresh thin face per ai_step call shares the same declarations.
    from engine.faces.nodes import PluginNodesFace
    _engine_ctx.contributions.mount("nodes", PluginNodesFace())
    auto_discover_and_register(hook_registry, tool_registry=registry, context=_engine_ctx)
    # Phase 3 External Hook Plugins：每次进入 AI 节点时扫描工作区 plugins/。
    # 原因：用户需要在不修改 engine 源码的情况下添加自定义 handler。
    # 做法：调用幂等的外部插件加载器；HookRegistry 会按 handler.name 替换旧实例。
    # 目的：启动时自动发现插件，同时避免重复注册和单个插件失败影响引擎启动。
    load_external_plugins(hook_registry, rctx.workspace_root / "plugins", context=_engine_ctx, process="engine")

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)

    # ---- 收集附件 ----
    # [AutoC 2026-08-20] Why: resume attachment merging is a nested-fallback
    # chain over resume payloads, not loop logic. How: delegate to the resume
    # module collector. Purpose: keep the entry function declarative.
    collected_attachments = collect_resume_attachments(attachments, resume_data)
    _tool_produced_attachments: list[dict[str, Any]] = []

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
    _cache_warm_fork_tools: list[dict[str, Any]] | None = None
    _dispatch_origin_ctx = (rctx.task_context or {}).get("dispatch_origin") if isinstance(rctx.task_context, dict) else None
    _fork_snap_rel = str((_dispatch_origin_ctx or {}).get("fork_snapshot_path") or "").strip() if isinstance(_dispatch_origin_ctx, dict) else ""
    _fork_snap_data: dict[str, Any] | None = None
    _worker_task_prompt = instruction
    if _fork_snap_rel and not resume_data:
        try:
            import json as _json
            _fork_snap_file = (Path(rctx.workspace_root) / _fork_snap_rel).resolve()
            if _fork_snap_file.is_file():
                _fork_snap_data = _json.loads(_fork_snap_file.read_text(encoding="utf-8"))
                try:
                    _fork_snap_file.unlink()
                except Exception:
                    pass
        except Exception:
            _fork_snap_data = None

    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if _fork_snap_data and isinstance(_fork_snap_data.get("messages"), list) and _fork_snap_data["messages"]:
        # Cache-Warm Fork: reuse parent's exact pre-history + conversation history prefix
        # Freeze the parent request prefix before appending this worker's task.
        messages = [dict(message) for message in _fork_snap_data["messages"]]
        for message in messages:
            message.pop("_dynamic", None)
        _worker_task_prompt = (
            "[Sub-agent Worker Task]\n"
            "You are running as an isolated background worker sub-agent forked from the parent session to execute a specific bounded task.\n"
            "- Focus strictly on completing the task below and return your factual findings/results directly.\n"
            "- Do NOT roleplay as the chat persona or call switch_node; return only task-relevant results.\n\n"
            f"Task Instruction:\n{instruction}"
        )
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(_worker_task_prompt, attachments, workspace_root=rctx.workspace_root)})
        else:
            messages.append({"role": "user", "content": _worker_task_prompt})
        if isinstance(_fork_snap_data.get("openai_tools"), list):
            _cache_warm_fork_tools = list(_fork_snap_data["openai_tools"])
    elif snapshot and isinstance(snapshot.get("messages"), list):
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
            inbound_message=getattr(rctx, "inbound_message", None) if not resume_data else None,
        )
        if resume_data and not instruction:
            messages = [m for m in messages if not (is_dynamic_context(m) and not m.get("id") and not m.get("_persisted_dynamic"))]
        _assembled_fresh = True

    if _assembled_fresh:
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
                "is_resume": bool(resume_data),
            },
        )
        _prompt_result = await hook_registry.afire("before_prompt_build", _prompt_ctx)
        if _prompt_result.action is not None:
            return _prompt_result.action

    # Preserve the durable input identity after prompt hooks have rebuilt the list.
    # Historical rows already carry ids; never identify them by text or position.
    inbound_id = getattr(rctx, "inbound_message_id", "")
    if inbound_id and instruction and not resume_data:
        for message in reversed(messages):
            if message.get("role") != "user" or message.get("id") or is_dynamic_context(message):
                continue
            body = message.get("content")
            text = body if isinstance(body, str) else next((
                p.get("text", "") for p in (body or [])
                if isinstance(p, dict) and p.get("type") == "text"
            ), "")
            expected = _worker_task_prompt
            if text == expected or (attachments and text.startswith(expected + "\n")):
                message["id"] = inbound_id
                inbound = getattr(rctx, "inbound_message", None) or {}
                if inbound.get("message_type"):
                    message["message_type"] = inbound["message_type"]
                if inbound.get("_meta"):
                    message["_meta"] = dict(inbound["_meta"])
                break

    # ---- 追加恢复消息 ----
    formatter = create_tool_formatter(node.tool_mode)
    if resume_data:
        await apply_resume_messages(
            messages=messages, resume_data=resume_data,
            formatter=formatter, node=node, rctx=rctx,
        )

    # Finish this registry's initial MCP attempt before freezing visible tools,
    # including legacy resume. The bounded wait preserves coroutine cancellation;
    # business cancellation is checked by the existing before_step hook afterwards.
    from engine.builtin.mcp import wait_mcp_initial_discovery
    await wait_mcp_initial_discovery(registry=registry)

    # ---- 构建工具列表 ----
    _allowed_real_tools, _node_openai_tools = build_node_tool_specs(
        registry=registry, node=node,
        downstream_info=downstream_info, switch_info=switch_info,
        task_context=rctx.task_context,
        has_active_children=False,
    )
    if _cache_warm_fork_tools is not None:
        # Keep the exact same tools array in the API payload to preserve 100% of parent's Prefix KV Cache,
        # while enforcing the worker node's authorization set (and blocking switch_node/dispatch_to).
        openai_tools = _cache_warm_fork_tools
        _allowed_pseudo_tools = allowed_pseudo_names(_node_openai_tools, node) - {"switch_node", "dispatch_to"}
    else:
        openai_tools = _node_openai_tools
        _allowed_pseudo_tools = allowed_pseudo_names(openai_tools, node)
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
    # [AutoC 2026-08-20] Why: config-plumbing defaults (compact thresholds,
    # retry backoff, plaintext retry) are not entry-function concerns. How:
    # delegate to the loop-state factory with the resolved inputs.
    # Purpose: run_ai_node states its inputs, not its config defaults.
    ls = _build_loop_state(
        rctx=rctx, node=node, provider=provider, registry=registry,
        run_id=run_id, context_ref=context_ref, runtime_cfg=runtime_cfg,
        streaming=streaming, messages=messages, system_prompt=system_prompt,
        is_block_mode=_is_block_mode, openai_tools=openai_tools, history=history,
        collected_attachments=collected_attachments,
        tool_produced_attachments=_tool_produced_attachments,
        formatter=formatter, allowed_real_tools=_allowed_real_tools,
        allowed_pseudo_tools=_allowed_pseudo_tools,
    )

    ls._cache_warm_fork = _cache_warm_fork_tools is not None
    if str((resume_data or {}).get("type") or "") == "output_rejected":
        from engine.builtin.preempt import is_user_entry_task
        if is_user_entry_task(ls):
            ls.plugin_data.setdefault("preempt", {})["defer_for_output_reject"] = True

    # [fix 2026-09-11] Why: a compactor failure resumes the caller with
    # compact_done success=False. Marking only deduplicated resumes as compacted
    # made the same caller immediately dispatch another compactor. How: every
    # completed compaction attempt consumes this loop's automatic compaction
    # opportunity; the session circuit breaker controls later tasks. Purpose:
    # one failed upstream compaction cannot create a resume loop.
    if str((resume_data or {}).get("type") or "") == "compact_done":
        _is_entry_branch = bool(
            getattr(rctx, "parent_session_id", "")
            and not getattr(rctx, "child_session_id", "")
        )
        _min_kept = 0 if _is_entry_branch else 1
        if (
            not resume_data.get("success", True)
            or resume_data.get("deduped")
            or int(resume_data.get("kept_segments", 0) or 0) <= _min_kept
        ):
            ls.compacted = True

        rebuild_dynamic_context(
            ls.messages, workspace_root=rctx.workspace_root, node=node,
            runtime_cfg=runtime_cfg, history=ls.messages,
            instruction=str(getattr(rctx, "user_text", "") or instruction or ""),
            is_block_mode=_is_block_mode, system_prompt=system_prompt,
            session_id=rctx.session_id,
            workspace_name=getattr(rctx, "workspace_name", "") or "",
            task_context=rctx.task_context,
        )

    _persist_dynamic_messages(ls)
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

        _persist_dynamic_messages(ls)
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
        # The hook can request one alternate call without knowing LoopState or
        # duplicating formatting, request IDs, cancellation and preempt handling.
        async def _call_provider(candidate, *, messages=None):
            return await call_provider_once(ls, step, candidate, messages=messages)

        _usage_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls, "call_provider": _call_provider},
        )
        _usage_result = await hook_registry.afire("after_llm_call", _usage_ctx)
        if _usage_result.action is not None:
            return _usage_result.action
        if _usage_result.skip_step:
            continue
        # [2026-05-24] Allow after_llm_call hooks (e.g. fallback_provider) to
        # replace the response. Without this, a hook that sets ctx.response to
        # a successful fallback result would be ignored because the local `resp`
        # variable still points to the original failed response.
        if _usage_ctx.response is not resp:
            resp = _usage_ctx.response

        if not resp.ok:
            return _build_failure_action(ls, resp, step)

        # Only the accepted response supplies message provenance and usage.
        # The configured ls.provider is intentionally unchanged for the next step.
        ls.response_provider = _usage_ctx.provider
        ls.last_usage = dict(resp.usage or {})
        if isinstance(ls.last_usage.get("prompt_tokens"), int):
            ls.last_prompt_tokens = ls.last_usage["prompt_tokens"]
        ls.rctx.response_info = {
            "provider": getattr(_usage_ctx.provider, "name", "") or "unknown",
            "model": getattr(_usage_ctx.provider, "model", "") or "",
            "notices": list(_usage_result.channels.get("response_notices") or []),
            "usage": dict(ls.last_usage),
        }

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
                    inline_data=resp.inline_data,
                    provider_meta=resp.provider_meta,
                    raw=resp.raw,
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
            provider=ls.response_provider or ls.provider,
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
        error="Maximum step limit reached.",
        context_ref=ctx_ref,
    )
