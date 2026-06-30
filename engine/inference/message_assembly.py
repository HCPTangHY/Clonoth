from __future__ import annotations

from pathlib import Path
from typing import Any

from .dynamic_context import _load_dynamic_context_vars
from ..attachments import build_multimodal_content
from ..node import Node
from ..prompt import assemble_prompt
from clonoth_runtime import get_int


def _conversational_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter history to only real conversation messages for keyword scanning.

    Excludes:
      - tool_result messages (tool execution output)
      - _dynamic injections (previously injected dynamic context)
      - messages with no text content (pure tool_call assistants)
    Keeps everything else: user_input, summary, assistant with text,
    and legacy messages without message_type.
    """
    result: list[dict[str, Any]] = []
    for m in history:
        # Skip old dynamic context injections
        if m.get("_dynamic"):
            continue
        # Skip tool execution results
        if m.get("message_type") == "tool_result":
            continue
        # Only include messages with actual text content
        role = m.get("role", "")
        if role in ("user", "assistant"):
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                result.append(m)
    return result


def assemble_messages_with_injections(
    *,
    workspace_root: Path,
    system_prompt: list[dict[str, Any]],
    history: list[dict[str, Any]],
    instruction: str,
    attachments: list[dict[str, Any]] | None = None,
    skill_static: list[dict[str, Any]] | None = None,
    skill_dynamic: list[dict[str, Any]] | None = None,
    memory_static: list[dict[str, Any]] | None = None,
    memory_dynamic: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Lay out prompt messages using precomputed skill and memory injections.

    Why: KnowledgeInjector must be able to rebuild the same prompt layout that
    assemble_initial_messages historically produced, without duplicating
    injection in two places. How: keep the block-mode and string-mode
    placement rules in this shared helper and pass precomputed static/dynamic
    injection messages into it. Purpose: allow before_prompt_build hooks to own
    skill and memory construction while preserving byte-level prompt layout.
    """
    skill_static = list(skill_static or [])
    skill_dynamic = list(skill_dynamic or [])
    memory_static = list(memory_static or [])
    memory_dynamic = list(memory_dynamic or [])

    # ---- Prompt cache friendly layout ----
    # Detect block list mode: assemble_prompt returns blocks with {role: history}
    _is_block_mode = any(
        isinstance(m, dict) and m.get("role") == "history"
        for m in system_prompt
    )

    messages: list[dict[str, Any]] = []

    if _is_block_mode:
        # === Block list mode ===
        # User controls message structure via prompt blocks.
        # {role: history} marks where conversation history is expanded.

        # Partition blocks around history marker
        _before_history: list[dict[str, Any]] = []
        _after_history: list[dict[str, Any]] = []
        _found_marker = False
        for _blk in system_prompt:
            if isinstance(_blk, dict) and _blk.get("role") == "history":
                _found_marker = True
                continue
            if _found_marker:
                _after_history.append(_blk)
            else:
                _before_history.append(_blk)

        # Separate depth-blocks from normal post-history blocks
        _depth_blocks: list[dict[str, Any]] = []
        _post_blocks: list[dict[str, Any]] = []
        for _pb in _after_history:
            if "depth" in _pb:
                _depth_blocks.append(_pb)
            else:
                _post_blocks.append(_pb)

        # --- Pre-history blocks (user-defined) ---
        messages.extend(_before_history)
        # Skill/memory static (cache-friendly, before history)
        messages.extend(skill_static)
        messages.extend(memory_static)

        # --- Conversation history ---
        messages.extend(history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- Dynamic (在 instruction 之前) ---
        _dynamic_parts: list[str] = []
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        if _dynamic_parts:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文，每轮可能变化。\n\n" + "\n\n".join(_dynamic_parts),
                "_dynamic": True,
            })

        # --- Post-history blocks (no depth) ---
        for _pb in _post_blocks:
            messages.append({k: v for k, v in _pb.items() if k != "depth"})

        # --- Instruction（始终在 dynamic/post_blocks 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)})
        elif instruction:
            messages.append({"role": "user", "content": instruction})

        # --- Depth blocks (insert from highest depth to lowest) ---
        if _depth_blocks:
            _depth_blocks.sort(key=lambda b: b.get("depth", 0), reverse=True)
            for _db in _depth_blocks:
                _d = int(_db.get("depth", 0))
                _clean = {k: v for k, v in _db.items() if k != "depth"}
                _insert_pos = max(0, len(messages) - _d)
                messages.insert(_insert_pos, _clean)

    else:
        # === String mode (existing behavior) ===
        # Stable prefix → history → dynamic suffix → instruction
        #
        # Dynamic content uses role=user instead of role=system so that
        # Anthropic/Gemini (which merge all system messages into a single
        # system field) keep a stable system cache across turns.

        # --- stable prefix (system) ---
        if system_prompt:
            messages.append(system_prompt[0])  # static part
        messages.extend(skill_static)
        messages.extend(memory_static)

        # --- history ---
        messages.extend(history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- dynamic (在 instruction 之前) ---
        _dynamic_parts_s: list[str] = []
        if len(system_prompt) >= 2 and system_prompt[1].get("content"):
            _dynamic_parts_s.append(system_prompt[1]["content"])
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        if _dynamic_parts_s:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n" + "\n\n".join(_dynamic_parts_s),
                "_dynamic": True,
            })

        # --- instruction（始终在 dynamic 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)})
        elif instruction:
            messages.append({"role": "user", "content": instruction})

    return messages, _is_block_mode


