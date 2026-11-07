"""伪工具 spec 构建器和工具列表 helpers。

从 ai_step.py 中拆出，这些函数零外部依赖，纯数据结构构建。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..node import Node


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({
    "finish",
    "ask",
    "intermediate_reply",
    "switch_node",
    "compact_context",
    "spawn_worker",
    "manage_workers",
    "dispatch_to",
})

# Retired pseudo tools. They were folded into manage_workers and are intentionally
# NOT pseudo names anymore: a model that hallucinates them is rejected by the
# unauthorized-tool path instead of reaching a handler. The names stay reserved in
# toolbox/builtins so script tools cannot squat on them.
_RETIRED_PSEUDO_TOOL_NAMES = frozenset({"preempt_task", "cancel_active_tasks"})

DISPATCH_TOOL_PREFIX = "dispatch_to_"


def _is_pseudo_tool_name(name: str) -> bool:
    """Return whether ``name`` is handled by the engine pseudo-tool layer."""
    tool_name = str(name or "").strip()
    return tool_name in _PSEUDO_TOOL_NAMES or (
        tool_name.startswith(DISPATCH_TOOL_PREFIX)
        and bool(tool_name[len(DISPATCH_TOOL_PREFIX):].strip())
    )


def allowed_pseudo_names(openai_tools: list[dict], node: Any = None) -> set[str]:
    """Derive the authorized pseudo-tool set from the composed tool list."""
    names: set[str] = set()
    for spec in openai_tools or []:
        if not isinstance(spec, dict):
            continue
        name = str((spec.get("function") or {}).get("name") or "").strip()
        if name and _is_pseudo_tool_name(name):
            names.add(name)
    ct = getattr(node, "control_tools", "all") if node is not None else "all"
    if isinstance(ct, str):
        if ct.strip().lower() != "none":
            names.add("finish")
            names.add("ask")
    elif isinstance(ct, list):
        ct_set = {str(x).strip() for x in ct}
        if "finish" in ct_set:
            names.add("finish")
        if "ask" in ct_set:
            names.add("ask")
    # Backward-compatible alias:
    # - dispatch_to also authorizes legacy dispatch_to_{target_id} calls for node.delegate_targets
    if "dispatch_to" in names and node is not None:
        for t in (getattr(node, "delegate_targets", None) or []):
            t_str = str(t or "").strip()
            if t_str:
                names.add(_dispatch_tool_name(t_str))
    # No alias path for the retired preempt_task / cancel_active_tasks: manage_workers is the
    # only worker-control authorization. tool_access deny/exclusion applies to pseudo tools too,
    # so a control tool excluded by the node is never authorized here.
    if node is not None:
        names = {n for n in names if is_tool_action_allowed(node, n, {})}
    return names


import re as _re

_TOOL_NAME_SANITIZE_RE = _re.compile(r'[^a-zA-Z0-9_-]')
# Reverse mapping: sanitized tool name suffix → original node id
_DISPATCH_TOOL_REVERSE: dict[str, str] = {}


def _sanitize_node_id_for_tool(node_id: str) -> str:
    """Sanitize a node id to comply with OpenAI tool name pattern [a-zA-Z0-9_-]."""
    return _TOOL_NAME_SANITIZE_RE.sub('_', str(node_id or '').strip())


def _dispatch_tool_name(target_id: str) -> str:
    """Build the public per-target dispatch tool name (legacy helper)."""
    raw = str(target_id or '').strip()
    sanitized = _sanitize_node_id_for_tool(raw)
    name = f"{DISPATCH_TOOL_PREFIX}{sanitized}"
    _DISPATCH_TOOL_REVERSE[sanitized] = raw
    return name


def _dispatch_target_from_tool_name(tool_name: str) -> str:
    """Extract the original target id from a dispatch_to_{sanitized_id} tool name."""
    name = str(tool_name or "").strip()
    if not name.startswith(DISPATCH_TOOL_PREFIX):
        return ""
    sanitized_suffix = name[len(DISPATCH_TOOL_PREFIX):].strip()
    return _DISPATCH_TOOL_REVERSE.get(sanitized_suffix, sanitized_suffix)


CONTROL_TOOL_NAMES = frozenset({"finish", "ask"})


def _is_control_tool_name(name: str) -> bool:
    """Return whether ``name`` is an engine control-flow tool."""
    return str(name or "").strip() in CONTROL_TOOL_NAMES


# ---------------------------------------------------------------------------
#  v3 伪工具 spec 构建
# ---------------------------------------------------------------------------

def _dispatch_delegate_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "Clear, specific, actionable instructions for the target node. State what to do, why, and what you already know.",
            },
            "context_mode": {
                "type": "string",
                "enum": ["fresh", "fork", "accumulate"],
                "default": "accumulate",
                "description": "Child context mode. fresh = start from scratch with no history each time; fork = inherit the parent's conversation history; accumulate = first run starts fresh, later runs resume the child's own context (default accumulate).",
            },
            "context_key": {
                "type": "string",
                "description": "Context inheritance key (only effective in accumulate mode). Tasks sharing the same context_key share one context chain. Defaults to the target node ID.",
            },
            "attachment_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
            },
        },
        "required": ["instruction"],
    }


def _dispatch_delegate_spec(target_id: str, info: dict[str, str] | None = None) -> dict:
    """Build one legacy dispatch_to_{target_id} pseudo-tool spec."""
    target = str(target_id or "").strip()
    node_name = str((info or {}).get("name") or target).strip() or target
    node_description = str((info or {}).get("description") or node_name or target).strip()
    desc_parts = [
        f"Delegate a task to the fixed target node {node_name} ({target}).",
        node_description,
        "",
        "⚡ This is an async non-terminating tool: the target node executes independently in the background. Its result will be wrapped in <dispatch_result> tags and delivered via preempt while your task is running, or as a follow-up message after your task finishes.",
        "No target parameter is provided; the tool name already fixes the target node.",
        "To delegate in parallel, call multiple dispatch_to_{target_id} tools in the same turn.",
        "Returned results are wrapped in <dispatch_result> tags: the tag content is system-delivered child output, not user speech; do not treat it as a question to answer or greet the user about it — integrate it and continue your current work.",
    ]
    return {
        "type": "function",
        "function": {
            "name": _dispatch_tool_name(target),
            "description": "\n".join(part for part in desc_parts if part is not None),
            "parameters": _dispatch_delegate_parameters(),
        },
    }


def _dispatch_delegate_specs(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> list[dict]:
    """Build legacy per-target dispatch pseudo-tools for all delegate targets."""
    info_by_id = {
        str(info.get("id") or "").strip(): info
        for info in (downstream_info or [])
        if isinstance(info, dict) and str(info.get("id") or "").strip()
    }
    result: list[dict] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        result.append(_dispatch_delegate_spec(target, info_by_id.get(target)))
    return result


def _dispatch_to_spec(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> dict:
    """Build the unified dispatch_to pseudo-tool spec with dynamic target metadata."""
    info_by_id = {
        str(info.get("id") or "").strip(): info
        for info in (downstream_info or [])
        if isinstance(info, dict) and str(info.get("id") or "").strip()
    }
    enum_targets: list[str] = []
    target_lines: list[str] = []
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if not target or target in enum_targets:
            continue
        enum_targets.append(target)
        _dispatch_tool_name(target)  # populate _DISPATCH_TOOL_REVERSE for legacy compatibility
        info = info_by_id.get(target) or {}
        node_name = str(info.get("name") or target).strip() or target
        node_desc = str(info.get("description") or node_name or target).strip()
        if node_name and node_name != target:
            target_lines.append(f"- {target} ({node_name}): {node_desc}")
        else:
            target_lines.append(f"- {target}: {node_desc}")

    desc_parts = [
        "Delegate a task to a specialized target sub-agent node.",
        "",
        "⚡ This is an async non-terminating tool: the target node executes independently in the background. "
        "Its result will be wrapped in <dispatch_result> tags and delivered automatically via preempt while your task is running, "
        "or as a follow-up message after your task finishes. You can continue working or finish immediately without waiting.",
        "To delegate multiple tasks in parallel, emit multiple dispatch_to tool calls in the same turn.",
        "Returned results are wrapped in <dispatch_result> tags: the tag content is system-delivered child output, not user speech; "
        "do not treat it as a question to answer or greet the user about it — integrate it and continue your current work.",
    ]
    if target_lines:
        desc_parts.append("")
        desc_parts.append("Available target nodes:")
        desc_parts.extend(target_lines)

    target_prop: dict[str, Any] = {
        "type": "string",
        "description": "Target node ID to delegate the task to.",
    }
    if enum_targets:
        target_prop["enum"] = enum_targets

    return {
        "type": "function",
        "function": {
            "name": "dispatch_to",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target_prop,
                    "instruction": {
                        "type": "string",
                        "description": "Clear, specific, actionable instructions for the target node. State what to do, why, and what you already know.",
                    },
                    "context_mode": {
                        "type": "string",
                        "enum": ["fresh", "fork", "accumulate"],
                        "default": "accumulate",
                        "description": "Child context mode. accumulate = resume the target node's own context (default for persistent nodes); fresh = start a clean context from scratch; fork = inherit the parent's conversation history.",
                    },
                    "context_key": {
                        "type": "string",
                        "description": "Context inheritance key (only effective in accumulate mode). Tasks sharing the same context_key share one context chain. Defaults to the target node ID.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
                    },
                },
                "required": ["target", "instruction"],
            },
        },
    }


def refresh_dispatch_to_spec(ls: Any) -> bool:
    """Dynamically refresh the dispatch_to tool spec in ls.openai_tools if nodes changed on disk.

    Returns True if ls.openai_tools was updated, False if unchanged (preserving prompt cache).
    """
    if getattr(ls, "_cache_warm_fork", False):
        return False

    try:
        rctx = getattr(ls, "rctx", None)
        node = getattr(ls, "node", None)
        openai_tools = getattr(ls, "openai_tools", None)
        if rctx is None or node is None or not isinstance(openai_tools, list):
            return False
        ws_root = Path(getattr(rctx, "workspace_root", "."))
        from ..node import load_node as _load_node
        from ..runner import _collect_node_info
        fresh_node = _load_node(ws_root, node.id) or node
        targets = [str(t).strip() for t in (getattr(fresh_node, "delegate_targets", None) or []) if str(t).strip()]
        # Filter out targets whose YAML file was deleted and is not a declared plugin node
        valid_targets: list[str] = []
        for t in targets:
            if _load_node(ws_root, t) is not None:
                valid_targets.append(t)
        # If all configured targets are synthetic (e.g. unit tests without files), keep raw targets
        effective_targets = valid_targets if valid_targets or not targets else targets
        node.delegate_targets = list(effective_targets)

        existing_idx = next(
            (i for i, s in enumerate(openai_tools) if isinstance(s, dict) and (s.get("function") or {}).get("name") == "dispatch_to"),
            -1,
        )
        if not effective_targets:
            if existing_idx >= 0:
                openai_tools.pop(existing_idx)
                ls.allowed_pseudo_tools = allowed_pseudo_names(openai_tools, node)
                return True
            return False

        ds_info = _collect_node_info(ws_root, effective_targets)
        new_spec = _dispatch_to_spec(effective_targets, ds_info)
        if existing_idx >= 0:
            if openai_tools[existing_idx] == new_spec:
                return False
            openai_tools[existing_idx] = new_spec
        else:
            openai_tools.append(new_spec)
        ls.allowed_pseudo_tools = allowed_pseudo_names(openai_tools, node)
        return True
    except Exception:
        return False


def _finish_spec() -> dict:
    """Build the finish pseudo-tool spec."""
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. If you have other tools to call (like execute_command or manage_memory), you MUST wait for them to complete (`ok`) before calling finish.\n\n"
                "⚠️ finish MUST be called ALONE — never in the same turn as other tools (except intermediate_reply). "
                "If you call finish alongside execute_command, manage_memory, a dispatch tool, or any other tool, ALL calls will be REJECTED and you must retry. "
                "Always: execute tools first → wait for results → then call finish separately.\n\n"
                "Do NOT send your final report via `intermediate_reply` while waiting for tools. Just call the tools silently, wait for the next turn, and put your ENTIRE final report in the `finish` tool's text.\n\n"
                "The `text` parameter is the ACTUAL DELIVERABLE — the content the caller "
                "or user will receive. Put your real output here (full answer, full data, "
                "full summary), not a status report of what you did. If the caller expects "
                "a structured payload, put the entire structured payload in `text`.\n\n"
                "Free prose outside of tool calls is NOT delivered to the caller or user. "
                "Only `intermediate_reply()` and `finish()` reach the recipient.\n\n"
                "Use cases:\n"
                "- Task completed: put the deliverable content in `text`.\n"
                "- Need more information: put your specific question in `text`. "
                "The caller or user will see it and respond.\n\n"
                "Parameters:\n"
                "- text: The actual deliverable content, or a question if you need more info.\n"
                "- attachment_paths: File paths to attach (optional).\n\n"
                "Do not include internal protocol markers or debug info in text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The final result text, or a specific question when more information is needed.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of accompanying file paths (use only when there are attachments such as images).",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _ask_spec() -> dict:
    """Build the legacy ask pseudo-tool spec (no longer injected by default)."""
    return {
        "type": "function",
        "function": {
            "name": "ask",
            "description": (
                "Ask the upstream (caller or user) for additional information or clarification. "
                "This terminates the current task, similar to finish, but signals that you need "
                "more input before you can complete your work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Your question or request for additional information.",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _reply_spec() -> dict:
    """Build the intermediate_reply pseudo-tool spec (non-terminating)."""
    return {
        "type": "function",
        "function": {
            "name": "intermediate_reply",
            "description": (
                "Send an interim message to the user WITHOUT terminating this node.\n\n"
                "The node keeps running after this call — you can continue calling tools "
                "or do more work in subsequent turns.\n\n"
                "The `text` parameter is the message content the user will read. "
                "Anything you want the user to see mid-task must go here.\n\n"
                "When to use:\n"
                "- You have a partial result or progress update to share, but more work remains.\n"
                "- The user asked for multiple things and you want to respond to one "
                "while continuing to work on the rest.\n\n"
                "When NOT to use:\n"
                "- You are done with all work → just say your final answer directly (free prose, or call finish in tool_only mode).\n"
                "- You need more information from the user → ask directly in your final response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message content the user will read.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of workspace-relative file paths to attach. "
                            "Files will be sent alongside the text message."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    }


def _compact_context_spec() -> dict:
    """伪工具：手动触发上下文压缩。"""
    return {
        "type": "function",
        "function": {
            "name": "compact_context",
            "description": (
                "Compress the current conversation context by summarizing older messages.\n\n"
                "Use when context is getting very long and you want to free up token budget. "
                "Recent messages are preserved; older messages are replaced with a structured summary.\n\n"
                "Calling this suspends the current step to run the compaction sub-task and automatically "
                "resumes this node once compression finishes.\n"
                "⚠️ compact_context MUST be called ALONE in its own turn — never alongside other tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "description": "Number of recent conversation rounds to preserve (default uses system config, typically 6).",
                        "type": "integer",
                    },
                },
                "required": [],
            },
        },
    }


def _spawn_worker_spec() -> dict:
    """Build the spawn_worker pseudo-tool spec for anonymous ephemeral sub-agents."""
    return {
        "type": "function",
        "function": {
            "name": "spawn_worker",
            "description": (
                "Spawn an anonymous ephemeral worker sub-agent that executes a single bounded task "
                "in the background and returns the result. The worker runs in its own isolated context.\n\n"
                "Use this when you need to delegate a bounded task without a pre-defined "
                "target node. Useful for parallel fan-out: call spawn_worker multiple times "
                "in one turn to run tasks concurrently.\n\n"
                "Context modes:\n"
                "- 'fork' (default, recommended): Cache-Warm Fork — inherits the parent session's exact "
                "system prompt, tool declarations, and conversation history prefix so the worker hits "
                "the parent session's hot prompt cache while knowing full prior context.\n"
                "- 'fresh': Starts from a minimal blank worker prompt with no conversation history.\n\n"
                "Results are delivered asynchronously: while your current task is still "
                "running they are injected into it as preempt messages (several results "
                "may arrive combined); after it finishes they arrive as follow-up messages "
                "in the same conversation. No polling is needed.\n\n"
                "Every result is wrapped in a <dispatch_result>...</dispatch_result> block. "
                "Content inside is system-delivered sub-agent output, NOT user speech: never "
                "treat it as a question or greet the user about it — integrate it and continue "
                "your work.\n\n"
                "Parameters:\n"
                "- instruction: Clear, specific task for the worker to complete.\n"
                "- context_mode: 'fork' (default, cache-warm fork of parent session) or 'fresh' (minimal blank worker).\n"
                "- template: Optional node ID to use as the worker's base config (model, tools). "
                "If omitted, uses the built-in _worker template."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "Clear, specific, actionable task instruction for the temporary worker.",
                    },
                    "context_mode": {
                        "type": "string",
                        "enum": ["fresh", "fork"],
                        "default": "fork",
                        "description": "Child context mode. fork = cache-warm fork inheriting parent's exact system prompt, tools, and conversation history (recommended); fresh = start from a minimal blank template.",
                    },
                    "template": {
                        "type": "string",
                        "description": "Optional: create the worker from an existing node's config (model/tools). Defaults to the built-in _worker template.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths (relative to workspace root) to attach to the worker's initial context.",
                    },
                },
                "required": ["instruction"],
            },
        },
    }


def _manage_workers_spec() -> dict:
    """Build the unified manage_workers pseudo-tool spec (list / message / preempt / cancel)."""
    return {
        "type": "function",
        "function": {
            "name": "manage_workers",
            "description": (
                "List, message, gracefully preempt, or hard-cancel active child tasks "
                "(dispatched sub-agents and spawned workers).\n\n"
                "Actions:\n"
                "- list: Show all active child tasks in the current session with their task_id, node_id, status, and elapsed time.\n"
                "- message: Inject an additional instruction into a running child task (identified by target/task_id or node_id). "
                "The child finishes its current atomic step, receives your message, and continues working.\n"
                "- preempt: Gracefully preempt a running child task (identified by target/task_id or node_id) so it stops after its current step.\n"
                "- cancel: Hard-cancel a specific child task (by target/task_id or node_id), or omit target/task_id/node_id to cancel ALL active downstream tasks in the current session.\n\n"
                "Parameters:\n"
                "- action: Required. One of 'list', 'message', 'preempt', 'cancel'.\n"
                "- target: Target task ID (full or prefix) OR target node ID (e.g. 'smith', 'scout'). "
                "Required for 'message' and 'preempt'; optional for 'cancel' (omit to cancel all).\n"
                "- message: Instruction text to inject (required for 'message', optional for 'preempt')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "message", "preempt", "cancel"],
                        "description": "Action type: list = view active child tasks, message = inject instruction into a running child, preempt = gracefully stop a child after its current step, cancel = hard-cancel specific or all active child tasks.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target task ID (full or prefix) OR target node ID (e.g. 'smith', 'scout'). Required for message/preempt; optional for cancel (omit = cancel all).",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Alias for target (task ID prefix or node ID).",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional node ID filter (e.g. 'scout', 'smith') when targeting or cancelling by node name.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Instruction text to inject into the child task (required for 'message' action).",
                    },
                },
                "required": ["action"],
            },
        },
    }


def _switch_node_spec(targets: list[str], switch_info: list[dict[str, str]] | None = None, *, current_node_id: str = "", current_node_name: str = "") -> dict:
    """构建 switch_node 伪工具定义——切换 session 的对话节点。"""
    desc_parts = [
        "Switch the conversation's dialogue node. The current node finishes on call; the user's next message is handled by the target node.",
        "",
        "When to use:",
        "- The user explicitly asks to switch to a node (e.g. 'switch to the coding node')",
        "- The current node is insufficient and another node should take over the conversation",
        "",
        "Notes:",
        "- The current node terminates immediately after the call; no further tool calls run",
        "- The target node can see the prior conversation history",
        "- Passing an empty string as target restores the default entry node",
    ]
    if switch_info:
        desc_parts.append("")
        desc_parts.append("Switchable nodes:")
        for info in switch_info:
            desc_parts.append(f"- {info.get('name', info['id'])} ({info['id']}): {info.get('description', '')}")
    enum_targets: list[str] = []
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if target and target not in enum_targets:
            enum_targets.append(target)
    target_prop: dict = {
        "type": "string",
        "description": "Target node ID. Pass an empty string to restore the default entry node.",
    }
    if enum_targets:
        target_prop["enum"] = enum_targets
    return {
        "type": "function",
        "function": {
            "name": "switch_node",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target_prop,
                    "text": {
                        "type": "string",
                        "description": "Reply text for the user (e.g. 'Switched to the coding node').",
                    },
                },
                "required": ["target", "text"],
            },
        },
    }


# ---------------------------------------------------------------------------
#  工具 spec 相关
# ---------------------------------------------------------------------------

_ASYNC_TOOL_HINT = (
    "\n\n⚡ This is an async tool. Execution runs in background; "
    "result will be delivered automatically via preempt when ready. "
    "You can continue working or finish — no need to wait."
)


def _sanitize_schema_patterns(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k == "pattern" and isinstance(v, str):
                out[k] = v.replace("\\_", "_")
            else:
                out[k] = _sanitize_schema_patterns(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_schema_patterns(x) for x in obj]
    return obj


def _require_declared_selector(params: dict, name: str) -> dict:
    """Make a schema-declared ``mode`` selector mandatory when the engine cannot default it.

    Why: the action gate reads ``mode`` for such tools and fails closed when it is
    missing, so the schema must ask for it explicitly instead of relying on an
    implicit server-side default the engine cannot see. How: only touch schemas
    whose selector is ``mode`` and whose resolver has no verified default (grep
    keeps its 'search' default). Purpose: visible schema and gate agree.
    """
    if not isinstance(params, dict) or not params:
        return params
    if "mode" not in (params.get("properties") or {}):
        return params
    if resolve_effective_action(name, {}, {"name": name, "input_schema": params})[1] is not None:
        return params
    required = [r for r in (params.get("required") or []) if r != "mode"]
    return {**params, "required": required + ["mode"]}


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    result = []
    for s in specs:
        desc = s.get("description", "")
        if s.get("async_mode"):
            desc += _ASYNC_TOOL_HINT
        params = s.get("input_schema", {"type": "object", "properties": {}, "required": []})
        result.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": desc,
                "parameters": _sanitize_schema_patterns(_require_declared_selector(params, s["name"])),
            },
        })
    return result

_LEGACY_TO_CONSOLIDATED: dict[str, str] = {
    "read_image": "read_media",
    "read_video": "read_media",
    "list_schedules": "manage_schedule",
    "create_schedule": "manage_schedule",
    "delete_schedule": "manage_schedule",
    "list_mcp_clients": "manage_mcp_client",
    "create_or_update_mcp_client": "manage_mcp_client",
    "delete_mcp_client": "manage_mcp_client",
    "list_skills": "manage_skill",
    "create_or_update_skill": "manage_skill",
    "delete_skill": "manage_skill",
    "list_memories": "manage_memory",
    "save_memory": "manage_memory",
    "delete_memory": "manage_memory",
    "create_or_update_tool": "manage_tool",
    "reload_tools": "manage_tool",
}

_LEGACY_TO_CONSOLIDATED_ACTION: dict[str, tuple[str, str]] = {
    "list_schedules": ("manage_schedule", "list"),
    "create_schedule": ("manage_schedule", "upsert"),
    "delete_schedule": ("manage_schedule", "delete"),
    "list_mcp_clients": ("manage_mcp_client", "list"),
    "create_or_update_mcp_client": ("manage_mcp_client", "upsert"),
    "delete_mcp_client": ("manage_mcp_client", "delete"),
    "list_skills": ("manage_skill", "list"),
    "create_or_update_skill": ("manage_skill", "upsert"),
    "delete_skill": ("manage_skill", "delete"),
    "list_memories": ("manage_memory", "list"),
    "save_memory": ("manage_memory", "upsert"),
    "delete_memory": ("manage_memory", "delete"),
    "create_or_update_tool": ("manage_tool", "upsert"),
    "reload_tools": ("manage_tool", "reload"),
}


def _schema_action_selector(spec: dict[str, Any] | None) -> tuple[str, list] | None:
    """Return (field, enum) when a tool schema uses ``mode`` as its action selector.

    Why: nodes gate calls on the *effective action*, so the engine must read the
    same field the tool itself reads. External tools (MCP / script) commonly
    declare ``mode`` with an enum and no ``action``; a sibling ``action`` argument
    is then a free-form extra the server ignores, so trusting it would let a
    denied mode slip through under an allowed-looking action.
    How: only the schema decides. A schema that also declares ``action`` keeps
    ``action`` semantics (the manage_* tools), and the grep family is handled by
    its verified default before this helper is consulted.
    Purpose: one shared selector definition for resolve / gate / spec pruning.
    """
    if not isinstance(spec, dict):
        return None
    schema = spec.get("input_schema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or "action" in props:
        return None
    mode = props.get("mode")
    enum = mode.get("enum") if isinstance(mode, dict) else None
    if isinstance(enum, list) and enum:
        return "mode", enum
    return None


def resolve_effective_action(
    tool_name: str,
    args: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Resolve (canonical_tool_name, effective_action) for a tool call, handling defaults and synonyms.

    ``spec`` (the tool's registry schema, when the caller has it) makes the
    resolution schema-aware for tools whose action selector is not ``action``.
    """
    if tool_name in _LEGACY_TO_CONSOLIDATED_ACTION:
        return _LEGACY_TO_CONSOLIDATED_ACTION[tool_name]
    cons_name = _LEGACY_TO_CONSOLIDATED.get(tool_name, tool_name)
    a = args if isinstance(args, dict) else {}
    if cons_name in ("grep", "search_in_files"):
        return "grep", str(a.get("mode") or "search").strip().lower()
    selector = _schema_action_selector(spec)
    if selector is not None:
        # Require exact enum membership first: the server may interpret other
        # spellings differently. Then use the same canonical policy token as
        # rule parsing and schema pruning; an uppercase enum cannot bypass deny.
        _raw_mode = a.get(selector[0])
        return cons_name, (_raw_mode.strip().lower() if isinstance(_raw_mode, str) and _raw_mode in selector[1] else None)
    act = str(a.get("action") or "").strip().lower()
    if cons_name in ("manage_skill", "manage_memory", "manage_mcp_client"):
        # These entrypoints default to list, never infer writes and reject
        # action aliases. Resolve exactly the operation they will execute.
        return cons_name, act or "list"
    if cons_name == "manage_schedule":
        if not act:
            # Mirrors toolbox/builtins/schedules.py:manage_schedule (the entrypoint is the
            # source of truth; a key it treats as a write must not resolve to 'list',
            # otherwise a deny on ':upsert' is bypassable by omitting action).
            act = "upsert" if (a.get("id") and any(a.get(k) is not None for k in (
                "cron", "text", "command", "type", "enabled", "once", "timeout",
                "silent", "conversation_key", "entry_node_id", "workflow_id"))) else "list"
        elif act in ("create", "update"):
            act = "upsert"
        elif act == "remove":
            act = "delete"
        return cons_name, act
    if cons_name == "manage_tool":
        if not act:
            act = "upsert" if (a.get("name") or a.get("script")) else "reload"
        elif act in ("create", "update"):
            act = "upsert"
        return cons_name, act
    if cons_name == "search_web":
        if not act:
            act = "fetch" if (a.get("urls") and not a.get("query")) else "search"
        return cons_name, act
    if act:
        return cons_name, act
    return cons_name, None


