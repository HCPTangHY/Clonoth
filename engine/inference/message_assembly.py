from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .dynamic_context import _load_dynamic_context_vars
from engine.faces.prompt_sections import PromptSectionContext, prompt_section_registry, content_sha1
from ..attachments import build_multimodal_content
from ..conversation_store import MessageType, is_dynamic_context
from ..node import Node
from ..prompt import assemble_prompt
from clonoth_runtime import get_int


_VOLATILE_PROMPT_DYNAMIC_LINE_RE = re.compile(
    r"^\s*(?:当前时间|Current\s+time|上下文使用率|Context\s+utilization|上下文占用|Context\s+usage)\s*[：:].*$",
    re.IGNORECASE | re.MULTILINE,
)

def _normalize_prompt_dynamic_for_hash(text: str) -> str:
    """Strip volatile time and context-utilization lines before hashing prompt_dynamic."""
    lines = [
        line.rstrip()
        for line in _VOLATILE_PROMPT_DYNAMIC_LINE_RE.sub("", str(text or "")).splitlines()
        if line.strip()
    ]
    return "\n".join(lines).strip()

def collect_active_injected_hashes(messages: list[dict[str, Any]] | None) -> dict[str, str]:
    """Scan current history/messages in order and merge surviving ``injected_hashes``.

    [Persistent Dynamic Context] Why: an in-memory session_id -> Set cache drifts
    whenever context is compacted, branches merge, or the engine restarts.
    How: walk the live message list (the single source of truth) and merge
    ``_meta.injected_hashes`` from every surviving ``dynamic_context`` message,
    with later entries overriding earlier ones. Purpose: compacted-away entries
    automatically disappear from ``active_injected`` and re-inject on demand,
    while kept segments stay untouched.
    """
    active: dict[str, str] = {}
    for m in messages or []:
        if not isinstance(m, dict) or not is_dynamic_context(m):
            continue
        meta = m.get("_meta") or m.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        hashes = meta.get("injected_hashes")
        if isinstance(hashes, dict):
            for k, v in hashes.items():
                if k and isinstance(v, str) and v:
                    active[str(k)] = v
    return active

def _build_dynamic_message(
    parts: list[str],
    injected_hashes: dict[str, str],
    *,
    is_block_mode: bool,
    source_task_id: str = "",
) -> dict[str, Any]:
    """Construct a persistable ``dynamic_context`` message with ``_meta.injected_hashes``."""
    prefix = (
        "Below is this turn's dynamic context; it may change between turns.\n\n"
        if is_block_mode
        else "Below is this turn's dynamic context information; it may change between turns. If it is irrelevant to the current task, ignore it and continue with your previous work.\n\n"
    )
    meta: dict[str, Any] = {
        "_dynamic": True,
        "message_type": MessageType.DYNAMIC_CONTEXT,
        "injected_hashes": dict(injected_hashes),
    }
    if source_task_id:
        meta["source_task_id"] = source_task_id
    return {
        "role": "user",
        "content": prefix + "\n\n".join(parts),
        "_dynamic": True,
        "message_type": MessageType.DYNAMIC_CONTEXT,
        "_meta": meta,
    }

