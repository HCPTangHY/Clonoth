"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具、dispatch_to 与向后兼容别名伪工具的执行逻辑。
"""
from __future__ import annotations

import copy
import json
import mimetypes as _mimetypes
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .resume_builder import _select_attachments
from ..compact_flow import prepare_compaction
from ..protocol import TaskAction, ACTION_ASK, ACTION_DISPATCH, ACTION_FINISH
from .loop_state import _LoopState, _short
from providers.tool_format import ParsedToolCall
from .pseudo_tools import _dispatch_target_from_tool_name
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType


def _persist_ctx(ls: _LoopState, step: int) -> str:
    from .ai_step import _persist_ctx as persist
    return persist(ls, step)


def _paths_to_attachments_with_missing(paths: list, workspace_root: Path) -> tuple[list[dict], list[str]]:
    """Convert file paths to attachment dicts and return (attachments, missing_paths)."""
    from ..attachments import inspect_raster_image_bytes

    result = []
    missing = []
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
            continue
        full = Path(p_str) if Path(p_str).is_absolute() else workspace_root / p_str
        full = full.resolve()
        if not full.exists() or not full.is_file():
            missing.append(p_str)
            continue
        mime = _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        is_svg = full.suffix.lower() == ".svg" or mime.lower() == "image/svg+xml"
        att_type = "file"
        if not is_svg:
            try:
                if full.stat().st_size <= 25 * 1024 * 1024:
                    real_mime, _ = inspect_raster_image_bytes(full.read_bytes())
                    if real_mime:
                        att_type = "image"
                        mime = real_mime
            except Exception:
                pass
        result.append({
            "type": att_type,
            "path": str(full),
            "mime_type": mime,
            "name": full.name,
        })
    return result, missing


def _paths_to_attachments(paths: list, workspace_root: Path) -> list[dict]:
    """Convert file paths to attachment dicts for dispatch."""
    atts, _ = _paths_to_attachments_with_missing(paths, workspace_root)
    return atts


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
    """统一写入伪工具的 tool_result，确保 native 模式下 tool_use/tool_result 配对完整。"""
    from .ai_step import _shadow_write
    _parsed = ParsedToolCall(
        id=getattr(pseudo_call, "id", "") or "",
        name=pseudo_call.name,
        arguments=dict(pseudo_call.arguments or {}),
    )
    tool_msg = ls.formatter.format_tool_result(_parsed, content)
    if control_tool_name:
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
        _reply_att_paths = args.get("attachment_paths") or args.get("attachments") or []
        _reply_atts, _missing_paths = (
            _paths_to_attachments_with_missing(_reply_att_paths, ls.rctx.workspace_root)
            if _reply_att_paths else ([], [])
        )
        if reply_text or _reply_atts:
            _reply_payload: dict[str, Any] = {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
            }
            if _reply_atts:
                _reply_payload["attachments"] = _reply_atts
            await ls.rctx.emit_event("intermediate_reply", _reply_payload)
        if _missing_paths:
            _warn_msg = (
                f"ok (warning: attachment file not found on disk and was not attached: {', '.join(_missing_paths)})"
                if (reply_text or _reply_atts)
                else f"error: intermediate_reply had no text and attachment file was not found on disk: {', '.join(_missing_paths)}"
            )
            _emit_pseudo_tool_result(ls, pseudo_call, _warn_msg)
        else:
            _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # Record substantive delegation/worker pseudo-tools into tool_call_log
    _fixed_dispatch_target = _dispatch_target_from_tool_name(pseudo_call.name)
    if pseudo_call.name in ("manage_workers", "spawn_worker", "dispatch_to") or _fixed_dispatch_target:
        _tc_log = getattr(getattr(ls, "rctx", None), "tool_call_log", None)
        if isinstance(_tc_log, list):
            _tc_log.append({
                "name": pseudo_call.name,
                "call_id": str(getattr(pseudo_call, "id", "") or ""),
                "args_summary": str(args)[:200],
            })

    # manage_workers is the only worker-control entry point. The retired
    # preempt_task / cancel_active_tasks have no handler and no alias: such a call is
    # rejected upstream as a nonexistent tool, so it can never reach a HTTP mutation.
    if pseudo_call.name == "manage_workers":
        return await _handle_manage_workers(ls, args, pseudo_call)

    # spawn_worker: non-terminal anonymous delegation
    if pseudo_call.name == "spawn_worker":
        return await _handle_spawn_worker(ls, args, pseudo_call)

    # dispatch_to: unified dynamic delegation tool
    if pseudo_call.name == "dispatch_to":
        return await _handle_pseudo_dispatch(ls, args, pseudo_call)

    # Legacy dispatch_to_{target_id} compatibility
    if _fixed_dispatch_target:
        return await _handle_pseudo_dispatch(ls, {**args, "target": _fixed_dispatch_target}, pseudo_call)

    # ---- 终止型伪工具：finish / ask / switch_node ----
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

        ls.rctx.tool_call_log.append({
            "name": terminal_name,
            "call_id": _call_id,
            "args_summary": str(_call_args)[:200],
        })

        ctx_ref = _persist_ctx(ls, step + 1)
        final_atts = []
        if terminal_name == "finish":
            _selected_paths = args.get("attachment_paths") or args.get("attachments")
            if isinstance(_selected_paths, list) and _selected_paths:
                final_atts = _select_attachments(
                    ls.collected_attachments, _selected_paths,
                    workspace_root=ls.rctx.workspace_root,
                    session_id=ls.rctx.session_id,
                )

        _tool_start_payload: dict[str, Any] = {
            "tool_call_id": _call_id,
            "tool_name": terminal_name,
            "arguments": _call_args,
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        }
        if final_atts:
            _tool_start_payload["attachments"] = list(final_atts)
        await ls.rctx.emit_event("tool_call_start", _tool_start_payload)
        _tool_meta_patch: dict[str, Any] = {
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "tool_result_raw_inline": "ok",
            "tool_result_format": "text",
        }
        if final_atts:
            _tool_meta_patch["attachments"] = list(final_atts)
            _tool_meta_patch["tool_result_attachments"] = list(final_atts)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok", meta_patch=_tool_meta_patch)

        result_payload = {
            "text": result_text,
        }
        if terminal_name == "finish":
            result_payload["attachments"] = final_atts
        return TaskAction(
            action=ACTION_ASK if terminal_name == "ask" else ACTION_FINISH,
            node_id=ls.node.id,
            result=result_payload,
            context_ref=ctx_ref,
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
        })
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
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


async def _handle_spawn_worker(ls: _LoopState, args: dict, pseudo_call) -> None:
    """Create an anonymous ephemeral sub-agent through shared dispatch handling."""
    instr = str(args.get("instruction") or "").strip()
    if not instr:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "instruction is required",
        }, ensure_ascii=False))
        return None

    ctx_mode = str(args.get("context_mode") or "fork").strip()
    if ctx_mode not in ("fresh", "fork"):
        ctx_mode = "fork"

    template = str(args.get("template") or "_worker").strip() or "_worker"
    raw_attachment_paths = args.get("attachment_paths") or []
    attachment_paths = raw_attachment_paths if isinstance(raw_attachment_paths, list) else []

    dispatch_args = {
        "target": template,
        "instruction": instr,
        "context_mode": ctx_mode,
        "attachment_paths": attachment_paths,
    }
    return await _handle_pseudo_dispatch(ls, dispatch_args, pseudo_call)


# Active child statuses mirror RunContext.list_active_child_tasks: only tasks that can still
# be messaged/preempted/cancelled count as workers of this conversation.
_ACTIVE_CHILD_STATUSES = frozenset({"pending", "running", "suspended", "awaiting_approval"})


def _scoped_active_children(tasks: list[Any], caller_task_id: str) -> list[dict[str, Any]]:
    """Keep only this conversation's live child tasks, never the caller's own task.

    Discovery (RunContext.list_active_child_tasks) already applies this predicate; it is
    re-applied here so a raw or partially filtered list cannot smuggle in the caller, a
    terminal task, or a task outside the current conversation's target range.
    """
    caller = str(caller_task_id or "").strip()
    scoped: dict[str, dict[str, Any]] = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        status = str(task.get("status") or "").strip()
        if not task_id or task_id == caller or status not in _ACTIVE_CHILD_STATUSES:
            continue
        scoped[task_id] = dict(task)
    return list(scoped.values())


def _resolve_active_child(
    tasks: list[dict[str, Any]], requested: str
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a full/unique-prefix task id OR node_id within the active child set.

    Returns ``(task, "")`` on success and ``(None, error)`` with an explicit ambiguity or
    empty-result error otherwise: guessing among several children used to widen the blast
    radius of a cancel, so ambiguity is now a rejection instead of a last-match pick.
    """
    needle = str(requested or "").strip()
    if not needle:
        return None, "target is required"
    id_matches: dict[str, dict[str, Any]] = {}
    for task in tasks or []:
        task_id = str(task.get("task_id") or "").strip()
        if task_id and task_id.startswith(needle):
            id_matches[task_id] = task
    if len(id_matches) == 1:
        return next(iter(id_matches.values())), ""
    if len(id_matches) > 1:
        return None, (
            f"ambiguous task_id prefix '{needle}' in the current conversation "
            f"({len(id_matches)} active child tasks match); pass a longer task_id"
        )
    node_matches: dict[str, dict[str, Any]] = {}
    for task in tasks or []:
        task_id = str(task.get("task_id") or "").strip()
        if task_id and str(task.get("node_id") or "").strip() == needle:
            node_matches[task_id] = task
    if len(node_matches) == 1:
        return next(iter(node_matches.values())), ""
    if len(node_matches) > 1:
        return None, (
            f"ambiguous node '{needle}' in the current conversation "
            f"({len(node_matches)} active child tasks); pass the task_id"
        )
    return None, f"no active child task for '{needle}' in the current conversation"