def _parse_action_spec(act_spec: str) -> tuple[bool, set[str]] | None:
    """Parse one rule's action list into ``(negated, actions)``; ``None`` means malformed.

    Why: a rule the engine cannot read must neither grant (allow) nor be dropped
    (deny). Dropping it made ``allow: ['tool:']`` fall back to "whole tool allowed"
    and ``deny: ['tool:list,!delete']`` lose the negation. How: accept only two
    shapes -- all-positive tokens, or a leading '!' plus plain tokens. Purpose:
    the caller fails closed instead of guessing.
    """
    tokens = [t.strip() for t in str(act_spec or "").split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    negated = [t.startswith("!") for t in tokens]
    if any(negated):
        # '!a,b' and '!a,!b' both mean "all except {a,b}"; 'a,!b' is ambiguous.
        if not negated[0] or (any(negated[1:]) and not all(negated)):
            return None
        acts = {t.lstrip("!").strip().lower() for t in tokens}
    else:
        acts = {t.lower() for t in tokens}
    acts.discard("")
    return (any(negated), acts) if acts else None


def _parse_node_action_rules(node: "Node") -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """Parse node.tool_access into (whole_denied_tools, denied_actions_by_tool, allowed_only_actions_by_tool).

    Supports:
    - 'tool_name': whole tool denied / whole tool allowed (in allow)
    - 'tool_name:act1,act2': these actions denied / allowed
    - 'tool_name:!act1,act2' or 'tool_name:!act1,!act2': all actions EXCEPT {act1, act2}
      (denied in deny; granted in allow, unless another allow entry names the action explicitly)
    - legacy alias names in deny (e.g. 'delete_skill' -> deny 'manage_skill:delete'; if all aliases of a tool are in deny -> whole tool denied)
    - partial legacy aliases or 'tool:act' in allow (when mode == 'allowlist' and the whole tool is not in allow)

    Multiple allow entries are alternatives (union), not a replacement: the union of
    'tool:list' + 'tool:upsert' grants both, and an explicit grant overrides a '!'
    grant from the same list. Deny still wins over every allow form. A rule that
    cannot be parsed fails closed for the named tool (unreadable allow grants
    nothing, unreadable deny denies the tool).
    """
    whole_denied: set[str] = set()
    denied_actions: dict[str, set[str]] = {}
    allowed_only: dict[str, set[str]] = {}

    def _add_allowed_only(tname: str, keep_set: set[str]) -> None:
        if tname in allowed_only:
            allowed_only[tname] &= keep_set
        else:
            allowed_only[tname] = set(keep_set)

    raw_deny = [str(x).strip() for x in (node.tool_access.deny or []) if str(x).strip()]
    legacy_deny_seen: dict[str, set[str]] = {}

    for entry in raw_deny:
        if ":" in entry:
            tname, act_spec = entry.split(":", 1)
            tname = _LEGACY_TO_CONSOLIDATED.get(tname.strip(), tname.strip())
            if not tname:
                continue
            parsed = _parse_action_spec(act_spec)
            if parsed is None:
                # Fail closed: an unreadable deny rule denies the whole tool.
                whole_denied.add(tname)
                continue
            negated, acts = parsed
            if negated:
                _add_allowed_only(tname, acts)
            else:
                denied_actions.setdefault(tname, set()).update(acts)
        elif entry in _LEGACY_TO_CONSOLIDATED_ACTION:
            cons, act = _LEGACY_TO_CONSOLIDATED_ACTION[entry]
            denied_actions.setdefault(cons, set()).update({act})
            legacy_deny_seen.setdefault(cons, set()).add(entry)
        else:
            whole_denied.add(entry)
            if entry in _LEGACY_TO_CONSOLIDATED and entry not in _LEGACY_TO_CONSOLIDATED_ACTION:
                legacy_deny_seen.setdefault(_LEGACY_TO_CONSOLIDATED[entry], set()).add(entry)

    legacy_groups: dict[str, set[str]] = {}
    for leg, cons in _LEGACY_TO_CONSOLIDATED.items():
        legacy_groups.setdefault(cons, set()).add(leg)
    for cons, aliases in legacy_groups.items():
        if aliases <= legacy_deny_seen.get(cons, set()):
            whole_denied.add(cons)

    mode = (node.tool_access.mode or "none").lower()
    if mode == "allowlist":
        raw_allow = [str(x).strip() for x in (node.tool_access.allow or []) if str(x).strip()]
        whole_allowed: set[str] = set()
        allow_actions: dict[str, set[str]] = {}
        allow_except: dict[str, set[str]] = {}
        for entry in raw_allow:
            if ":" in entry:
                tname, act_spec = entry.split(":", 1)
                tname = _LEGACY_TO_CONSOLIDATED.get(tname.strip(), tname.strip())
                if not tname:
                    continue
                parsed = _parse_action_spec(act_spec)
                if parsed is None:
                    # Fail closed: an unreadable grant grants nothing.
                    whole_denied.add(tname)
                    continue
                negated, acts = parsed
                if negated:
                    # Union of complement grants excludes only their intersection:
                    # (!a) OR (!b) permits everything when a and b differ.
                    if tname in allow_except:
                        allow_except[tname] &= acts
                    else:
                        allow_except[tname] = set(acts)
                else:
                    allow_actions.setdefault(tname, set()).update(acts)
            elif entry in _LEGACY_TO_CONSOLIDATED_ACTION:
                cons, act = _LEGACY_TO_CONSOLIDATED_ACTION[entry]
                allow_actions.setdefault(cons, set()).update({act})
            else:
                whole_allowed.add(entry)
                if entry in _LEGACY_TO_CONSOLIDATED:
                    whole_allowed.add(_LEGACY_TO_CONSOLIDATED[entry])
        for tname, acts in allow_actions.items():
            if tname not in whole_allowed and tname not in allow_except:
                _add_allowed_only(tname, acts)
        for tname, acts in allow_except.items():
            if tname in whole_allowed:
                continue  # An explicit whole-tool grant includes every action.
            # 'tool:!a' grants every action except a. The full action set only exists
            # in the tool schema, so record the excluded actions as a targeted deny;
            # an explicit grant from the same allow list still wins over the '!' form.
            excluded = acts - allow_actions.get(tname, set())
            if excluded:
                denied_actions.setdefault(tname, set()).update(excluded)

    for tname in list(allowed_only.keys()):
        if tname in denied_actions:
            allowed_only[tname] -= denied_actions[tname]
        if not allowed_only[tname]:
            whole_denied.add(tname)

    return whole_denied, denied_actions, allowed_only


def is_tool_action_allowed(
    node: "Node",
    tool_name: str,
    args: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
) -> bool:
    """Return True if the specific action on tool_name is permitted by node.tool_access.

    Pass ``spec`` (the tool's registry schema) when available: for tools whose
    action selector is a schema-declared ``mode`` enum, the gate then reads the
    same field the tool reads instead of trusting an unrelated ``action`` argument.
    """
    whole_denied, denied_actions, allowed_only = _parse_node_action_rules(node)
    cons_name, act = resolve_effective_action(tool_name, args, spec)
    if tool_name in whole_denied or cons_name in whole_denied:
        return False
    if act is None:
        # A schema-declared selector could not be resolved (missing or non-enum value):
        # the server may still read it as some other mode, so when the tool carries
        # action rules the call cannot be proven allowed — fail closed. Tools without
        # a declared selector keep the legacy 'no action known, no action rule applies'.
        if _schema_action_selector(spec) is None:
            return True
        return not (cons_name in denied_actions or cons_name in allowed_only)
    if cons_name in denied_actions and act in denied_actions[cons_name]:
        return False
    if cons_name in allowed_only and act not in allowed_only[cons_name]:
        return False
    return True


def _prune_spec_actions(
    spec: dict[str, Any],
    denied_acts: set[str] | None,
    allowed_only_acts: set[str] | None,
) -> dict[str, Any] | None:
    """Prune denied actions from a tool spec's input_schema enum. Returns None if all enum actions are denied."""
    import copy
    if not denied_acts and allowed_only_acts is None:
        return spec
    schema = spec.get("input_schema")
    if not isinstance(schema, dict):
        return None if (allowed_only_acts is not None and len(allowed_only_acts) == 0) else spec
    props = schema.get("properties")
    if not isinstance(props, dict):
        return spec
    # Same selector definition the action gate uses (schema-declared 'mode' enum,
    # no 'action' property), so a pruned enum and the gate cannot disagree.
    selector = _schema_action_selector(spec)
    act_key = "action" if "action" in props else (selector[0] if selector else None)
    if not act_key or not isinstance(props.get(act_key), dict):
        return spec
    enum_vals = props[act_key].get("enum")
    if not isinstance(enum_vals, list) or not enum_vals:
        return spec

    def _ok(val: Any) -> bool:
        v = str(val).strip().lower()
        if denied_acts and v in denied_acts:
            return False
        if allowed_only_acts is not None and v not in allowed_only_acts:
            return False
        return True

    kept = [v for v in enum_vals if _ok(v)]
    if not kept:
        return None
    if len(kept) == len(enum_vals):
        return spec
    cloned = copy.deepcopy(spec)
    cloned["input_schema"]["properties"][act_key]["enum"] = kept
    return cloned


def _filter_tool_specs(node: "Node", all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mode = (node.tool_access.mode or "none").lower()
    if mode not in ("all", "allowlist"):
        return []
    whole_denied, denied_actions, allowed_only = _parse_node_action_rules(node)
    if mode == "allowlist":
        allowed: set[str] = set()
        for raw_entry in (node.tool_access.allow or []):
            entry = str(raw_entry).strip()
            if not entry:
                continue
            base = entry.split(":", 1)[0].strip()
            allowed.add(base)
            if base in _LEGACY_TO_CONSOLIDATED:
                allowed.add(_LEGACY_TO_CONSOLIDATED[base])
        candidates = [s for s in all_specs if s.get("name") in allowed]
    else:
        candidates = list(all_specs)

    filtered: list[dict[str, Any]] = []
    for s in candidates:
        tname = str(s.get("name") or "")
        if not tname or tname in whole_denied:
            continue
        pruned = _prune_spec_actions(s, denied_actions.get(tname), allowed_only.get(tname))
        if pruned is not None:
            filtered.append(pruned)
    return filtered


def build_node_tool_specs(
    *,
    registry: Any,
    node: "Node",
    downstream_info: list[dict[str, str]] | None = None,
    switch_info: list[dict[str, str]] | None = None,
    task_context: dict[str, Any] | None = None,
    has_active_children: bool = False,
) -> tuple[set[str], list[dict]]:
    """Build the model-visible tool list and the allowed real-tool name set."""
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    allowed = {s.get("name") for s in tool_specs if s.get("name")}
    # Authorize legacy alias names only if both their consolidated tool and their specific action are allowed
    for leg_name, cons_name in _LEGACY_TO_CONSOLIDATED.items():
        if cons_name in allowed and is_tool_action_allowed(node, leg_name, {}):
            allowed.add(leg_name)
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    _ct_raw = getattr(node, "control_tools", "all")
    if isinstance(_ct_raw, str):
        _ct_none = _ct_raw.strip().lower() == "none"
        _ct_allow: set[str] | None = None
    elif isinstance(_ct_raw, list):
        _ct_none = False
        _ct_allow = {str(x).strip() for x in _ct_raw if isinstance(x, str)}
    else:
        _ct_none = False
        _ct_allow = None

    def _ctl(name: str) -> bool:
        if _ct_none:
            return False
        if _ct_allow is not None and name not in _ct_allow:
            return False
        return is_tool_action_allowed(node, name, {})

    delegate_targets = [str(t).strip() for t in list(node.delegate_targets or []) if str(t).strip()]
    if delegate_targets and _ctl("dispatch_to"):
        # Register a single unified dispatch_to tool with dynamic node descriptions and target enum.
        openai_tools.append(_dispatch_to_spec(delegate_targets, downstream_info))

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
    _is_system_task = bool((task_context or {}).get("is_system_task"))
    if not _is_system_task and _ctl("switch_node"):
        _sw_targets = [info["id"] for info in (switch_info or [])]
        openai_tools.append(_switch_node_spec(
            _sw_targets, switch_info,
            current_node_id=node.id, current_node_name=node.name,
        ))

    # hybrid 模式下不注入 finish 工具：free prose 即隐式 finish。tool_only 模式下保留。
    _output_mode = getattr(node, 'output_mode', 'hybrid')
    if _output_mode == 'tool_only' and _ctl("finish"):
        openai_tools.append(_finish_spec())

    # ask 已下线不再向模型注入（底层 handler 仍保留兼容）。
    if _ctl("intermediate_reply"):
        openai_tools.append(_reply_spec())
    if _ctl("compact_context"):
        openai_tools.append(_compact_context_spec())
    if _ctl("spawn_worker"):
        openai_tools.append(_spawn_worker_spec())
    # manage_workers 是唯一的工作者控制入口；退役的 preempt_task / cancel_active_tasks
    # 不再注入、不再授权、也不再兼容执行。
    if _ctl("manage_workers"):
        openai_tools.append(_manage_workers_spec())
    return allowed, openai_tools