def _collect_incremental_dynamic_parts(
    *,
    system_prompt: list[dict[str, Any]],
    is_block_mode: bool,
    skill_dynamic: list[dict[str, Any]],
    memory_dynamic: list[dict[str, Any]],
    section_entries: list[tuple[str, dict[str, str]]],
    active_injected: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Filter dynamic parts against ``active_injected`` and return ``(parts, new_hashes)``.

    [Persistent Dynamic Context] Why: only genuinely new or updated dynamic items
    should be emitted on a turn; unchanged items already sit in the uncompacted
    history. How: compare each item's sha1 against ``active_injected`` (using the
    time-normalized digest for ``prompt_dynamic``). Purpose: turns with no delta
    produce 0 dynamic messages and 0 extra tokens.
    """
    parts: list[str] = []
    new_hashes: dict[str, str] = {}

    if not is_block_mode and len(system_prompt) >= 2 and system_prompt[1].get("content"):
        raw_pd = str(system_prompt[1]["content"])
        norm_pd = _normalize_prompt_dynamic_for_hash(raw_pd)
        if norm_pd:
            pd_hash = content_sha1(norm_pd)
            if active_injected.get("prompt_dynamic") != pd_hash:
                parts.append(raw_pd)
                new_hashes["prompt_dynamic"] = pd_hash

    for idx, dm in enumerate(skill_dynamic):
        text = str(dm.get("content") or "").strip()
        if not text:
            continue
        item_hashes = dm.get("_injected_hashes")
        if not isinstance(item_hashes, dict) or not item_hashes:
            key = f"skill_block:{idx}"
            digest = content_sha1(text)
            if active_injected.get(key) == digest:
                continue
            item_hashes = {key: digest}
        parts.append(text)
        for k, v in item_hashes.items():
            new_hashes[str(k)] = str(v)

    for idx, dm in enumerate(memory_dynamic):
        text = str(dm.get("content") or "").strip()
        if not text:
            continue
        item_hashes = dm.get("_injected_hashes")
        if not isinstance(item_hashes, dict) or not item_hashes:
            key = f"memory_block:{idx}"
            digest = content_sha1(text)
            if active_injected.get(key) == digest:
                continue
            item_hashes = {key: digest}
        parts.append(text)
        for k, v in item_hashes.items():
            new_hashes[str(k)] = str(v)

    for text, sec_hashes in section_entries:
        if not text:
            continue
        # KnowledgeInjector already filtered per-entry against active_injected;
        # generic sections have a single section:<name> key that we check here.
        changed = {
            str(k): str(v)
            for k, v in (sec_hashes or {}).items()
            if active_injected.get(str(k)) != str(v)
        }
        if not changed:
            continue
        parts.append(text)
        new_hashes.update(changed)

    return parts, new_hashes

def _registry_dynamic_entries(
    section_context: PromptSectionContext | None,
) -> list[tuple[str, dict[str, str]]]:
    """Render registered dynamic sections with their deduplication hash maps."""
    if section_context is None:
        return []
    return prompt_section_registry.render_dynamic_entries(section_context)


def _registry_static_messages(
    section_context: PromptSectionContext | None,
) -> list[dict[str, Any]]:
    """Render registered static sections into cache-friendly pre-history messages."""
    if section_context is None:
        return []
    return prompt_section_registry.render_messages("static", section_context)


def _registry_dynamic_parts(section_context: PromptSectionContext | None) -> list[str]:
    """Render registered dynamic sections into per-turn dynamic content parts."""
    if section_context is None:
        return []
    return prompt_section_registry.render_scope("dynamic", section_context)


def conversational_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter history to only real conversation messages for keyword scanning.

    Excludes:
      - tool_result messages (tool execution output)
      - _dynamic injections (previously injected dynamic context)
      - messages with no text content (pure tool_call assistants)
    Keeps user_input, assistant text as turn boundaries, and legacy untyped
    messages. Types, not transport-specific text markers, define exclusions.
    """
    result: list[dict[str, Any]] = []
    for m in history:
        # Skip old dynamic context injections
        if is_dynamic_context(m):
            continue
        # Skip tool execution results, system rejection hints, and rejected drafts for keyword scanning
        _m_meta = m.get("_meta") or m.get("meta") or {}
        if not isinstance(_m_meta, dict):
            _m_meta = {}
        excluded = {
            "summary", "tool_result", "tool_result_attachment", "retry_hint",
            "output_rejected", "dispatch_result", "async_tool_result", "system_notice",
        }
        if m.get("message_type") in excluded or _m_meta.get("message_type") in excluded:
            continue
        if (_m_meta.get("rejected_draft") or _m_meta.get("is_rejection")
                or _m_meta.get("source_task_id") == "compact_summary"):
            continue
        # Only include messages with actual text content
        role = m.get("role", "")
        if role in ("user", "assistant"):
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                result.append(m)
            elif isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "text"
                and str(part.get("text") or "").strip() for part in content
            ):
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
    section_context: PromptSectionContext | None = None,
    inbound_message: dict[str, Any] | None = None,
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
    # Why: prompt sections are a plugin-facing contribution surface. How: render
    # the registry after plugin-provided static lists so built-in ordering is
    # unchanged when nothing is registered. Purpose: sections extend the prompt
    # without replacing the existing injection layout.
    section_static = _registry_static_messages(section_context)
    section_entries = _registry_dynamic_entries(section_context)
    active_injected = collect_active_injected_hashes(history)
    _dynamic_parts, _dynamic_hashes = _collect_incremental_dynamic_parts(
        system_prompt=system_prompt,
        is_block_mode=any(
            isinstance(m, dict) and m.get("role") == "history"
            for m in system_prompt
        ),
        skill_dynamic=skill_dynamic,
        memory_dynamic=memory_dynamic,
        section_entries=section_entries,
        active_injected=active_injected,
    )

    # ---- Prompt cache friendly layout ----
    # Detect block list mode: assemble_prompt returns blocks with {role: history}
    _is_block_mode = any(
        isinstance(m, dict) and m.get("role") == "history"
        for m in system_prompt
    )

    messages: list[dict[str, Any]] = []
    # Only a durable identity proves this is the same input. Equal text can be
    # a new turn (including a retry after a different task was cancelled).
    inbound_id = (inbound_message or {}).get("id")
    _already_in_history = bool(inbound_id) and any(
        m.get("id") == inbound_id for m in history
    )
    current_input = dict(inbound_message) if inbound_message else None
    if current_input is None and (instruction or attachments):
        current_input = {
            "role": "user",
            "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)
            if attachments else instruction,
        }

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
        messages.extend(section_static)

        # --- Conversation history ---
        messages.extend(history)

        # --- Instruction（在 history 之后、dynamic 之前，与 JSONL 落盘顺序一致） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if current_input is not None and not _already_in_history:
            messages.append(current_input)

        # --- Dynamic (在 instruction 之后，与 JSONL 落盘顺序 [User_N, Dynamic_N] 对齐) ---
        if _dynamic_parts:
            messages.append(_build_dynamic_message(
                _dynamic_parts, _dynamic_hashes, is_block_mode=True,
            ))

        # --- Post-history blocks (no depth) ---
        for _pb in _post_blocks:
            messages.append({k: v for k, v in _pb.items() if k != "depth"})

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
        # Stable prefix → history → instruction → dynamic suffix
        #
        # Dynamic content uses role=user instead of role=system so that
        # Anthropic/Gemini (which merge all system messages into a single
        # system field) keep a stable system cache across turns.

        # --- stable prefix (system) ---
        if system_prompt:
            messages.append(system_prompt[0])  # static part
        messages.extend(skill_static)
        messages.extend(memory_static)
        messages.extend(section_static)

        # --- history ---
        messages.extend(history)

        # --- instruction（固定历史前缀） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        if current_input is not None and not _already_in_history:
            messages.append(current_input)

        # --- dynamic (在 instruction 之后) ---
        if _dynamic_parts:
            messages.append(_build_dynamic_message(
                _dynamic_parts, _dynamic_hashes, is_block_mode=False,
            ))

    return messages, _is_block_mode


