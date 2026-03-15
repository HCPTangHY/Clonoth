from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "x").strip() or "x")[:80]


async def write_artifact(
    workspace_root: Path,
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    raw_format: str,
    raw_text: str,
) -> str:
    """把工具原始输出写入 artifact 文件，返回相对路径。"""
    d = workspace_root / "data" / "artifacts" / str(run_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    ext = ".json" if raw_format == "json" else ".txt"
    path = d / f"{_sanitize(tool_call_id)}_{_sanitize(tool_name)}{ext}"
    path.write_text(raw_text, encoding="utf-8")
    return str(path.relative_to(workspace_root))


def result_to_raw(tool_name: str, result: Any) -> tuple[str, str]:
    """把工具结果转为 (format, raw_text)。"""
    if tool_name == "read_file" and isinstance(result, dict):
        c = result.get("content")
        if isinstance(c, str) and c.strip():
            return "text", c
    if isinstance(result, dict) and "returncode" in result and isinstance(result.get("output"), str):
        return "text", f"returncode={result.get('returncode')}\n{result.get('output', '')}"
    try:
        return "json", json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return "json", str(result)


def summarize_result(tool_name: str, result: Any) -> str:
    """生成简短的工具结果摘要。"""
    if not isinstance(result, dict):
        return "已获得结果"
    if result.get("ok") is False:
        return f"失败: {result.get('error', 'unknown')}"
    if tool_name == "read_file":
        return f"已读取 {result.get('path', '')}"
    if tool_name == "execute_command":
        return f"命令完成 (rc={result.get('returncode')})"
    if tool_name == "write_file":
        return f"已写入 {result.get('path', '')}"
    return "已获得结果"


def format_tool_trace(entries: list[dict[str, Any]]) -> str:
    """把一批工具调用结果格式化为 CLONOTH_TOOL_TRACE 块。"""
    lines = ["[CLONOTH_TOOL_TRACE v1]"]
    for e in entries:
        lines.append(f"TOOL_CALL: {e['name']} {json.dumps(e.get('args', {}), ensure_ascii=False)}")
        lines.append(f"TOOL_RESULT_FORMAT: {e.get('format', 'json')}")
        lines.append(f"TOOL_RESULT_TRUNCATED: {str(e.get('truncated', False)).lower()}")
        if e.get("ref"):
            lines.append(f"TOOL_RESULT_REF: {e['ref']}")
        raw = e.get("raw_inline", "")
        if raw:
            lines.append("TOOL_RESULT_RAW: |")
            for ln in raw.splitlines():
                lines.append("  " + ln)
        else:
            lines.append("TOOL_RESULT_RAW: <empty>")
        lines.append(f"TOOL_RESULT_SUMMARY: {e.get('summary', '')}")
        atts = e.get("attachments")
        if isinstance(atts, list) and atts:
            att_paths = [str(a.get('path', '')) for a in atts if isinstance(a, dict) and a.get('path')]
            if att_paths:
                lines.append(f"TOOL_RESULT_ATTACHMENTS: {', '.join(att_paths)}")
        lines.append("-")
    lines.append("[/CLONOTH_TOOL_TRACE]")
    return "\n".join(lines)
