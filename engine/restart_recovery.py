"""Engine-side interruption records; only Supervisor authorizes automatic recovery."""
from __future__ import annotations

import copy
from contextvars import ContextVar
import json
from typing import Any
from uuid import uuid4

from .conversation_store import Message


current_context: ContextVar[Any] = ContextVar("restart_run_context", default=None)

INTERRUPTED_TEXT = (
    "Execution was interrupted. The operation did not return a confirmed result; "
    "external side effects may already have occurred. Check the actual state before retrying."
)


def interrupted_result() -> dict[str, Any]:
    return {"ok": False, "cancelled": True, "error": INTERRUPTED_TEXT,
            "interruption_reason": "execution_interrupted"}


def runtime_state(rctx: Any) -> dict[str, Any]:
    state = {
        "tool_call_log": copy.deepcopy(getattr(rctx, "tool_call_log", [])),
        "total_usage": dict(getattr(rctx, "total_usage", {}) or {}),
        "completed_steps": int(getattr(rctx, "completed_steps", 0)),
        "input_persisted": bool(getattr(rctx, '_restart_input_persisted', False)),
        "resume_execution_id": str(getattr(rctx, '_resume_execution_id', '')),
        "first_shadow_message_id": str(getattr(rctx, "first_shadow_message_id", "")),
        "last_shadow_message_id": str(getattr(rctx, "last_shadow_message_id", "")),
        "tool_produced_attachments": copy.deepcopy(getattr(rctx, "_restart_tool_attachments", [])),
    }
    if getattr(rctx, "_history_write_error", ""):
        state["checkpoint_error"] = str(rctx._history_write_error)
    return state


def cancelled_task_result(rctx: Any, node_id: str) -> dict[str, Any]:
    result = {"action": "cancelled", "node_id": node_id, "error": "engine graceful shutdown"}
    if rctx is None:
        result['_runtime_state'] = {'input_persisted': False, 'completed_steps': 0}
    else:
        state = runtime_state(rctx)
        result["_runtime_state"] = state
        if state.get("checkpoint_error"):
            result["checkpoint_error"] = state["checkpoint_error"]
        if getattr(rctx, "child_session_id", ""):
            result["child_session_id"] = rctx.child_session_id
    return result


def restore_runtime_state(rctx: Any, state: dict[str, Any]) -> None:
    for key in ("tool_call_log", "total_usage", "completed_steps",
                "first_shadow_message_id", "last_shadow_message_id"):
        if key in state:
            setattr(rctx, key, copy.deepcopy(state[key]))
    if "resume_execution_id" in state:
        rctx._resume_execution_id = str(state["resume_execution_id"])
    rctx._restart_tool_attachments = copy.deepcopy(state.get("tool_produced_attachments", []))
    if state.get("checkpoint_error"):
        raise ValueError("Cannot resume an unconfirmed history checkpoint: " + str(state["checkpoint_error"]))


def repair_restart_history(messages: list[dict[str, Any]], resume_data: dict[str, Any],
                           formatter: Any, rctx: Any, node: Any) -> None:
    """Close interrupted call pairs without executing tools or adding a new user input.

    A confirmed restart tool receipt is the only cancellation replaced by success.
    Other confirmed results remain untouched. Writes are fail-fast before inference.
    """
    restart_call = str(resume_data.get("restart_tool_call_id") or "")
    store = getattr(rctx, "conversation_store", None)
    sid = getattr(rctx, "child_session_id", "") or rctx.session_id
    task_id = str(getattr(rctx, "task_id", ""))
    from providers.tool_format import ParsedToolCall

    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        # Never repair another task's historical incomplete turn.
        owner = str((message.get("_meta") or {}).get("source_task_id") or message.get("source_task_id") or "")
        if not calls or owner != task_id:
            index += 1
            continue
        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if any(m.get('role') == 'assistant' and str((m.get('_meta') or {}).get('source_task_id') or m.get('source_task_id') or '') == task_id for m in messages[end:]):
            index = end
            continue
        rows = messages[index + 1:end]
        by_id = {str(row.get("tool_call_id") or ""): row for row in rows}
        ordered = []
        changed = False
        for call in calls:
            cid = str(call.get("id") or "")
            fn = call.get("function") or {}
            name = str(call.get("name") or fn.get("name") or "")
            row = by_id.get(cid)
            is_restart = bool(cid and cid == restart_call and name == "request_restart")
            structured = ((row or {}).get("_meta") or {}).get("tool_result_structured") or {}
            needs_receipt = is_restart and (row is None or structured.get("cancelled") or not structured.get("ok"))
            if row is None or needs_receipt:
                result = ({"ok": True, "scheduled": True, "data": {"scheduled": True},
                           "result": "Restart request accepted. Continue after this tool call; do not request it again."}
                          if is_restart else interrupted_result())
                replacement = formatter.format_tool_result(ParsedToolCall(id=cid, name=name), json.dumps(result, ensure_ascii=False))
                replacement["_meta"] = {"message_type": "tool_result", "tool_mode": node.tool_mode,
                                        "tool_result_structured": result, "source_task_id": task_id}
                if store is None:
                    raise RuntimeError("Restart recovery requires persistent conversation history")
                replacement["id"] = str((row or {}).get("id") or uuid4())
                replacement['source_task_id'] = task_id
                replacement['source_node_id'] = node.id
                replacement['message_type'] = 'tool_result'
                row = replacement
                changed = True
            ordered.append(row)
        if changed:
            # Insert paired results at their original turn, not at the transcript tail.
            # Startup restores one execution per task; all unrelated rows stay untouched.
            stored = store.load(sid)
            assistant_id = str(message.get('id') or '')
            anchor = next((i for i, m in enumerate(stored) if m.id == assistant_id and m.source_task_id == task_id), None)
            if anchor is None:
                raise RuntimeError('Restart recovery cannot locate the persisted assistant turn')
            disk_end = anchor + 1
            while disk_end < len(stored) and stored[disk_end].role == 'tool':
                disk_end += 1
            stored[anchor + 1:disk_end] = [Message.from_dict({**row, 'meta': row.get('_meta') or {},
                                                           'source_task_id': task_id,
                                                           'source_node_id': node.id}) for row in ordered]
            store.replace_all(sid, stored)
        messages[index + 1:end] = ordered
        index += len(ordered) + 1
