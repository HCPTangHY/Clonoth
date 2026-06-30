"""恢复消息构建和附件筛选。

从 ai_step.py 中拆出。依赖 engine.attachments 中的 build_multimodal_content。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..attachments import build_multimodal_content


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
