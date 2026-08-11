"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具和 dispatch:{target_id} 动态伪工具的执行逻辑。
"""
from __future__ import annotations

import json
import mimetypes as _mimetypes
from pathlib import Path
from typing import Any

from .resume_builder import _select_attachments
from ..compact_flow import prepare_compaction
from ..protocol import TaskAction, ACTION_ASK, ACTION_DISPATCH, ACTION_FINISH
from .loop_state import _LoopState, _persist_ctx, _short
# 【Fix 3】reply 工具结果统一走 formatter.format_tool_result，需要 ParsedToolCall 构造、
# MessageMeta 标注 message_type、MessageType 影子写入。延迟导入 _shadow_write 避免循环依赖。
from .tool_format import ParsedToolCall
from .pseudo_tools import _dispatch_target_from_tool_name
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType


# ---------------------------------------------------------------------------
#  [2026-04-22] 辅助函数：将 workspace-relative 路径列表转换为 attachment dict 列表。
#  [2026-05-04] 现在只服务 dispatch:{target_id} 动态伪工具。
#  为什么：旧聚合委派工具已删除，但动态委派仍需把父节点文件传给子节点。
#  怎么做：保留路径到 attachment dict 的转换函数，删除旧分支调用。
#  目的：让动态 dispatch 的附件行为保持不变。
# ---------------------------------------------------------------------------
import shutil as _shutil
import uuid as _uuid


def _paths_to_attachments(
    paths: list,
    workspace_root: Path,
    *,
    session_workspace: Path | None = None,
) -> list[dict]:
    """Convert file paths to attachment dicts for dispatch.

    Accepts workspace-relative and absolute paths.  Uses classify_path to
    determine trust level:
      - workspace / trusted: store the path as-is (backend serves via
        session workspace trust resolution)
      - external: copy into data/attachments/tool_output/ so the
        authenticated endpoint can serve from a safe directory
    """
    from clonoth_runtime import classify_path as _classify, parse_extra_roots, load_yaml_dict

    _policy_path = workspace_root / "data" / "policy.yaml"
    _policy_cfg = load_yaml_dict(_policy_path) if _policy_path.exists() else {}
    _extra_roots = parse_extra_roots(workspace_root, _policy_cfg.get("extra_roots"))

    result = []
    ws_resolved = workspace_root.resolve()
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
            continue
        full = Path(p_str) if Path(p_str).is_absolute() else workspace_root / p_str
        full = full.resolve()
        if not full.exists():
            continue

        _, _, trust = _classify(
            workspace_root, _extra_roots, str(full),
            workspace=session_workspace,
        )

        if trust in ("workspace", "trusted"):
            # Store as absolute path; backend resolves trust at serve time
            serve_path = str(full)
        else:
            # External: copy to safe directory
            att_dir = workspace_root / "data" / "attachments" / "tool_output"
            att_dir.mkdir(parents=True, exist_ok=True)
            dest_name = f"{_uuid.uuid4().hex[:8]}_{full.name}"
            dest = att_dir / dest_name
            _shutil.copy2(str(full), str(dest))
            serve_path = f"data/attachments/tool_output/{dest_name}"

        mime = _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        att_type = "image" if mime.startswith("image/") else "file"
        result.append({
            "type": att_type,
            "path": serve_path,
            "mime_type": mime,
            "name": full.name,
        })
    return result


def _emit_pseudo_tool_result(
    ls: _LoopState,
    pseudo_call,
    content: str,
    *,
    persist: bool = True,
    control_tool_name: str = "",
    control_tool_status: str = "",
    meta_patch: dict[str, Any] | None = None,
) -> None:
    """统一写入伪工具的 tool_result，确保 native 模式下 tool_use/tool_result 配对完整。

    [2026-05-07] 正常 finish 也通过本函数写普通工具结果。
    原因：finish 现在是完整落盘的真实 API 工具，不能再被改写为普通 assistant 文本。
    做法：默认 persist=True 且不设置 control 标记；只有未交付的拦截类结果才使用 control 参数。
    目的：保留 provider 配对，同时让长期历史保存 assistant.tool_call + tool_result。
    """
    from .ai_step import _shadow_write
    _parsed = ParsedToolCall(
        id=getattr(pseudo_call, "id", "") or "",
        name=pseudo_call.name,
        arguments=dict(pseudo_call.arguments or {}),
    )
    tool_msg = ls.formatter.format_tool_result(_parsed, content)
    if control_tool_name:
        # [2026-05-07] 控制流工具结果必须保留运行期配对字段，但不能成为长期历史。
        # 原因：finish 是真实 provider tool_use，需要 ACK；同时 fake-native/json 结果默认没有 call_id，旧清洗只能按全局兜底处理。
        # 做法：给控制 ACK 标记 _ephemeral，并补齐 tool_call_id/name，供当轮内存配对和清洗函数精确识别。
        # 目的：满足 provider 配对要求，同时避免 finish 结果进入 ConversationStore、快照、压缩和摘要。
        tool_msg["_ephemeral"] = True
        if _parsed.id:
            tool_msg.setdefault("tool_call_id", _parsed.id)
        tool_msg.setdefault("name", control_tool_name)
    set_message_meta(tool_msg, MessageMeta(
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="tool_result",
        control_tool_name=control_tool_name,
        control_tool_status=control_tool_status,
    ))
    if meta_patch:
        # [AutoC 2026-06-17] Why: terminal pseudo tools can carry final delivery
        # metadata such as selected attachments and request ids, but the stored
        # assistant tool_call row is written before the pseudo handler knows the
        # final attachment list. How: attach durable presentation metadata to the
        # paired tool_result row. Purpose: refreshed web history can restore the
        # same final attachments and request ownership as realtime outbound events.
        tool_msg.setdefault("_meta", {}).update(meta_patch)
    ls.messages.append(tool_msg)
    if persist:
        _shadow_write(ls, tool_msg, MessageType.TOOL_RESULT)


async def _handle_pseudo_tool(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理伪工具调用。

    返回 TaskAction 则退出循环（终止型伪工具或 compact dispatch）；
    返回 None 表示已处理完毕，调用方判断是否继续。
    """
    args = pseudo_call.arguments or {}

    # intermediate_reply: 非终止，发送中间消息
    if pseudo_call.name == "intermediate_reply":
        reply_text = str(args.get("text") or "").strip()
        if reply_text:
            # 解析附件
            _reply_att_paths = args.get("attachment_paths") or []
            _reply_atts = _paths_to_attachments(_reply_att_paths, ls.rctx.workspace_root, session_workspace=ls.rctx.workspace)
            _reply_payload: dict[str, Any] = {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
            }
            if _reply_atts:
                _reply_payload["attachments"] = _reply_atts
            await ls.rctx.emit_event("intermediate_reply", _reply_payload)
            _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # preempt_task: 非终止，软打断子任务
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, pseudo_call, args)

    # [2026-05-04] dispatch:{target_id}: 非终止，固定目标异步委派。
    # Why: dynamic per-target tools remove the target parameter from the schema.
    # How: extract target_id from the tool name and pass it to the shared dispatch
    # sender. Purpose: keep supervisor API behavior identical while making target
    # selection happen at tool registration time.
    _fixed_dispatch_target = _dispatch_target_from_tool_name(pseudo_call.name)
    if _fixed_dispatch_target:
        return await _handle_pseudo_dispatch(ls, {**args, "target": _fixed_dispatch_target}, pseudo_call)

    # ---- 终止型伪工具：finish / ask / switch_node ----

    # [AutoC 2026-08-06] hybrid 模式下 finish 仍然正常执行（不拒绝）。
    # 虽然 hybrid 模式不注入 finish 工具定义，但过渡期模型可能从历史中
    # 学到 finish 模式。直接让它走正常 finish 路径，避免拒绝循环。
    if pseudo_call.name in ("finish", "ask"):
        terminal_name = str(pseudo_call.name or "").strip()
        result_text = str(args.get("text") or "").strip()
        _call_id = getattr(pseudo_call, "id", "") or ""
        _call_args = dict(pseudo_call.arguments or {})
        if not result_text:
            _reject_text = (
                f'❌ REJECTED: {terminal_name}() called with empty text. Your visible content MUST go '
                f'in the {terminal_name} tool\'s `text` parameter, NOT in free prose outside tool calls. '
                'Free prose is never delivered to the user. Put your actual answer/question/data '
                f'in text and call {terminal_name} again.'
            )
            _emit_pseudo_tool_result(ls, pseudo_call, _reject_text)
            # [AutoC 2026-07-27] Why: _emit_pseudo_tool_result only writes to ls.messages
            # and JSONL; the live frontend never sees the rejection, leaving the tool card
            # spinning forever. How: emit tool_call_end so the reducer can close the card.
            # Purpose: live rendering matches history reconstruction for rejected finish.
            await ls.rctx.emit_event("tool_call_end", {
                "tool_call_id": _call_id,
                "tool_name": terminal_name,
                "status": "error",
                "rejected": True,
                "rejection_code": "empty_text",
                "error": f"{terminal_name}() called with empty text",
                "raw_inline": _reject_text,
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            return None

        # [AutoC 2026-06-10] Why: output_rejected needs to resume the entry node
        # with a formatter-paired result for the exact finish call. How: persist
        # terminal pseudo-tool call_id in the task runtime tool_call_log only after
        # the terminal call is valid. Purpose: supervisor can carry the latest real
        # _finish_tool_call_id back in resume_data.
        ls.rctx.tool_call_log.append({
            "name": terminal_name,
            "call_id": _call_id,
            "args_summary": str(_call_args)[:200],
        })

        ctx_ref = _persist_ctx(ls, step + 1)
        summary_text = str(args.get("summary") or "").strip()
        final_atts = []
        if terminal_name == "finish":
            _selected_paths = args.get("attachment_paths")
            if isinstance(_selected_paths, list) and _selected_paths:
                final_atts = _select_attachments(
                    ls.collected_attachments, _selected_paths,
                    workspace_root=ls.rctx.workspace_root,
                    session_id=ls.rctx.session_id,
                )

        # [2026-06-10] Emit tool_call_start so the frontend can split a new card
        # (allToolsTerminal triggers card break). Do NOT emit tool_call_end here:
        # finish is async — the task may be handed to a caller chain that asks
        # back. outbound_message (emitted by runner after TaskAction) is the real
        # completion signal; the frontend closes the message there.
        _tool_start_payload: dict[str, Any] = {
            "tool_call_id": _call_id,
            "tool_name": terminal_name,
            "arguments": _call_args,
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        }
        if final_atts:
            # [AutoC 2026-06-17] Why: live web rendering may receive the terminal
            # tool start before the later outbound_message, and older supervisors may
            # not mirror finish attachments on outbound. How: include the resolved
            # attachment metadata on the control tool event as well. Purpose: the
            # reducer can recover final finish attachments from the owning tool card.
            _tool_start_payload["attachments"] = list(final_atts)
        await ls.rctx.emit_event("tool_call_start", _tool_start_payload)
        _tool_meta_patch: dict[str, Any] = {
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "tool_result_raw_inline": "ok",
            "tool_result_format": "text",
            "tool_result_summary": summary_text,
        }
        if final_atts:
            _tool_meta_patch["attachments"] = list(final_atts)
            _tool_meta_patch["tool_result_attachments"] = list(final_atts)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok", meta_patch=_tool_meta_patch)

        result_payload = {
            "summary": summary_text,
            "text": result_text,
        }
        if terminal_name == "finish":
            result_payload["attachments"] = final_atts
        return TaskAction(
            action=ACTION_ASK if terminal_name == "ask" else ACTION_FINISH,
            node_id=ls.node.id,
            result=result_payload,
            context_ref=ctx_ref,
            summary=_short(summary_text, 240) if summary_text else "",
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    # switch_node 也需要 ctx_ref，单独计算
    if pseudo_call.name == "switch_node":
        switch_atts = list(ls.tool_produced_attachments)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok", meta_patch={
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "attachments": switch_atts,
            "tool_result_attachments": switch_atts,
            "tool_result_raw_inline": "ok",
            "tool_result_format": "text",
            "tool_result_summary": "switch_node",
        })
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        # [Fork/Merge 2026-05-17] Why: switch_node changes the entry node for
        # future inbound messages, which are keyed by the parent conversation
        # session, not the temporary branch runtime session. How: prefer
        # rctx.parent_session_id for the supervisor endpoint. Purpose: node
        # switching remains effective after the current branch is merged/cleaned.
        route_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
        try:
            await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/switch_node",
                json={"target_node_id": switch_target},
            )
        except Exception:
            pass
        await ls.rctx.emit_event("node_switch", {
            "target_node_id": switch_target,
            "node_id": ls.node.id,
        })
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": switch_text,
                "attachments": switch_atts,
            },
            context_ref=ctx_ref,
            summary=f"switch → {switch_target or 'default'}",
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    return None  # 未知伪工具，按非终止处理