def _estimate_history_prompt_tokens(history: list[dict[str, Any]]) -> int:
    """Estimate usage from the last measured request, falling back to message sizes.

    ponytail: the fallback omits system and tool-schema tokens; this is a usage hint,
    not a replacement for the provider's token accounting.
    """
    from engine.builtin.compact import _seed_prompt_tokens
    from ..compact import estimate_message_tokens

    seeded = _seed_prompt_tokens(history)
    if seeded is not None:
        return max(0, int(seeded))
    return sum(estimate_message_tokens(m) for m in history if isinstance(m, dict))


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
    workspace_name: str = "",
    inbound_message: dict[str, Any] | None = None,
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
        # [2026-11-05 A2] Real/estimated usage instead of the implicit 0. The
        # utilization line is stripped before prompt_dynamic hashing, so a
        # changing value never triggers re-injection by itself.
        prompt_tokens=_estimate_history_prompt_tokens(history),
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
    ))
    system_prompt = assemble_prompt(workspace_root, node, variables=prompt_vars)
    section_context = PromptSectionContext(
        workspace_root=workspace_root,
        node=node,
        session_id=session_id,
        history=history,
        instruction=instruction,
        extra={"inbound_message": inbound_message},
        task_context=dict(task_context or {}),
        runtime_cfg=runtime_cfg,
        workspace_name=workspace_name,
    )
    # [Persistent Dynamic Context] Why: assemble_initial_messages and
    # assemble_messages_with_injections previously duplicated the entire block/string
    # layout. How: delegate to assemble_messages_with_injections now that both paths
    # share incremental hash deduplication. Purpose: one canonical assembly path.
    messages, _is_block_mode = assemble_messages_with_injections(
        workspace_root=workspace_root,
        system_prompt=system_prompt,
        history=history,
        instruction=instruction,
        attachments=attachments,
        section_context=section_context,
        inbound_message=inbound_message,
    )
    return messages, _is_block_mode, system_prompt


def rebuild_dynamic_context(
    messages: list[dict[str, Any]],
    *,
    workspace_root: Path,
    node: Node,
    runtime_cfg: dict[str, Any],
    history: list[dict[str, Any]],
    instruction: str,
    is_block_mode: bool,
    system_prompt: list[dict[str, Any]],
    session_id: str = "",
    workspace_name: str = "",
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append incremental ``dynamic_context`` for a mid-task preempt or post-compact resume.

    [Persistent Dynamic Context] Why: removing earlier ``_dynamic`` messages shifts
    message indices and invalidates the Prefix KV Cache. How: scan ``messages``
    for all surviving ``injected_hashes``, pass ``messages`` into the section
    context so KnowledgeInjector deduplicates against the live in-memory state,
    and append a new ``dynamic_context`` message only when genuinely new or updated
    entries appear. Purpose: keep all prior dynamic messages in place and return
    the newly appended message (or None) so callers can persist it to JSONL.
    """
    active_injected = collect_active_injected_hashes(messages)
    section_context = PromptSectionContext(
        workspace_root=workspace_root,
        node=node,
        session_id=session_id,
        history=messages,
        instruction=instruction,
        task_context=dict(task_context or {}),
        runtime_cfg=runtime_cfg,
        workspace_name=workspace_name,
    )
    section_entries = prompt_section_registry.render_dynamic_entries(section_context)
    parts, new_hashes = _collect_incremental_dynamic_parts(
        system_prompt=system_prompt,
        is_block_mode=is_block_mode,
        skill_dynamic=[],
        memory_dynamic=[],
        section_entries=section_entries,
        active_injected=active_injected,
    )
    if not parts:
        return None
    dyn_msg = _build_dynamic_message(
        parts, new_hashes, is_block_mode=is_block_mode,
    )
    messages.append(dyn_msg)
    return dyn_msg
