from __future__ import annotations

import asyncio
import json
import math
import re
import textwrap
from typing import Any


_BUILTIN_NAMES = {
    "len", "any", "all", "str", "int", "float", "list", "dict", "set", "tuple",
    "range", "enumerate", "isinstance", "type", "sorted", "reversed", "min", "max",
    "sum", "zip", "map", "filter", "print", "repr", "bool", "abs", "round",
}


def _fail(node_id: str, error: str) -> dict[str, Any]:
    """Build a script-node failure action dict."""
    # [AutoC 2026-06-09] Why: script_step returns TaskAction dictionaries before
    # runner converts them into TaskAction objects. How: centralize the failure
    # payload shape here. Purpose: timeout, compile, runtime, and empty-result
    # failures stay compatible with the supervisor task result protocol.
    return {"action": "fail", "node_id": node_id, "error": str(error)}


def _coerce_summary(summary: Any) -> str:
    """Coerce optional helper summaries to the existing protocol string shape."""
    # [AutoC 2026-06-09] Why: script authors may pass None or non-string summary
    # values. How: keep None as an empty string and stringify other values.
    # Purpose: finish and ask helpers always return stable serializable payloads.
    return "" if summary is None else str(summary)


def _allowed_builtins() -> dict[str, Any]:
    """Return the deliberately small builtin set exposed to inline scripts."""
    # [AutoC 2026-06-09] Why: Phase 1 script nodes should be deterministic logic
    # and must not receive unrestricted Python builtins by accident. How: expose
    # only the requested common pure helpers. Purpose: keep the execution surface
    # small until later phases explicitly add tool or file permissions.
    builtins_obj = __builtins__
    builtins_dict = builtins_obj if isinstance(builtins_obj, dict) else vars(builtins_obj)
    return {name: builtins_dict[name] for name in _BUILTIN_NAMES if name in builtins_dict}


async def run_script_node(*, rctx, node, text, attachments, input_data) -> dict:
    """执行 script 节点，返回 TaskAction dict。"""
    # [AutoC 2026-06-09] Why: script nodes bypass provider resolution and the LLM
    # loop but still need the same terminal action contract. How: compile the
    # configured inline Python as an async function with a restricted globals
    # dictionary and wait_for the node-level timeout. Purpose: deterministic gate
    # nodes can finish or ask through existing supervisor routing.
    node_id = str(getattr(node, "id", "") or "")
    script = getattr(node, "script", "")
    if not isinstance(script, str) or not script.strip():
        return _fail(node_id, "script is empty")

    task_input = input_data if isinstance(input_data, dict) else {}
    script_text = str(task_input.get("instruction") or text or "")
    script_attachments = task_input.get("attachments") if isinstance(task_input.get("attachments"), list) else attachments
    if not isinstance(script_attachments, list):
        script_attachments = []

    def _make_finish(text, summary=None, attachments=None):
        summary_text = _coerce_summary(summary)
        result = {"text": str(text), "summary": summary_text}
        if isinstance(attachments, list) and attachments:
            result["attachments"] = attachments
        return {
            "action": "finish",
            "node_id": node_id,
            "result": result,
            "summary": summary_text,
        }

    def _make_ask(text, summary=None):
        summary_text = _coerce_summary(summary)
        return {
            "action": "ask",
            "node_id": node_id,
            "result": {"text": str(text), "summary": summary_text},
            "summary": summary_text,
        }

    def _make_reject(reason, summary=None):
        # [AutoC 2026-06-10] Why: output chain quality scripts must reject bad
        # upstream output without using ask's clarification semantics. How: expose
        # a reject helper with the same result shape as finish/ask. Purpose: script
        # QA nodes can send a clear reject action back to the chain entry.
        summary_text = _coerce_summary(summary)
        return {
            "action": "reject",
            "node_id": node_id,
            "result": {"text": str(reason), "summary": summary_text},
            "summary": summary_text,
        }

    script_globals: dict[str, Any] = {
        "__builtins__": _allowed_builtins(),
        "text": script_text,
        "attachments": script_attachments,
        "input_data": task_input,
        "finish": _make_finish,
        "ask": _make_ask,
        "reject": _make_reject,
        "re": re,
        "json": json,
        "math": math,
    }

    try:
        timeout = float(getattr(node, "script_timeout_sec", 30.0) or 30.0)
    except Exception:
        timeout = 30.0
    if timeout <= 0:
        timeout = 30.0

    try:
        _wrapped = "async def __script_main__():\n" + textwrap.indent(script, "    ")
        code = compile(_wrapped, f"<script:{node_id}>", "exec")
        exec(code, script_globals)
        result = await asyncio.wait_for(script_globals["__script_main__"](), timeout=timeout)
    except asyncio.TimeoutError:
        return _fail(node_id, "script timeout")
    except Exception as exc:
        return _fail(node_id, f"script error: {exc}")

    if isinstance(result, dict) and "action" in result:
        return result
    if result is None:
        return _fail(node_id, "script returned no action")
    return _fail(node_id, "script returned invalid action")