async def _handle_pseudo_compact(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理 compact_context 伪工具。可能返回 DISPATCH action。"""
    _manual_keep = ls.compact_keep_recent
    try:
        _kr_arg = pseudo_call.arguments.get("keep_recent")
        if _kr_arg is not None:
            _manual_keep = int(_kr_arg)
    except (AttributeError, TypeError, ValueError):
        pass

    # [2026-06-06] Why: compact_context should no longer own prompt filtering or
    # dispatch payload construction. How: compute only the durable target session
    # from runtime metadata and delegate planning to engine.compact_flow. Purpose:
    # the pseudo tool stays a thin wrapper around the shared compact planner.
    target_session_id = str(
        getattr(ls.rctx, "child_session_id", "")
        or getattr(ls.rctx, "parent_session_id", "")
        or getattr(ls.rctx, "session_id", "")
        or ""
    ).strip()

    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step, "manual": True})
        plan = await prepare_compaction(
            messages=ls.messages,
            target_session_id=target_session_id,
            node_id=ls.node.id,
            keep_recent=_manual_keep,
        )
        if plan.reason.startswith("truncated:"):
            try:
                original_chars = int(plan.reason.split(":", 1)[1])
            except (IndexError, ValueError):
                original_chars = 0
            await ls.rctx.emit_event("ptl_truncated", {
                "node_id": ls.node.id,
                "step": step,
                "original_chars": original_chars,
            })
        if plan.status == "dispatch":
            # Dispatch 路径：写 tool_result 后退出循环
            _emit_pseudo_tool_result(ls, pseudo_call, "compacting...")
            ctx_ref = _persist_ctx(ls, step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input=plan.dispatch_input,
            )
        if plan.status == "skip":
            _emit_pseudo_tool_result(ls, pseudo_call, f"skipped: {plan.reason or 'no compressible content'}")
        else:
            await ls.rctx.emit_event("compact_failed", {
                "node_id": ls.node.id,
                "step": step,
                "error": plan.reason,
            })
            _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {plan.reason}")
    except Exception as compact_err:
        await ls.rctx.emit_event("compact_failed", {"node_id": ls.node.id, "step": step, "error": str(compact_err)})
        _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {compact_err}")
    return None


# [2026-05-28] 判断字符串是否像 UUID 或 hex 前缀（task_id 支持前缀匹配）。
# 为什么：preempt_task 新增 node_id 支持，需要区分传入值是 task_id 还是 node_id。
# 怎么判断：全 hex+连字符且长度≥8 视为 task_id/前缀，否则视为 node_id。
import re as _re
_UUID_FULL_PATTERN = _re.compile(r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$', _re.I)

def _looks_like_task_id(s: str) -> bool:
    """Return True if s looks like a UUID or hex prefix (for task_id prefix match)."""
    if _UUID_FULL_PATTERN.fullmatch(s):
        return True
    # hex prefix: all hex chars (plus optional dashes), length >= 8
    return len(s) >= 8 and all(c in '0123456789abcdef-' for c in s.lower())


async def _handle_pseudo_preempt_task(ls: _LoopState, pseudo_call, args: dict) -> None:
    """处理 preempt_task 伪工具。始终返回 None（非终止）。"""
    _pt_tid = str(args.get("task_id") or "").strip()
    _pt_msg = str(args.get("message") or "").strip()
    if _pt_tid:
        try:
            # [2026-05-28] 支持按 node_id 查找活跃任务再 preempt。
            # 为什么：调用方通常只知道子节点名（如 "bob"），不知道动态 task_id。
            # 怎么改：如果传入值不像 UUID/hex 前缀，视为 node_id，先查出真实 task_id。
            # 目的：简化 preempt 调用，无需事先查询 task_id。
            real_task_id = _pt_tid
            if not _looks_like_task_id(_pt_tid):
                # 当作 node_id 处理，查询当前 session 中该节点的活跃任务
                _route_sid = ls.rctx.parent_session_id or ls.rctx.session_id
                _node_resp = await ls.rctx.http.get(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{_route_sid}/tasks/by-node/{_pt_tid}",
                )
                if _node_resp.status_code == 200:
                    _node_data = _node_resp.json()
                    real_task_id = _node_data.get("task_id", "")
                elif _node_resp.status_code == 404:
                    # [2026-05-28] fallback 到全局 by-node 查找。
                    # 为什么：持久节点的任务运行在独立 session 上，
                    #   session 内查找找不到。
                    # 怎么改：404 时 fallback 到 /v1/tasks/active-by-node/{node_id}。
                    # 目的：跨 session 支持 preempt 持久节点任务。
                    _global_resp = await ls.rctx.http.get(
                        f"{ls.rctx.supervisor_url}/v1/tasks/active-by-node/{_pt_tid}",
                    )
                    if _global_resp.status_code == 200:
                        _global_data = _global_resp.json()
                        real_task_id = _global_data.get("task_id", "")
                    else:
                        _pt_result = f"节点 '{_pt_tid}' 在当前 session 及全局范围内均无活跃任务"
                        _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                        return None
                else:
                    _pt_result = f"查询节点 '{_pt_tid}' 的任务失败: HTTP {_node_resp.status_code}"
                    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                    return None

            # [2026-05-23] Pass message to supervisor so preempt can inject
            # additional instructions instead of just stopping the child.
            _pt_body: dict[str, Any] = {}
            if _pt_msg:
                _pt_body["message"] = _pt_msg
            _pt_resp = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/tasks/{real_task_id}/preempt",
                json=_pt_body if _pt_body else None,
            )
            _display_id = _pt_tid if _looks_like_task_id(_pt_tid) else f"{_pt_tid}({real_task_id[:8]})"
            if _pt_resp.status_code == 200:
                if _pt_msg:
                    _pt_result = f"已向 task {_display_id} 注入追加指令，子任务继续执行"
                else:
                    _pt_result = f"已标记 task {_display_id} 为 preempt，等待优雅退出"
            elif _pt_resp.status_code == 404:
                _pt_result = f"task {_display_id} 不存在或已结束"
            else:
                _pt_result = f"API 返回 {_pt_resp.status_code}"
        except Exception as _pt_e:
            _pt_result = f"调用失败 {_pt_e}"
    else:
        _pt_result = "task_id 不能为空"
    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
    return None


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch:{target_id} 动态伪工具。始终返回 None（非终止）。

    [2026-05-28] 所有异步 dispatch 统一走 inbound 路径。
    为什么：让异步子节点复用现有的 session / entry-branch / conversation 机制，
    而不是用单独的 dispatch-async 端点。
    怎么改：构造 inbound payload（含 dispatch_origin 回调信息），POST /v1/inbound。
    目的：统一生命周期管理，为 persistent node 支持铺路。
    """
    target = str(args.get("target") or "").strip()
    instr = str(args.get("instruction") or "").strip()
    _raw_ctx_mode = args.get("context_mode")
    if _raw_ctx_mode is not None:
        ctx_mode = str(_raw_ctx_mode).strip()
    else:
        ctx_mode = "accumulate"
    ctx_key = str(args.get("context_key") or "").strip() or None
    attachment_paths = args.get("attachment_paths") or []
    attachments = _paths_to_attachments(attachment_paths, ls.rctx.workspace_root, session_workspace=ls.rctx.workspace)

    # [AutoC 2026-06-06] Why: persistent nodes (like Smith, Scout) are designed
    # to accumulate context across tasks, but callers can accidentally pass
    # context_mode="fresh" which creates a new session every time, causing
    # session proliferation. How: load the target node config and force
    # accumulate mode when the node declares persistent=true. Purpose: enforce
    # the node author's persistence intention regardless of caller behavior.
    if ctx_mode != "accumulate":
        from ..node import load_node as _load_node
        _target_node = _load_node(Path(ls.rctx.workspace_root), target)
        if _target_node is not None and getattr(_target_node, "persistent", False):
            import logging as _lg
            _lg.getLogger(__name__).info(
                "dispatch to persistent node %r: overriding context_mode %r -> accumulate",
                target, ctx_mode,
            )
            ctx_mode = "accumulate"

    # 获取父 session 信息
    parent_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
    # [2026-05-29 方案C第一步] 为什么：conversation_key 同时承担存储身份和
    # 路由可见性时，嵌套 dispatch 会生成多层 agent: 前缀，EventRouter 只能反解析
    # 字符串来找父频道。怎么改：优先读取 task_context.route_conversation_key，
    # 没有时才使用当前 task 的原 conversation_key。目的：所有嵌套子任务都保留
    # 根父频道的原始 conversation_key，后续 SDK 可直接按结构化字段路由。
    parent_conv_key = str(
        ls.rctx.task_context.get("route_conversation_key")
        or ls.rctx.task_context.get("conversation_key")
        or ""
    ).strip()
    parent_channel = ls.rctx.task_context.get("channel", "internal")
    target_node_id = target

    # 生成 conversation_key
    if ctx_mode in ("fresh", "fork"):
        import uuid as _uuid
        conv_key = f"agent:{target_node_id}:{parent_conv_key}:{_uuid.uuid4()}"
    else:  # accumulate
        if ctx_key:
            conv_key = f"agent:{target_node_id}:{ctx_key}:{parent_conv_key}"
        else:
            conv_key = f"agent:{target_node_id}:{parent_conv_key}"

    # 构造 inbound payload
    inbound_payload: dict[str, Any] = {
        "channel": parent_channel,
        "conversation_key": conv_key,
        "text": instr,
        "entry_node_id": target_node_id,
        "use_context": True,
        "attachments": attachments or [],
        "dispatch_origin": {
            "parent_session_id": parent_session_id,
            "caller_node_id": ls.node.id,
            # [2026-05-29 方案C第一步] 为什么：审批和子进度事件只带子 session，
            # 旧 SDK 只能从 agent: conversation_key 反解析父频道，容易误判或丢事件。
            # 怎么改：把父频道的原始 conversation_key 与上下文模式随 dispatch_origin
            # 一起下发。目的：task_created 事件可直接携带路由元数据，无需字符串猜测。
            "parent_conversation_key": parent_conv_key,
            "context_mode": ctx_mode,
        },
        "dispatch_context_mode": ctx_mode,
    }
    if ctx_mode == "fork":
        inbound_payload["dispatch_fork_from_session"] = parent_session_id

    # [2026-06-06] Why: dispatch pseudo-tools did not emit tool_call_start/end
    # WebSocket events, so the frontend could not render ToolCallCards during
    # streaming. How: emit the same event pair as real tools. Purpose: dispatch
    # tool calls appear in the chat stream with name, args, and result.
    _call_id = getattr(pseudo_call, "id", "") or ""
    _call_args = dict(pseudo_call.arguments or {})
    await ls.rctx.emit_event("tool_call_start", {
        "tool_call_id": _call_id,
        "tool_name": pseudo_call.name,
        "arguments": _call_args,
    })

    try:
        _dispatch_resp = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/inbound",
            json=inbound_payload,
            timeout=10.0,
        )
        if _dispatch_resp.status_code == 200:
            _d_data = _dispatch_resp.json()
            _dispatch_result = json.dumps({
                "success": True,
                "session_id": _d_data.get("session_id", ""),
                "message": f"已异步委派给 {target}",
            }, ensure_ascii=False)
        else:
            _dispatch_result = json.dumps({
                "success": False,
                "error": f"inbound API 返回 {_dispatch_resp.status_code}: {_dispatch_resp.text}",
            }, ensure_ascii=False)
    except Exception as e:
        _dispatch_result = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    await ls.rctx.emit_event("tool_call_end", {
        "tool_call_id": _call_id,
        "tool_name": pseudo_call.name,
        "status": "success" if '"success": true' in _dispatch_result else "error",
        "result": _dispatch_result,
    })
    _emit_pseudo_tool_result(ls, pseudo_call, _dispatch_result)
    return None
