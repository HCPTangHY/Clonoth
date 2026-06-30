"""动态上下文变量加载和格式化。

从 ai_step.py 中拆出。自包含，只依赖 yaml + datetime + logging。
"""
from __future__ import annotations

import logging as _logging
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
from pathlib import Path
from typing import Any

import yaml as _yaml

_dclog = _logging.getLogger("engine.dynamic_context")


def _load_dynamic_context_vars(
    workspace_root: Path,
    *,
    task_context: dict[str, Any] | None = None,
    session_id: str = "",
    node_id: str = "",
    prompt_tokens: int = 0,
    compact_threshold: int = 0,
) -> dict[str, str]:
    """从 config/dynamic_context.yaml 加载并求值动态变量。

    YAML 中每个 key 是变量名，value 是 Python 表达式。
    求值结果作为 {{key}} 模板变量供节点 prompt 使用。

    eval 上下文中可用：task_context (dict), session_id, node_id,
    prompt_tokens, compact_threshold, datetime, timezone, timedelta。
    以后需要传新数据只需往 supervisor 的 task_context 里加字段即可。
    """
    config_path = workspace_root / "config" / "dynamic_context.yaml"
    if not config_path.exists():
        return {}
    try:
        data = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    variables = data.get("variables")
    if not isinstance(variables, dict):
        return {}

    _tc = dict(task_context or {})
    # 构建 eval 上下文：task_context 整体 + 常用字段展开为顶层便于引用
    eval_ctx: dict[str, Any] = {
        "task_context": _tc,
        "session_id": session_id,
        "node_id": node_id,
        "prompt_tokens": prompt_tokens,
        "compact_threshold": compact_threshold,
        "datetime": _dt,
        "timezone": _tz,
        "timedelta": _td,
    }
    # 把 task_context 的所有 key 也展开到顶层，方便 YAML 里直接写 conversation_key 而非 task_context['conversation_key']
    eval_ctx.update(_tc)

    result: dict[str, str] = {}
    for name, expr in variables.items():
        if not isinstance(expr, str):
            continue
        try:
            val = eval(expr, {"__builtins__": __builtins__}, eval_ctx)  # noqa: S307
            result[str(name)] = str(val)
        except Exception as exc:
            _dclog.warning("dynamic_context var %r eval failed: %s (expr=%r)", name, exc, expr)
            result[str(name)] = ""
    return result


def _format_context_vars_block(vars_dict: dict[str, str]) -> str:
    """Format dynamic context vars into a delimited block for per-step updates.

    Returns a text block like:
        [CONTEXT_VARS]
        - beijing_time: 2026-04-13 08:22 CST
        - context_utilization: 45%
        [/CONTEXT_VARS]
    """
    if not vars_dict:
        return ""
    lines = []
    for k, v in vars_dict.items():
        if v:
            lines.append(f"- {k}: {v}")
    if not lines:
        return ""
    return "\n\n[CONTEXT_VARS]\n" + "\n".join(lines) + "\n[/CONTEXT_VARS]"