def _node_filter_mismatch(task: dict[str, Any], explicit_node_id: Any) -> str:
    """Reject an explicit node_id filter that contradicts the resolved child task."""
    wanted = str(explicit_node_id or "").strip()
    if not wanted:
        return ""
    actual = str(task.get("node_id") or "").strip()
    if actual == wanted:
        return ""
    return (
        f"node_id '{wanted}' does not match the resolved child task's node '{actual}' "
        "in the current conversation"
    )


def _supervisor_failure(response: Any) -> str:
    """Return a failure message if the supervisor call failed or reported failure in a 2xx body."""
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        return f"API returned {status}: {getattr(response, 'text', '')}"
    try:
        payload = response.json()
    except Exception:
        return ""
    if isinstance(payload, dict) and (payload.get("ok") is False or payload.get("success") is False):
        return str(payload.get("error") or payload.get("detail") or "supervisor reported failure")
    return ""


def _task_elapsed_seconds(created_at: Any) -> int:
    """Return elapsed whole seconds for an ISO timestamp; 0 when absent or unparseable."""
    raw = str(created_at or "").strip()
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return 0


async def _handle_manage_workers(ls: _LoopState, args: dict, pseudo_call) -> None:
    """List, message, gracefully preempt, or cancel active child agents of this conversation.

    Every action is scoped to the active child tasks discovered for the routed session.
    There is deliberately no global/by-node fallback: a target that is not an active
    child of the current conversation is an error, never a wider cancel.
    """
    action = str(args.get("action") or "").strip().lower()
    route_session_id = str(
        getattr(ls.rctx, "parent_session_id", "")
        or getattr(ls.rctx, "session_id", "")
        or ""
    ).strip()
    caller_task_id = str(getattr(ls.rctx, "task_id", "") or "").strip()

    def _emit(payload: dict[str, Any]) -> None:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps(payload, ensure_ascii=False))

    def _fail(error: str) -> None:
        _emit({"success": False, "error": error})

    async def _active_tasks() -> list[dict[str, Any]]:
        discover = getattr(ls.rctx, "list_active_child_tasks", None)
        if not callable(discover):
            return []
        result = await discover()
        rows = [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        return _scoped_active_children(rows, caller_task_id)

    def _session_scope_error() -> str:
        """Session-wide cancellation needs a routed session and the caller's own task id."""
        if not route_session_id:
            return "no routed session for this task; session-wide worker control is unavailable"
        if not caller_task_id:
            return (
                "caller task identity is unavailable; refusing a session-wide cancel "
                "that could include this task"
            )
        return ""

    if action == "list":
        node_filter = str(args.get("node_id") or "").strip()
        try:
            tasks = await _active_tasks()
        except Exception as exc:
            _fail(str(exc))
            return None
        task_list: list[dict[str, Any]] = []
        for task in tasks:
            node_id = str(task.get("node_id") or "")
            if node_filter and node_id != node_filter:
                continue
            created_at = str(task.get("created_at") or "")
            task_list.append({
                "task_id": str(task.get("task_id") or "")[:12],
                "node_id": node_id,
                "status": str(task.get("status") or ""),
                "created_at": created_at,
                "elapsed_sec": _task_elapsed_seconds(created_at),
            })
        _emit({"success": True, "active_children": len(task_list), "tasks": task_list})
        return None

    if action in ("message", "preempt"):
        requested_target = str(args.get("target") or args.get("task_id") or args.get("node_id") or "").strip()
        message = str(args.get("message") or "").strip()
        if action == "message" and (not requested_target or not message):
            _fail("task_id and message are required for 'message' action")
            return None
        if action == "preempt" and not requested_target:
            _fail("target (or task_id/node_id) is required for 'preempt' action")
            return None
        try:
            task, resolution_error = _resolve_active_child(await _active_tasks(), requested_target)
        except Exception as exc:
            _fail(str(exc))
            return None
        if resolution_error:
            _fail(resolution_error)
            return None
        mismatch = _node_filter_mismatch(task, args.get("node_id"))
        if mismatch:
            _fail(mismatch)
            return None
        task_id = str(task.get("task_id") or "")
        try:
            post_kwargs: dict[str, Any] = {"timeout": 10.0}
            if message:
                post_kwargs["json"] = {"message": message}
            response = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/tasks/{task_id}/preempt",
                **post_kwargs,
            )
        except Exception as exc:
            _fail(str(exc))
            return None
        failure = _supervisor_failure(response)
        if failure:
            _fail(failure)
            return None
        _emit({
            "success": True,
            "message": (
                f"Instruction injected to {task_id[:12]}" if message
                else f"Graceful preempt requested for {task_id[:12]}"
            ),
        })
        return None

    if action == "cancel":
        requested_target = str(args.get("target") or args.get("task_id") or "").strip()
        explicit_node_id = str(args.get("node_id") or "").strip()
        if explicit_node_id and not requested_target:
            scope_error = _session_scope_error()
            if scope_error:
                _fail(scope_error)
                return None
            try:
                response = await ls.rctx.http.post(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
                    params={"exclude_task_id": caller_task_id, "node_id": explicit_node_id},
                    timeout=10.0,
                )
            except Exception as exc:
                _fail(str(exc))
                return None
            failure = _supervisor_failure(response)
            if failure:
                _fail(failure)
                return None
            _emit({"success": True, "cancelled": f"node:{explicit_node_id}"})
            return None
        if requested_target:
            try:
                task, resolution_error = _resolve_active_child(await _active_tasks(), requested_target)
            except Exception as exc:
                _fail(str(exc))
                return None
            if resolution_error:
                _fail(resolution_error)
                return None
            mismatch = _node_filter_mismatch(task, explicit_node_id)
            if mismatch:
                _fail(mismatch)
                return None
            task_id = str(task.get("task_id") or "")
            try:
                response = await ls.rctx.http.post(
                    f"{ls.rctx.supervisor_url}/v1/tasks/{task_id}/cancel",
                    timeout=10.0,
                )
            except Exception as exc:
                _fail(str(exc))
                return None
            failure = _supervisor_failure(response)
            if failure:
                _fail(failure)
                return None
            _emit({"success": True, "cancelled": task_id[:12]})
            return None
        # Cancel-all stays session-scoped and always excludes this task.
        scope_error = _session_scope_error()
        if scope_error:
            _fail(scope_error)
            return None
        try:
            response = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
                params={"exclude_task_id": caller_task_id},
                timeout=10.0,
            )
        except Exception as exc:
            _fail(str(exc))
            return None
        failure = _supervisor_failure(response)
        if failure:
            _fail(failure)
            return None
        _emit({"success": True, "cancelled": "all"})
        return None

    _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
        "success": False,
        "error": f"Unknown action: {action}. Use 'list', 'message', 'preempt', or 'cancel'.",
    }, ensure_ascii=False))
    return None