def assemble_initial_messages(
    *,
    workspace_root: Path,
    runtime_cfg: dict[str, Any],
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    task_context: dict[str, Any] | None = None,
    session_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    """构建初始 messages 数组。

    返回 (messages, is_block_mode, system_prompt_blocks)。
    system_prompt_blocks 是 assemble_prompt 的原始输出，供后续 preempt 注入使用。
    """
    prompt_vars: dict[str, str] = {
        "node_id": node.id,
        "node_name": node.name,
        "instruction": instruction,
    }
    # 合并 config/dynamic_context.yaml 定义的动态变量
    prompt_vars.update(_load_dynamic_context_vars(
        workspace_root,
        task_context=task_context,
        session_id=session_id,
        node_id=node.id,
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
    ))
    system_prompt = assemble_prompt(workspace_root, node, variables=prompt_vars)
    # Why: this function now builds only the prompt skeleton, while knowledge
    # content is added by before_prompt_build handlers. How: keep the existing
    # layout code but pass empty skill and memory lists. Purpose: preserve message
    # ordering without importing injection logic in the inference layer.
    skill_static, skill_dynamic = [], []
    memory_static, memory_dynamic = [], []

    # ---- Prompt cache friendly layout ----
    # Detect block list mode: assemble_prompt returns blocks with {role: history}
    _is_block_mode = any(
        isinstance(m, dict) and m.get("role") == "history"
        for m in system_prompt
    )

    messages: list[dict[str, Any]] = []

    if _is_block_mode:
        # === Block list mode ===
        # User controls message structure via prompt blocks.
        # {role: history} marks where conversation history is expanded.

        # Partition blocks around history marker
        _before_history: list[dict[str, Any]] = []
        _after_history: list[dict[str, Any]] = []
        _found_marker = False
        for _blk in system_prompt:
            if isinstance(_blk, dict) and _blk.get("role") == "history":
                _found_marker = True
                continue
            if _found_marker:
                _after_history.append(_blk)
            else:
                _before_history.append(_blk)

        # Separate depth-blocks from normal post-history blocks
        _depth_blocks: list[dict[str, Any]] = []
        _post_blocks: list[dict[str, Any]] = []
        for _pb in _after_history:
            if "depth" in _pb:
                _depth_blocks.append(_pb)
            else:
                _post_blocks.append(_pb)

        # --- Pre-history blocks (user-defined) ---
        messages.extend(_before_history)
        # Skill/memory static (cache-friendly, before history)
        messages.extend(skill_static)
        messages.extend(memory_static)

        # --- Conversation history ---
        messages.extend(history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- Dynamic (在 instruction 之前) ---
        _dynamic_parts: list[str] = []
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        if _dynamic_parts:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文，每轮可能变化。\n\n" + "\n\n".join(_dynamic_parts),
                "_dynamic": True,
            })

        # --- Post-history blocks (no depth) ---
        for _pb in _post_blocks:
            messages.append({k: v for k, v in _pb.items() if k != "depth"})

        # --- Instruction（始终在 dynamic/post_blocks 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)})
        elif instruction:
            messages.append({"role": "user", "content": instruction})

        # --- Depth blocks (insert from highest depth to lowest) ---
        if _depth_blocks:
            _depth_blocks.sort(key=lambda b: b.get("depth", 0), reverse=True)
            for _db in _depth_blocks:
                _d = int(_db.get("depth", 0))
                _clean = {k: v for k, v in _db.items() if k != "depth"}
                _insert_pos = max(0, len(messages) - _d)
                messages.insert(_insert_pos, _clean)

    else:
        # === String mode (existing behavior) ===
        # Stable prefix → history → dynamic suffix → instruction
        #
        # Dynamic content uses role=user instead of role=system so that
        # Anthropic/Gemini (which merge all system messages into a single
        # system field) keep a stable system cache across turns.

        # --- stable prefix (system) ---
        if system_prompt:
            messages.append(system_prompt[0])  # static part
        messages.extend(skill_static)
        messages.extend(memory_static)

        # --- history ---
        messages.extend(history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- dynamic (在 instruction 之前) ---
        _dynamic_parts_s: list[str] = []
        if len(system_prompt) >= 2 and system_prompt[1].get("content"):
            _dynamic_parts_s.append(system_prompt[1]["content"])
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        if _dynamic_parts_s:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n" + "\n\n".join(_dynamic_parts_s),
                "_dynamic": True,
            })

        # --- instruction（始终在 dynamic 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)})
        elif instruction:
            messages.append({"role": "user", "content": instruction})

    return messages, _is_block_mode, system_prompt
