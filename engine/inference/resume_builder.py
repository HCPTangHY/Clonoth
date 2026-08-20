"""恢复消息构建和附件筛选。

从 ai_step.py 中拆出。依赖 engine.attachments 中的 build_multimodal_content。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..attachments import build_multimodal_content
from ..compact import record_compact_success
from ..conversation_store import Message
from ..signals import Signal, get_bus
from .message_model import MessageMeta, set_message_meta
from .tool_format import ParsedToolCall

logger = logging.getLogger(__name__)


def _attachments_from_payload(payload: Any) -> list[Any]:
    # [AutoC 2026-05-31] Why: resume reconstruction can receive tool or child
    # payloads from before and after the ok/data/error migration. How: prefer
    # data.attachments and fall back to the legacy top-level attachments list.
    # Purpose: keep generated files visible after a task resumes.
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    legacy = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    return list(nested or legacy)


# ---------------------------------------------------------------------------
#  恢复消息构建（同时兼容 v1 和 v2 格式）
# ---------------------------------------------------------------------------

def _build_resume_messages(resume_data: dict[str, Any]) -> list[dict[str, Any]]:
    """从 resume_data / resume_event 构建恢复消息。

    v2 格式:
      - child_result:    下级节点完成
      - child_failed:    下级节点失败
      - child_cancelled: 下级节点被取消
    v1 兼容:
      - tool_results:    工具调用结果
      - handoff_result:  子链返回结果
    """
    rtype = str(resume_data.get("type") or "").strip()

    # v2: child_result
    if rtype == "child_result":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        summary = str(result.get("summary") or "")
        text = str(result.get("text") or "")
        child_atts = _attachments_from_payload(result)
        lines = [f"下游节点 {from_node} 已完成。" if from_node else "下游节点已完成。"]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append("结果：")
            lines.append(text)
        content_text = "\n".join(lines).strip()
        if isinstance(child_atts, list) and child_atts:
            return [{"role": "user", "content": build_multimodal_content(content_text, child_atts)}]
        return [{"role": "user", "content": content_text}]

    # v2: child_ask
    if rtype == "child_ask":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        text = str(result.get("text") or "").strip()
        if from_node and text:
            content = f"下游节点 {from_node} 需要补充信息：{text}"
        elif text:
            content = f"下游节点需要补充信息：{text}"
        elif from_node:
            content = f"下游节点 {from_node} 需要补充信息。"
        else:
            content = "下游节点需要补充信息。"
        return [{"role": "user", "content": content}]

    # v2: output_rejected (output chain downstream rejected upstream finish)
    if rtype == "output_rejected":
        # [AutoC 2026-06-10] Why: reject must present as the finish tool's result
        # through the real formatter pipeline (native/fake-native/json). How: return
        # empty here; ai_step handles output_rejected by injecting a properly
        # formatted tool result via the node's formatter. Purpose: API-level
        # tool_call_id pairing stays correct across all tool_mode variants.
        return []

    # v2: child_failed
    if rtype == "child_failed":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        error = str(resume_data.get("error") or "未知错误")
        prefix = f"下游节点 {from_node} 执行失败：" if from_node else "下游节点执行失败："
        return [{"role": "user", "content": f"{prefix}{error}"}]

    # v2: child_cancelled
    if rtype == "child_cancelled":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        text = f"下游节点 {from_node} 已被取消。" if from_node else "下游节点已被取消。"
        return [{"role": "user", "content": text}]

    # v2: child_preempted
    if rtype == "child_preempted":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        ctx_ref = str(resume_data.get("context_ref") or "")
        prefix = f"下游节点 {from_node} 被打断，上下文已保存。" if from_node else "下游节点被打断，上下文已保存。"
        if ctx_ref:
            prefix += f"（context_ref: {ctx_ref}）"
        return [{"role": "user", "content": prefix}]

    # v2: compact_done (compactor 子 task 完成后恢复)
    if rtype == "compact_done":
        success = resume_data.get("success", True)
        # 压缩只动上方旧消息，当前 task 消息链完整保留，LLM 无需感知。
        # 注入假 user 消息反而会破坏工具调用的角色链。
        return []

    # v1: tool_results
    if rtype == "tool_results":
        entries = resume_data.get("tool_results")
        if not isinstance(entries, list):
            entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = []
            all_atts: list[dict[str, Any]] = [] 
            for e in entries:
                _name = e.get("name", "unknown")
                _raw = e.get("raw_inline", "")
                msgs.append({"role": "user", "content": f'Tool result for "{_name}":\n{_raw}'})
                atts = _attachments_from_payload(e)
                if atts:
                    all_atts.extend(atts)
            if all_atts:
                msgs.append({"role": "user", "content": build_multimodal_content("以上工具执行产生了以下图片结果：", all_atts)})
            return msgs
        return []

    # v3: batch_results（统一批量返回，node 和 tool 共用）
    if rtype == "batch_results":
        entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = []
            all_atts: list[dict[str, Any]] = []
            for e in entries:
                _kind = str(e.get("kind") or "node")
                _status = str(e.get("status") or "")

                if _kind == "tool":
                    _name = e.get("name", "unknown")
                    _raw = e.get("raw_inline", "")
                    if _status == "fail":
                        msgs.append({"role": "user", "content": f'Tool "{_name}" 执行失败：{e.get("error", "")}'})    
                    else:
                        msgs.append({"role": "user", "content": f'Tool result for "{_name}":\n{_raw}'})
                else:
                    _node = str(e.get("node_id") or "unknown")
                    _instr = str(e.get("instruction") or "")
                    _text = str(e.get("text") or "")
                    _summary = str(e.get("summary") or "")
                    if _status == "fail":
                        msgs.append({"role": "user", "content": f"子节点 {_node} 执行失败：{e.get('error', '')}"})    
                    else:
                        lines = [f"子节点 {_node}（指令：{_instr[:100]}）已完成。"]
                        if _summary:
                            lines.append(f"摘要：{_summary}")
                        if _text:
                            lines.append(f"结果：\n{_text}")
                        msgs.append({"role": "user", "content": "\n".join(lines)})

                atts = _attachments_from_payload(e)
                if atts:
                    all_atts.extend(atts)
            if all_atts:
                msgs.append({"role": "user", "content": build_multimodal_content("批量执行产生了以下图片结果：", all_atts)})
            return msgs
        return []

    return []


# ---------------------------------------------------------------------------
#  附件筛选
# ---------------------------------------------------------------------------

def _select_attachments(
    collected: list[dict[str, Any]],
    selected_paths: Any,
    workspace_root: "Path | None" = None,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Select attachments by path from collected, or read from disk as fallback.

    Disk fallback is restricted to paths under workspace_root for security.
    """
    if not isinstance(selected_paths, list) or not selected_paths:
        return collected

    path_set = {str(p).strip() for p in selected_paths if isinstance(p, str) and str(p).strip()}

    def _attachment_path(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("path") or "").strip()
        if isinstance(item, str):
            return item.strip()
        return ""

    def _attachment_from_path(path: str) -> dict[str, Any]:
        suffix = Path(path).suffix.lower()
        image_exts = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
        return {
            "path": path,
            "name": Path(path).name or "附件",
            "type": "image" if suffix in image_exts else "file",
        }

    selected = [a if isinstance(a, dict) else _attachment_from_path(str(a).strip()) for a in collected if _attachment_path(a) in path_set]
    found_paths = {_attachment_path(a) for a in selected}

    if workspace_root:
        from ..attachments import save_attachment
        for raw in sorted(path_set - found_paths):
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = workspace_root / p
            # Security: only allow paths within workspace
            try:
                p.resolve().relative_to(workspace_root.resolve())
            except ValueError:
                continue
            if not p.is_file():
                continue
            try:
                data_bytes = p.read_bytes()
            except Exception:
                continue
            att = save_attachment(workspace_root, session_id, data_bytes, filename=p.name)
            selected.append(att)

    return selected if selected else collected