def _extract_cache_warm_fork_prefix(ls: _LoopState) -> list[dict[str, Any]]:
    """Extract the exact prefix of ls.messages that was sent to the LLM in the current step.

    Strips any trailing tool results emitted earlier in the current step and the
    current step's assistant tool_call message, plus any _ephemeral messages.
    """
    raw_msgs = getattr(ls, "messages", None)
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return []
    end = len(raw_msgs)
    while end > 0 and isinstance(raw_msgs[end - 1], dict) and raw_msgs[end - 1].get("role") == "tool":
        end -= 1
    if end > 0 and isinstance(raw_msgs[end - 1], dict) and raw_msgs[end - 1].get("role") == "assistant" and raw_msgs[end - 1].get("tool_calls"):
        end -= 1
    prefix: list[dict[str, Any]] = []
    for m in raw_msgs[:end]:
        if not isinstance(m, dict) or m.get("_ephemeral"):
            continue
        prefix.append(copy.deepcopy(m))
    return prefix


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch_to / spawn_worker / dispatch_to_{target_id} 动态伪工具。始终返回 None（非终止）。"""
    target = str(args.get("target") or "").strip()
    if not target:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "target is required",
        }, ensure_ascii=False))
        return None

    if target == "_worker":
        try:
            from engine.builtin.agent_manage import ensure_worker_node_yaml
            ensure_worker_node_yaml(ls.rctx.workspace_root)
        except Exception:
            pass

    instr = str(args.get("instruction") or "").strip()
    if not instr:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "instruction is required",
        }, ensure_ascii=False))
        return None

    _raw_ctx_mode = args.get("context_mode")
    if _raw_ctx_mode is not None:
        ctx_mode = str(_raw_ctx_mode).strip()
    else:
        ctx_mode = "accumulate"
    ctx_key = str(args.get("context_key") or "").strip() or None
    attachment_paths = args.get("attachment_paths") or []
    attachments = _paths_to_attachments(attachment_paths, ls.rctx.workspace_root)

    is_ephemeral_worker = (pseudo_call.name == "spawn_worker" or target == "_worker")

    # Only default persistent nodes to accumulate when NOT an ephemeral worker (spawn_worker)
    # and when the caller did not explicitly request fresh/fork (or when persistent node defaults apply).
    if not is_ephemeral_worker and ctx_mode != "accumulate" and _raw_ctx_mode is None:
        from ..node import load_node as _load_node
        _target_node = _load_node(Path(ls.rctx.workspace_root), target)
        if _target_node is not None and getattr(_target_node, "persistent", False):
            ctx_mode = "accumulate"

    parent_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
    task_ctx = getattr(ls.rctx, "task_context", None) or {}
    parent_conv_key = str(
        task_ctx.get("route_conversation_key")
        or task_ctx.get("conversation_key")
        or ""
    ).strip()
    parent_channel = task_ctx.get("channel", "internal")
    target_node_id = target

    if ctx_mode in ("fresh", "fork") or is_ephemeral_worker:
        conv_key = f"agent:{target_node_id}:{parent_conv_key}:{_uuid.uuid4()}"
    else:  # accumulate
        if ctx_key:
            conv_key = f"agent:{target_node_id}:{ctx_key}:{parent_conv_key}"
        else:
            conv_key = f"agent:{target_node_id}:{parent_conv_key}"
        _seen_conv = getattr(ls, "_step_dispatch_conv_keys", None)
        if _seen_conv is None:
            _seen_conv = {}
            setattr(ls, "_step_dispatch_conv_keys", _seen_conv)
        _step_tag = f"{getattr(ls.rctx, 'current_llm_request_id', '')}:{conv_key}"
        _cnt = _seen_conv.get(_step_tag, 0) + 1
        _seen_conv[_step_tag] = _cnt
        if _cnt > 1:
            conv_key = f"{conv_key}:par{_cnt}"

    dispatch_origin: dict[str, Any] = {
        "parent_session_id": parent_session_id,
        "caller_node_id": ls.node.id,
        "origin_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
        "origin_execution_id": str(getattr(ls.rctx, "execution_id", "") or ""),
        "parent_conversation_key": parent_conv_key,
        "context_mode": ctx_mode,
    }

    # Cache-Warm Fork: when spawning a fork worker (or fork dispatch), persist the parent's
    # exact cache-warmed prefix (system prompt + static blocks + history + tools + sticky_session_id)
    # so the child worker hits 100% of the parent session's upstream KV cache!
    if ctx_mode == "fork" and is_ephemeral_worker and target == "_worker":
        parent_sticky_sid = str(getattr(ls.rctx, "sticky_session_id", "") or parent_session_id or "").strip()
        if parent_sticky_sid:
            dispatch_origin["parent_sticky_session_id"] = parent_sticky_sid
        prefix_msgs = _extract_cache_warm_fork_prefix(ls)
        if prefix_msgs:
            try:
                snap_dir = Path(ls.rctx.workspace_root) / "data" / "temp" / "fork_snapshots"
                snap_dir.mkdir(parents=True, exist_ok=True)
                snap_name = f"fork_{_uuid.uuid4().hex}.json"
                snap_path = snap_dir / snap_name
                snap_payload = {
                    "messages": prefix_msgs,
                    "openai_tools": copy.deepcopy(getattr(ls, "openai_tools", None) or []),
                    "sticky_session_id": parent_sticky_sid,
                }
                snap_path.write_text(json.dumps(snap_payload, ensure_ascii=False), encoding="utf-8")
                dispatch_origin["fork_snapshot_path"] = f"data/temp/fork_snapshots/{snap_name}"
            except Exception:
                pass

    inbound_payload: dict[str, Any] = {
        "channel": parent_channel,
        "conversation_key": conv_key,
        "text": instr,
        "entry_node_id": target_node_id,
        "use_context": True,
        "attachments": attachments or [],
        "dispatch_origin": dispatch_origin,
        "dispatch_context_mode": ctx_mode,
    }
    if ctx_mode == "fork":
        inbound_payload["dispatch_fork_from_session"] = getattr(ls.rctx, "session_id", "") or parent_session_id

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
            _sid_short = str(_d_data.get("session_id", ""))[:8]
            _dispatch_result = json.dumps({
                "success": True,
                "session_id": _d_data.get("session_id", ""),
                "message": (
                    f"⏳ Task delegated to {target} (child session: {_sid_short}). "
                    "The result will arrive wrapped in <dispatch_result>, delivered via preempt "
                    "while this task is running, or as a follow-up message after it ends. "
                    "You can finish this task or continue working on other things."
                ),
            }, ensure_ascii=False)
        else:
            _dispatch_result = json.dumps({
                "success": False,
                "error": f"inbound API returned {_dispatch_resp.status_code}: {_dispatch_resp.text}",
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