# ---------------------------------------------------------------------------
#  [AutoC 2026-08-20] 从 ai_step.py 迁入：恢复期附件收集与
#  output_rejected / compact_done 两类特殊 resume 的处理。
#  ai_step 只保留 resume 入口调用，不再知道具体修复逻辑。
# ---------------------------------------------------------------------------

def collect_resume_attachments(
    attachments: list[dict[str, Any]] | None,
    resume_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Collect inbound and resume-carried attachments for the new run.

    Why: the attachment sources (task input, resumed tool entries, resume
    payloads, final results) were inlined in run_ai_node. How: iterate the same
    nested fallback chain here and return one merged list. Purpose: the entry
    function keeps only loop construction.
    """
    collected: list[dict[str, Any]] = []
    if attachments:
        collected.extend(attachments)
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
                    collected.extend(e_atts)
        if isinstance(resume_data.get("attachments"), list):
            collected.extend(resume_data["attachments"])
        rd = resume_data.get("result")
        if isinstance(rd, dict):
            # [AutoC 2026-05-31] Why: final result payloads can also be migrated to
            # data.attachments. How: prefer nested attachments and fall back to the
            # old result.attachments list. Purpose: keep finish-time attachments
            # selectable after a resume.
            rd_data = rd.get("data") if isinstance(rd.get("data"), dict) else {}
            rd_atts = rd_data.get("attachments") if isinstance(rd_data.get("attachments"), list) else rd.get("attachments")
            if isinstance(rd_atts, list):
                collected.extend(rd_atts)
    return collected


def _apply_output_rejected(
    *,
    messages: list[dict[str, Any]],
    resume_data: dict[str, Any],
    formatter: Any,
    node: Any,
    rctx: Any,
) -> None:
    """Inject a QA rejection into the resumed message history.

    Why: output_rejected resume is a message-repair concern, not loop mechanics.
    How: detect implicit (hybrid free prose) versus explicit finish rejections;
    either append a user-role reject line or replace the paired finish
    tool_result, persisting both to ConversationStore. Purpose: run_ai_node
    keeps only the resume entry point.
    """
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
                _store.append(_target_sid, Message(
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


async def _emit_compact_done(
    *,
    resume_data: dict[str, Any],
    node: Any,
    rctx: Any,
) -> None:
    """Reset the compaction breaker and emit compact_done signal/event."""
    # [2026-04-24] P1.5 熔断器：压缩成功时重置失败计数
    # [AutoC 2026-05-13] Why: compaction may have targeted the parent session
    # while the task resumed on a branch. How: reset the breaker on
    # parent_session_id when present. Purpose: success accounting stays
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


async def apply_resume_messages(
    *,
    messages: list[dict[str, Any]],
    resume_data: dict[str, Any],
    formatter: Any,
    node: Any,
    rctx: Any,
) -> None:
    """Append resume messages and run output_rejected / compact_done handling.

    Why: run_ai_node should own the resume decision (when to resume), not the
    repair logic (how each resume type mutates messages). How: extend the
    message list with rebuilt resume messages and dispatch the two special
    resume types by name. Purpose: keep the entry function slim while resume
    semantics stay in the resume module.
    """
    messages.extend(_build_resume_messages(resume_data))
    _rtype = str(resume_data.get("type") or "")
    if _rtype == "output_rejected":
        _apply_output_rejected(
            messages=messages, resume_data=resume_data,
            formatter=formatter, node=node, rctx=rctx,
        )
    elif _rtype == "compact_done":
        await _emit_compact_done(resume_data=resume_data, node=node, rctx=rctx)
