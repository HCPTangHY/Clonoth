"""Comprehensive tests for Persistent Incremental Dynamic Context and its 8 boundary conditions."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from toolbox.registry import ToolRegistry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin.compact import _seed_prompt_tokens
from engine.builtin.knowledge_inject import (
    KnowledgeInjector,
    _MemoryCache,
    _SkillCache,
    build_knowledge_messages,
)
from engine.builtin.memory_extract import _format_transcript_for_extract
from engine.builtin.preempt import _inject_preempt_message
from engine.compact import (
    _format_messages_for_summary,
    apply_compact_summary,
    split_task_segments,
)
from engine.compact_flow import prepare_compaction
from engine.conversation_store import (
    ConversationStore,
    Message,
    MessageType,
    is_dynamic_context,
)
from engine.faces.prompt_sections import (
    PromptSectionContext,
    PromptSectionRegistry,
    content_sha1,
    prompt_section_registry,
)
from engine.inference import ai_step
from engine.inference.message_assembly import (
    _normalize_prompt_dynamic_for_hash,
    assemble_initial_messages,
    assemble_messages_with_injections,
    collect_active_injected_hashes,
    conversational_history,
    rebuild_dynamic_context,
)
from providers.tool_format import create_tool_formatter
from engine.node import Node
from engine.runner import _message_to_history_dict


def _write_skill(ws: Path, name: str, body: str, *, keywords: list[str], priority: int = 10, scan_depth: int = 2) -> None:
    sdir = ws / "skills" / name
    sdir.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "name": name,
        "description": f"desc {name}",
        "enabled": True,
        "strategy": "normal",
        "keywords": keywords,
        "priority": priority,
        "scan_depth": scan_depth,
    }
    text = f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True)}---\n{body}\n"
    (sdir / "SKILL.md").write_text(text, encoding="utf-8")
    _SkillCache.invalidate(ws)

def _write_memories(ws: Path, entries: list[dict], *, book: str = "default") -> None:
    mdir = ws / "data" / "memory"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{book}.yaml").write_text(
        yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True),
        encoding="utf-8",
    )
    _MemoryCache.invalidate(ws, memory_book="")

def test_per_entry_skill_and_memory_dedup_and_zero_message_turn(tmp_path: Path) -> None:
    entries = [
        {
            "id": "discord_emojis",
            "kind": "skill",
            "content": "Emoji guide body",
            "strategy": "keyword",
            "keywords": ["emoji"],
            "compiled_keywords": ["emoji"],
            "priority": 10,
            "order": 1,
            "scan_depth": 2,
            "node_ids": [],
        },
        {
            "id": "git_workflow",
            "kind": "skill",
            "content": "Git workflow body",
            "strategy": "keyword",
            "keywords": ["git"],
            "compiled_keywords": ["git"],
            "priority": 10,
            "order": 2,
            "scan_depth": 2,
            "node_ids": [],
        },
        {
            "id": "rule_moonfanz",
            "kind": "memory",
            "content": "Do not modify negative prompt",
            "strategy": "keyword",
            "keywords": ["moonfanz"],
            "compiled_keywords": ["moonfanz"],
            "priority": 10,
            "order": 1,
            "scan_depth": 2,
            "node_ids": [],
            "book": "rules",
        },
    ]

    # Turn 1: triggers discord_emojis + rule_moonfanz
    _, sk_dyn1, _, mem_dyn1 = build_knowledge_messages(
        tmp_path, entries, instruction_text="use emoji for moonfanz", history=[],
    )
    sys_prompt = [
        {"role": "system", "content": "STATIC_SYS"},
        {"role": "system", "content": "当前频道：general\n当前时间：2026-11-04 10:01 CST\n上下文使用率：12%"},
    ]
    msgs1, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=sys_prompt,
        history=[],
        instruction="use emoji for moonfanz",
        skill_dynamic=sk_dyn1,
        memory_dynamic=mem_dyn1,
    )
    dyn_msgs1 = [m for m in msgs1 if is_dynamic_context(m)]
    assert len(dyn_msgs1) == 1
    hashes1 = dyn_msgs1[0]["_meta"]["injected_hashes"]
    assert set(hashes1.keys()) == {"prompt_dynamic", "skill:discord_emojis", "memory:rules/rule_moonfanz"}

    # Turn 2: same keywords + clock minute ticked -> 0 new dynamic messages!
    history_t2 = [m for m in msgs1 if m["role"] != "system"] + [
        {"role": "assistant", "content": "done turn 1"},
    ]
    _, sk_dyn2, _, mem_dyn2 = build_knowledge_messages(
        tmp_path, entries, instruction_text="another emoji for moonfanz", history=history_t2,
    )
    assert sk_dyn2 == []
    assert mem_dyn2 == []

    sys_prompt_t2 = [
        {"role": "system", "content": "STATIC_SYS"},
        {"role": "system", "content": "当前频道：general\n当前时间：2026-11-04 10:09 CST\n上下文使用率：48%"},
    ]
    msgs2, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=sys_prompt_t2,
        history=history_t2,
        instruction="another emoji for moonfanz",
        skill_dynamic=sk_dyn2,
        memory_dynamic=mem_dyn2,
    )
    # Only the original dynamic_context from Turn 1 exists; Turn 2 added 0 dynamic messages
    dyn_msgs2 = [m for m in msgs2 if is_dynamic_context(m)]
    assert len(dyn_msgs2) == 1
    assert msgs2[-1] == {"role": "user", "content": "another emoji for moonfanz"}

    # Turn 3: both emoji (already injected) and git (new) match -> only git_workflow is injected!
    history_t3 = [m for m in msgs2 if m["role"] != "system"] + [
        {"role": "assistant", "content": "done turn 2"},
    ]
    _, sk_dyn3, _, mem_dyn3 = build_knowledge_messages(
        tmp_path, entries, instruction_text="use emoji and git", history=history_t3,
    )
    assert len(sk_dyn3) == 1
    assert "git_workflow" in sk_dyn3[0]["content"]
    assert "discord_emojis" not in sk_dyn3[0]["content"]
    assert set(sk_dyn3[0]["_injected_hashes"].keys()) == {"skill:git_workflow"}

def test_outline_stays_active_until_source_edit(tmp_path: Path) -> None:
    long_body = "# Heading 1\nIntro line\n" + ("detailed guideline line\n" * 60)
    entries = [
        {
            "id": "high_prio",
            "kind": "skill",
            "content": "High priority content " * 20,
            "strategy": "keyword",
            "keywords": ["high"],
            "compiled_keywords": ["high"],
            "priority": 100,
            "order": 1,
            "scan_depth": 0,
            "node_ids": [],
        },
        {
            "id": "large_skill",
            "kind": "skill",
            "content": long_body,
            "strategy": "keyword",
            "keywords": ["large"],
            "compiled_keywords": ["large"],
            "priority": 10,
            "order": 2,
            "scan_depth": 0,
            "node_ids": [],
        },
    ]
    # Turn 1: tight budget forces large_skill into _outline_only
    _, sk_dyn1, _, _ = build_knowledge_messages(
        tmp_path,
        entries,
        instruction_text="high and large",
        history=[],
        skill_max_budget_chars=1200,
    )
    assert len(sk_dyn1) == 1
    assert "[Large skill" in sk_dyn1[0]["content"]
    msgs1, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=[{"role": "system", "content": "SYS"}],
        history=[],
        instruction="high and large",
        skill_dynamic=sk_dyn1,
    )
    history = [m for m in msgs1 if m["role"] != "system"]

    # Round4: increasing budget alone must not resend an unchanged outline.
    _, sk_dyn2, _, _ = build_knowledge_messages(
        tmp_path,
        entries,
        instruction_text="only large now",
        history=history,
        skill_max_budget_chars=5000,
    )
    assert sk_dyn2 == []

    # A source edit, unlike a budget change, must still re-inject current content.
    msgs2, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=[{"role": "system", "content": "SYS"}],
        history=history,
        instruction="only large now",
        skill_dynamic=sk_dyn2,
    )
    history2 = [m for m in msgs2 if m["role"] != "system"]
    entries[1] = {**entries[1], "content": long_body + "\nNEWLY_EDITED_SECTION"}
    _, sk_dyn3, _, _ = build_knowledge_messages(
        tmp_path,
        entries,
        instruction_text="only large now",
        history=history2,
        skill_max_budget_chars=5000,
    )
    assert len(sk_dyn3) == 1
    assert "NEWLY_EDITED_SECTION" in sk_dyn3[0]["content"]

def test_normalize_prompt_dynamic_strips_time_and_context_utilization() -> None:
    t1 = "当前频道：dev\n当前时间：2026-11-04 12:00 CST\n上下文使用率：15%\nCurrent time: 12:00\nContext utilization: 15%"
    t2 = "当前频道：dev\n当前时间：2026-11-04 12:45 CST\n上下文使用率：89%\nCurrent time: 12:45\nContext utilization: 89%"
    t3 = "当前频道：general\n当前时间：2026-11-04 12:45 CST\n上下文使用率：89%"
    assert _normalize_prompt_dynamic_for_hash(t1) == _normalize_prompt_dynamic_for_hash(t2)
    assert _normalize_prompt_dynamic_for_hash(t1) != _normalize_prompt_dynamic_for_hash(t3)

def test_compact_preserves_kept_dynamic_and_reinjects_compacted_dynamic(tmp_path: Path) -> None:
    entries = [
        {
            "id": "skill_old",
            "kind": "skill",
            "content": "Old skill body",
            "strategy": "keyword",
            "keywords": ["oldkw"],
            "compiled_keywords": ["oldkw"],
            "priority": 10,
            "order": 1,
            "scan_depth": 0,
            "node_ids": [],
        },
        {
            "id": "skill_recent",
            "kind": "skill",
            "content": "Recent skill body",
            "strategy": "keyword",
            "keywords": ["recentkw"],
            "compiled_keywords": ["recentkw"],
            "priority": 10,
            "order": 2,
            "scan_depth": 0,
            "node_ids": [],
        },
    ]

    # Build Task 1 (has skill_old) and Task 2 (has skill_recent)
    _, sk_d1, _, _ = build_knowledge_messages(tmp_path, entries, instruction_text="use oldkw", history=[])
    m_t1, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=[{"role": "system", "content": "SYS"}],
        history=[],
        instruction="use oldkw",
        skill_dynamic=sk_d1,
    )
    seg1 = [m for m in m_t1 if m["role"] != "system"]
    for m in seg1:
        m.setdefault("_meta", {})["source_task_id"] = "task_1"
    seg1.append({"role": "assistant", "content": "ans 1", "_meta": {"source_task_id": "task_1"}})

    _, sk_d2, _, _ = build_knowledge_messages(tmp_path, entries, instruction_text="use recentkw", history=seg1)
    m_t2, _ = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=[{"role": "system", "content": "SYS"}],
        history=seg1,
        instruction="use recentkw",
        skill_dynamic=sk_d2,
    )
    seg2 = m_t2[1 + len(seg1):]
    for m in seg2:
        m.setdefault("_meta", {})["source_task_id"] = "task_2"
    seg2.append({"role": "assistant", "content": "ans 2", "_meta": {"source_task_id": "task_2"}})

    all_msgs = [{"role": "system", "content": "SYS"}] + seg1 + seg2

    # Boundary 2 check: split_task_segments keeps dynamic_context in its task segment
    _, segments = split_task_segments(seg1 + seg2)
    assert len(segments) == 2
    assert any(is_dynamic_context(m) for m in segments[0])
    assert any(is_dynamic_context(m) for m in segments[1])

    # Boundary 2 check: _format_messages_for_summary skips dynamic_context bodies
    summary_input = _format_messages_for_summary(segments[0])
    assert "Old skill body" not in summary_input
    assert "use oldkw" in summary_input

    # Boundary 2 check: prepare_compaction keeps dynamic_context in segments but omits from conversation_text
    plan = asyncio.run(prepare_compaction(all_msgs, "sid_1", "node_1", keep_recent=1))
    assert plan.status == "dispatch"
    assert "Old skill body" not in plan.conversation_text

    # Apply compaction with keep_recent=1 -> seg1 replaced by summary, seg2 kept with its dynamic_context!
    compacted = apply_compact_summary(all_msgs, "Summary of task 1", keep_recent=1)
    kept_history = [m for m in compacted if m["role"] != "system"]
    active_after_compact = collect_active_injected_hashes(kept_history)
    assert "skill:skill_old" not in active_after_compact
    assert "skill:skill_recent" in active_after_compact

    # Boundary 1 check: next turn asks for both oldkw and recentkw ->
    # skill_old (compacted away) IS re-injected; skill_recent (in kept segment) is NOT re-injected!
    _, sk_d3, _, _ = build_knowledge_messages(
        tmp_path, entries, instruction_text="need oldkw and recentkw again", history=kept_history,
    )
    assert len(sk_d3) == 1
    assert "Old skill body" in sk_d3[0]["content"]
    assert "Recent skill body" not in sk_d3[0]["content"]

def test_jsonl_persistence_order_and_history_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill(tmp_path, "alpha_skill", "Alpha secret body mentions beta_kw", keywords=["alpha_kw"], scan_depth=2)
    _write_skill(tmp_path, "beta_skill", "Beta secret body", keywords=["beta_kw"], scan_depth=2)

    store = ConversationStore(tmp_path / "data" / "conversations")
    sid = "sess_order"

    # Simulate runner.py writing User_1 before calling run_ai_node
    store.append(sid, Message(
        id="u1",
        role="user",
        content="please use alpha_kw",
        message_type=MessageType.USER_INPUT,
        source_node_id="test_node",
        source_task_id="task_1",
    ))

    node = Node(
        id="test_node",
        type="ai",
        name="Test",
        prompt="STATIC_PROMPT\n# %%DYNAMIC%%\n当前频道：general\n当前时间：2026-11-04 12:00 CST",
        tool_mode="native",
    )
    rctx = SimpleNamespace(
        workspace_root=tmp_path,
        session_id=sid,
        child_session_id="",
        parent_session_id="",
        task_id="task_1",
        user_text="please use alpha_kw",
        workspace_name="",
        task_context={"conversation_key": "general"},
        conversation_store=store,
        first_shadow_message_id="u1",
        last_shadow_message_id="u1",
        response_info={},
        tool_call_log=[],
        total_usage={},
        completed_steps=0,
        check_cancelled=lambda: asyncio.sleep(0, result=False),
        check_preempted=lambda: asyncio.sleep(0, result={"preempted": False}),
        emit_event=lambda *a, **kw: asyncio.sleep(0),
    )

    async def fake_call_llm(ls, step):
        # Record assistant message via shadow write just like real loop
        asst = {"role": "assistant", "content": "Here is the answer", "_meta": {"tool_mode": "native"}}
        ls.messages.append(asst)
        ai_step._shadow_write(ls, asst, MessageType.ASSISTANT)
        from engine.protocol import TaskAction
        # TaskAction has no `text` field; finish payload lives in `result` (engine/protocol.py).
        return TaskAction(action="finish", node_id="test_node", result={"text": "done"})

    monkeypatch.setattr(ai_step, "_call_llm_with_retry", fake_call_llm)
    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *a, **kw: None)

    stored_before = store.load(sid)
    history_t1 = [_message_to_history_dict(m, current_task_id="task_1") for m in stored_before[:-1]]

    asyncio.run(ai_step.run_ai_node(
        rctx=rctx,
        provider=SimpleNamespace(name="openai"),
        # ToolRegistry requires workspace_root/tools_dir (keyword-only); same pattern as test_hooks.py.
        registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
        node=node,
        instruction="please use alpha_kw",
        history=history_t1,
        run_id="task_1",
    ))

    persisted = store.load(sid)
    # Strict order in JSONL: [User_1, Dynamic_1, Assistant_1]
    assert [m.message_type for m in persisted] == [
        MessageType.USER_INPUT,
        MessageType.DYNAMIC_CONTEXT,
        MessageType.ASSISTANT,
    ]
    assert "skill:alpha_skill" in persisted[1].meta["injected_hashes"]
    assert "prompt_dynamic" in persisted[1].meta["injected_hashes"]

    # Boundary 6: reload via _message_to_history_dict preserves _dynamic and injected_hashes,
    # and conversational_history filters Dynamic_1 so "beta_kw" inside alpha_skill's body
    # does NOT self-trigger beta_skill on Turn 2!
    reloaded_history = [_message_to_history_dict(m, current_task_id="task_2") for m in persisted]
    assert reloaded_history[1]["_dynamic"] is True
    assert reloaded_history[1]["message_type"] == "dynamic_context"
    assert "skill:alpha_skill" in reloaded_history[1]["_meta"]["injected_hashes"]

    conv_only = conversational_history(reloaded_history)
    assert all(not is_dynamic_context(m) for m in conv_only)

    # Turn 2 with unrelated instruction (scan_depth=2): alpha_skill is already in reloaded_history
    # and beta_kw only appeared inside Dynamic_1 (filtered out), so Turn 2 emits 0 dynamic messages!
    msgs_t2, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="unrelated follow up question",
        history=reloaded_history,
        task_context={"conversation_key": "general"},
        session_id=sid,
    )
    dyn_t2 = [m for m in msgs_t2 if is_dynamic_context(m)]
    assert len(dyn_t2) == 1  # Only the historical Dynamic_1 from Turn 1

def test_compact_done_resume_reinjects_compacted_skill_while_normal_resume_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(tmp_path, "resume_skill", "Resume skill body", keywords=["resumekw"], scan_depth=2)
    store = ConversationStore(tmp_path / "data" / "conversations")
    sid = "sess_resume"

    node = Node(
        id="test_node",
        type="ai",
        name="Test",
        prompt="STATIC\n# %%DYNAMIC%%\n当前频道：general",
        tool_mode="native",
    )

    # Case A: Normal child_result resume where Dynamic_1 is already in JSONL history -> no duplicate
    store.append(sid, Message(
        id="u1", role="user", content="use resumekw", message_type=MessageType.USER_INPUT, source_task_id="t1",
    ))
    # Run initial assembly to get exact hashes
    init_msgs, _, _ = assemble_initial_messages(
        workspace_root=tmp_path, runtime_cfg={}, node=node, instruction="use resumekw", history=[],
        task_context={"conversation_key": "general"}, session_id=sid,
    )
    dyn_init = next(m for m in init_msgs if is_dynamic_context(m))
    store.append(sid, Message(
        id="d1", role="user", content=dyn_init["content"],
        message_type=MessageType.DYNAMIC_CONTEXT, meta=dyn_init["_meta"], source_task_id="t1",
    ))
    store.append(sid, Message(
        id="a1", role="assistant", content="called child", message_type=MessageType.ASSISTANT, source_task_id="t1",
    ))

    history_normal = [_message_to_history_dict(m, current_task_id="t1") for m in store.load(sid)]
    rctx_normal = SimpleNamespace(
        workspace_root=tmp_path, session_id=sid, child_session_id="", parent_session_id="",
        task_id="t1", user_text="use resumekw", workspace_name="",
        task_context={"conversation_key": "general"}, conversation_store=store,
        first_shadow_message_id="u1", last_shadow_message_id="a1", response_info={},
        tool_call_log=[], total_usage={}, completed_steps=0,
    )

    async def stop_before_step(point, *a, **kw):
        # Only stop at before_step. Returning an action from before_prompt_build
        # makes run_ai_node exit before the compact_done re-injection and the
        # Boundary 3 persistence run, so Case B could never observe the new row.
        from engine.protocol import TaskAction
        if point == "before_step":
            return SimpleNamespace(action=TaskAction(action="finish", node_id="test_node"), skip_step=False)
        return SimpleNamespace(action=None, skip_step=False)

    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(ai_step.hook_registry, "afire", stop_before_step)

    asyncio.run(ai_step.run_ai_node(
        rctx=rctx_normal,
        provider=SimpleNamespace(name="openai"),
        registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
        node=node,
        instruction="",  # runner clears instruction on resume
        history=history_normal,
        run_id="t1",
        resume_data={"type": "child_result", "from_node": "worker", "result": {"text": "child output"}},
    ))
    # The callback is durable for restart recovery, but dynamic context is not duplicated.
    after_normal = store.load(sid)
    assert [m.id for m in after_normal if m.message_type != "resume_input"] == ["u1", "d1", "a1"]
    callbacks = [m for m in after_normal if m.message_type == "resume_input"]
    assert len(callbacks) == 1 and "child output" in callbacks[0].content

    # Case B: compact_done resume where d1 was in an older segment that got compacted away!
    sid_compact = "sess_compact_resume"
    store.append(sid_compact, Message(
        id="sum1", role="user",
        content="[Below is a structured summary of the previous conversation; the original context has been compacted]\n\nSummary",
        message_type=MessageType.SUMMARY, source_task_id="compact_summary",
    ))
    store.append(sid_compact, Message(
        id="u2", role="user", content="continue resumekw work",
        message_type=MessageType.USER_INPUT, source_task_id="t2",
    ))
    history_after_compact = [_message_to_history_dict(m, current_task_id="t2") for m in store.load(sid_compact)]
    rctx_compact = SimpleNamespace(
        workspace_root=tmp_path, session_id=sid_compact, child_session_id="", parent_session_id="",
        task_id="t2", user_text="continue resumekw work", workspace_name="",
        task_context={"conversation_key": "general"}, conversation_store=store,
        first_shadow_message_id="u2", last_shadow_message_id="u2", response_info={},
        tool_call_log=[], total_usage={}, completed_steps=0,
        emit_event=lambda *a, **kw: asyncio.sleep(0),
    )
    asyncio.run(ai_step.run_ai_node(
        rctx=rctx_compact,
        provider=SimpleNamespace(name="openai"),
        registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
        node=node,
        instruction="",  # cleared on resume by runner
        history=history_after_compact,
        run_id="t2",
        resume_data={"type": "compact_done", "success": True, "kept_segments": 1},
    ))
    after_rows = store.load(sid_compact)
    assert len(after_rows) == 3
    assert after_rows[-1].message_type == MessageType.DYNAMIC_CONTEXT
    assert "skill:resume_skill" in after_rows[-1].meta["injected_hashes"]

def test_preempt_preserves_prior_dynamic_and_appends_incremental_after_user_msg(tmp_path: Path) -> None:
    _write_skill(tmp_path, "skill_a", "Body A", keywords=["kwa"], scan_depth=2)
    _write_skill(tmp_path, "skill_b", "Body B", keywords=["kwb"], scan_depth=2)

    # Ensure KnowledgeInjector is registered on prompt_section_registry
    from engine.context import EngineContext
    from providers import registry as prov_reg
    from engine.hooks import hook_registry
    ectx = EngineContext(providers=prov_reg, hooks=hook_registry)
    ectx.contributions.mount("prompt_sections", prompt_section_registry)
    KnowledgeInjector(ectx)

    store = ConversationStore(tmp_path / "data" / "conversations")
    sid = "sess_preempt"
    node = Node(id="test_node", type="ai", name="Test", prompt="STATIC\n# %%DYNAMIC%%\n当前频道：general")

    # Initial turn with kwa
    store.append(sid, Message(id="u1", role="user", content="run kwa", message_type=MessageType.USER_INPUT, source_task_id="t1"))
    msgs, is_block, sys_prompt = assemble_initial_messages(
        workspace_root=tmp_path, runtime_cfg={}, node=node, instruction="run kwa",
        history=[], task_context={"conversation_key": "general"}, session_id=sid,
    )
    dyn1 = next(m for m in msgs if is_dynamic_context(m))
    store.append(sid, Message(
        id="d1", role="user", content=dyn1["content"],
        message_type=MessageType.DYNAMIC_CONTEXT, meta=dyn1["_meta"], source_task_id="t1",
    ))
    dyn1["id"] = "d1"
    dyn1["_persisted_dynamic"] = True

    # Simulate Step 1 tool call & result after Dynamic_1
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "res1"})

    rctx = SimpleNamespace(
        workspace_root=tmp_path,
        session_id=sid,
        child_session_id="",
        task_id="t1",
        workspace_name="",
        task_context={"conversation_key": "general"},
        conversation_store=store,
        first_shadow_message_id="u1",
        last_shadow_message_id="d1",
        response_info={},
        consume_preempt=lambda *a: asyncio.sleep(0),
        emit_event=lambda *a, **kw: asyncio.sleep(0),
    )
    ls = SimpleNamespace(
        rctx=rctx,
        node=node,
        runtime_cfg={},
        messages=msgs,
        history=[],
        is_block_mode=is_block,
        system_prompt=sys_prompt,
        preempt_inject_info={"message": "now also use kwb and kwa", "attachments": []},
        plaintext_retry_count=0,
        compacted=False,
        last_shadow_message_id="d1",
    )

    asyncio.run(_inject_preempt_message(SimpleNamespace(step=1), ls))

    # 1) Original Dynamic_1 is still in place right after User_1 (never deleted!)
    assert dyn1 in ls.messages
    assert ls.messages.index(dyn1) < len(ls.messages) - 2
    # 2) Preempt user message is at index -2, followed by ΔDynamic_Preempt at index -1
    assert ls.messages[-2]["role"] == "user"
    assert ls.messages[-2]["content"] == "now also use kwb and kwa"
    assert is_dynamic_context(ls.messages[-1])
    assert "Body B" in ls.messages[-1]["content"]
    assert "Body A" not in ls.messages[-1]["content"]
    assert set(ls.messages[-1]["_meta"]["injected_hashes"].keys()) == {"skill:skill_b"}

    # 3) JSONL order: [u1, d1, preempt_user, delta_dyn]
    disk_msgs = store.load(sid)
    assert [m.message_type for m in disk_msgs] == [
        MessageType.USER_INPUT,
        MessageType.DYNAMIC_CONTEXT,
        MessageType.USER_INPUT,
        MessageType.DYNAMIC_CONTEXT,
    ]
    assert disk_msgs[2].content == "now also use kwb and kwa"
    assert disk_msgs[3].meta["injected_hashes"] == {"skill:skill_b": ls.messages[-1]["_meta"]["injected_hashes"]["skill:skill_b"]}

def test_seed_prompt_tokens_counts_delta_dynamic_tail() -> None:
    messages = [
        {"role": "user", "content": "turn 1"},
        {
            "role": "assistant",
            "content": "reply 1",
            "_meta": {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
        },
        {"role": "user", "content": "turn 2"},
        {
            "role": "user",
            "content": "A" * 600,
            "_dynamic": True,
            "message_type": "dynamic_context",
        },
    ]
    seeded = _seed_prompt_tokens(messages)
    # 1000 + 100 + (len("turn 2") + 600) // 2 = 1403
    assert seeded == 1000 + 100 + (len("turn 2") + 600) // 2

def test_knowledge_injector_single_compute_and_same_length_outline_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify KnowledgeInjector._compute caches across render_static/render_dynamic
    within one assembly pass, and outline mode detects same-length body edits in
    the tail of a large skill/memory without altering outline text layout.
    """
    import engine.builtin.knowledge_inject as ki

    _write_skill(
        tmp_path,
        "bigskill",
        keywords=["bigkw"],
        body="Intro line\n\n# Section 1\n" + ("A" * 6200) + "TAIL_V1",
    )
    node = Node(id="t", name="t", type="ai", prompt="Static\n# %%DYNAMIC%%\nDyn")

    call_count = 0
    orig_bkc = ki.build_knowledge_context

    def counting_bkc(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_bkc(*args, **kwargs)

    monkeypatch.setattr(ki, "build_knowledge_context", counting_bkc)

    msgs1, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="use bigkw now",
        history=[],
    )
    # render_static and render_dynamic share the same PromptSectionContext -> 1 call
    assert call_count == 1
    dyn1 = [m for m in msgs1 if is_dynamic_context(m)]
    assert len(dyn1) == 1
    assert "[Large skill" in dyn1[0]["content"]

    # Turn 2 with unchanged file -> 0 new dynamic messages
    history = [m for m in msgs1 if m.get("role") != "system"]
    msgs2, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="use bigkw again",
        history=history,
    )
    assert len([m for m in msgs2 if is_dynamic_context(m)]) == 1

    # Same-length edit at the very end of the >6000-char body (outside preview & headings)
    _write_skill(
        tmp_path,
        "bigskill",
        keywords=["bigkw"],
        body="Intro line\n\n# Section 1\n" + ("A" * 6200) + "TAIL_V2",
    )
    msgs3, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="use bigkw after same-length edit",
        history=history,
    )
    dyn3 = [m for m in msgs3 if is_dynamic_context(m)]
    assert len(dyn3) == 2
    assert "skill:bigskill" in dyn3[-1]["_meta"]["injected_hashes"]

def test_workspace_context_preserves_prefix_cache_across_steps_turns_resume_and_switch(tmp_path: Path) -> None:
    """P0 Regression: WorkspaceContext must place _workspace_context after User_N,
    persist & restore _workspace_context via JSONL without duplicate 8000-char trees,
    keep persisted blocks immutable across multi-step file edits, and append at the
    tail when set_workspace switches workspace.
    """
    import asyncio
    from engine.builtin.workspace_context import WorkspaceContext
    from engine.conversation_store import ConversationStore, Message, MessageType
    from engine.hooks.types import HookContext
    from engine.inference.ai_step import _persist_dynamic_messages, _shadow_write
    from engine.runner import _message_to_history_dict

    install_dir = tmp_path / "install"
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    install_dir.mkdir()
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "a_initial.py").write_text("x = 1", encoding="utf-8")
    (proj_b / "b_file.py").write_text("y = 2", encoding="utf-8")

    store = ConversationStore(install_dir)
    sid = "ws-cache-session"
    node = Node(
        id="coder",
        name="coder",
        type="ai",
        prompt="STATIC_SYS\n# %%DYNAMIC%%\n当前时间：2026-11-05 08:00 CST\nNode: {{node_id}}",
        extra={"workspace_context": True},
    )
    wc = WorkspaceContext()

    async def _scenario():
        # --- Turn 1, Step 0: runner writes User_1 first, then run_ai_node assembles & persists ---
        store.append(sid, Message(id="u1", role="user", content="implement feature A", message_type=MessageType.USER_INPUT))
        hist_t1 = [_message_to_history_dict(m) for m in store.load(sid) if m.role != "system"]
        msgs_t1, _, _ = assemble_initial_messages(
            workspace_root=install_dir,
            runtime_cfg={},
            node=node,
            instruction="implement feature A",
            history=hist_t1,
            inbound_message=hist_t1[-1],
            workspace_name="proj-A",
        )
        rctx = SimpleNamespace(
            workspace_root=install_dir,
            workspace=proj_a,
            workspace_name="proj-A",
            task_context={},
            session_id=sid,
            child_session_id="",
            task_id="task-1",
            conversation_store=store,
            first_shadow_message_id="",
            last_shadow_message_id="",
            response_info={},
        )
        ls = SimpleNamespace(rctx=rctx, node=node, messages=msgs_t1, _cache_warm_fork=False)
        await wc.handle(HookContext(messages=msgs_t1, tools=[], node=node, provider=None, rctx=rctx, step=0))
        _persist_dynamic_messages(ls)

        # Memory order must be [System, User_1, Dynamic_1, Workspace_1] matching JSONL order
        non_sys_t1 = [m for m in msgs_t1 if m["role"] != "system"]
        assert non_sys_t1[0]["content"] == "implement feature A"
        assert is_dynamic_context(non_sys_t1[1]) and not non_sys_t1[1].get("_workspace_context")
        assert non_sys_t1[2].get("_workspace_context") is True
        assert non_sys_t1[2].get("_persisted_dynamic") is True

        # Step 0 before_step and Step 1 before_step after creating a file in proj_a:
        # persisted _workspace_context must NOT be mutated in-place (100% prefix stability)
        prefix_s0 = [dict(m) for m in msgs_t1]
        (proj_a / "new_during_step.py").write_text("z = 3", encoding="utf-8")
        res_s1 = await wc.handle(HookContext(messages=msgs_t1, tools=[], node=node, provider=None, rctx=rctx, step=1))
        assert res_s1 is None
        assert msgs_t1 == prefix_s0

        # Assistant reply persisted at end of Turn 1
        asst_1 = {"role": "assistant", "content": "Feature A done"}
        msgs_t1.append(asst_1)
        _shadow_write(ls, asst_1, MessageType.ASSISTANT)

        # --- Turn 2: reload from JSONL -> 0 new workspace blocks, 100% prefix match with Turn 1 ---
        store.append(sid, Message(id="u2", role="user", content="check status", message_type=MessageType.USER_INPUT))
        hist_t2 = [_message_to_history_dict(m) for m in store.load(sid) if m.role != "system"]
        # Hydrated history must match Turn 1 non-system messages in exact order and content
        assert [m["content"] for m in hist_t2[:-1]] == [m["content"] for m in msgs_t1 if m["role"] != "system"]
        assert sum(1 for m in hist_t2 if m.get("_workspace_context")) == 1

        msgs_t2, _, _ = assemble_initial_messages(
            workspace_root=install_dir,
            runtime_cfg={},
            node=node,
            instruction="check status",
            history=hist_t2,
            inbound_message=hist_t2[-1],
            workspace_name="proj-A",
        )
        ls2 = SimpleNamespace(rctx=rctx, node=node, messages=msgs_t2, _cache_warm_fork=False)
        res_t2 = await wc.handle(HookContext(messages=msgs_t2, tools=[], node=node, provider=None, rctx=rctx, step=0))
        assert res_t2 is None
        _persist_dynamic_messages(ls2)
        assert [m["content"] for m in msgs_t2[: len(msgs_t1)]] == [m["content"] for m in msgs_t1]
        assert sum(1 for m in store.load(sid) if (m.meta or {}).get("_workspace_context")) == 1

        # --- Workspace switch via set_workspace in Step 1 of Turn 2 ---
        rctx.workspace = proj_b
        rctx.workspace_name = "proj-B"
        prefix_before_switch = [m["content"] for m in msgs_t2]
        res_sw = await wc.handle(HookContext(messages=msgs_t2, tools=[], node=node, provider=None, rctx=rctx, step=1))
        assert res_sw is not None and res_sw.modified is True
        _persist_dynamic_messages(ls2)
        # Prefix before switch is 100% untouched; new workspace block appended at tail
        assert [m["content"] for m in msgs_t2[:-1]] == prefix_before_switch
        assert "b_file.py" in msgs_t2[-1]["content"]
        # Old workspace rows remain byte-for-byte intact; latest marker wins.
        assert sum(1 for m in msgs_t2 if m.get("_workspace_context")) == 2

    asyncio.run(_scenario())

def test_block_mode_places_instruction_before_dynamic_and_keeps_cross_turn_cache(tmp_path: Path) -> None:
    """P1 Regression: Block mode ({role: history}) must place User_N before Dynamic_N
    so in-memory order matches JSONL [User_N, Dynamic_N] across turns and resumes.
    """
    _write_skill(tmp_path, "blk_skill", keywords=["blk_kw"], body="Block mode skill content")
    node = Node(
        id="blk",
        name="blk",
        type="ai",
        prompt=[
            {"role": "system", "content": "PRE_SYS"},
            {"role": "history"},
        ],
    )
    msgs_t1, is_blk, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="trigger blk_kw",
        history=[],
    )
    assert is_blk is True
    non_sys = [m for m in msgs_t1 if m["role"] != "system"]
    assert non_sys[0]["content"] == "trigger blk_kw"
    assert is_dynamic_context(non_sys[1])

    # Simulate JSONL reload for Turn 2 (User_1 followed by Dynamic_1 followed by Assistant_1)
    hist_t2 = non_sys + [{"role": "assistant", "content": "reply 1"}]
    msgs_t2, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="turn 2 follow up",
        history=hist_t2,
    )
    assert msgs_t2[: len(msgs_t1)] == msgs_t1

def test_budget_skips_already_active_full_entries_without_outline_oscillation(tmp_path: Path) -> None:
    """Round4: surviving entries use no incremental budget or redundant outline."""
    body_a = "Skill A full content " + ("A" * 350)
    body_b = "Skill B full content " + ("B" * 350)
    _write_skill(tmp_path, "skill_a", body=body_a, keywords=["kwa"], priority=10, scan_depth=4)
    _write_skill(tmp_path, "skill_b", body=body_b, keywords=["kwb"], priority=20, scan_depth=4)
    node = Node(id="bnode", name="bnode", type="ai", prompt="SYS")
    # Budget fits one skill (~370 chars) at a time, not both (~740 chars)
    rt_cfg = {"skills": {"max_budget_chars": 500}}

    # Turn 1: trigger skill_a -> injected in full
    msgs_t1, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg=rt_cfg,
        node=node,
        instruction="use kwa",
        history=[],
    )
    dyn_t1 = [m for m in msgs_t1 if is_dynamic_context(m)]
    assert len(dyn_t1) == 1
    assert body_a in dyn_t1[0]["content"]

    # Turn 2: trigger skill_b while skill_a is still in scan_depth.
    # The 500-char budget applies to new bodies only, so skill_b fits in full.
    hist_t2 = [m for m in msgs_t1 if m["role"] != "system"] + [
        {"role": "assistant", "content": "done a"},
    ]
    msgs_t2, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg=rt_cfg,
        node=node,
        instruction="now use kwb",
        history=hist_t2,
    )
    dyn_t2 = [m for m in msgs_t2 if is_dynamic_context(m)]
    assert len(dyn_t2) == 2
    assert body_b in dyn_t2[-1]["content"]
    assert "[Large skill" not in dyn_t2[-1]["content"]
    assert "skill_b" in dyn_t2[-1]["content"]
    assert "skill_a" not in dyn_t2[-1]["content"]
    assert "skill:skill_a" not in dyn_t2[-1]["_meta"]["injected_hashes"]

    # Turn 3: same state -> 0 new dynamic messages (no oscillation!)
    hist_t3 = [m for m in msgs_t2 if m["role"] != "system"] + [
        {"role": "assistant", "content": "done b"},
    ]
    msgs_t3, _, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg=rt_cfg,
        node=node,
        instruction="continue kwb",
        history=hist_t3,
    )
    assert len([m for m in msgs_t3 if is_dynamic_context(m)]) == 2


def test_dynamic_write_failure_stops_before_reply(tmp_path):
    class BrokenStore:
        def append(self, *args):
            raise OSError("disk unavailable")
    ls = SimpleNamespace(rctx=SimpleNamespace(conversation_store=BrokenStore(),
        session_id="s", child_session_id="", task_id="t", response_info={}),
        node=SimpleNamespace(id="n"))
    message = {"role": "user", "content": "knowledge", "_dynamic": True,
               "message_type": MessageType.DYNAMIC_CONTEXT}
    with pytest.raises(OSError):
        ai_step._shadow_write(ls, message, MessageType.DYNAMIC_CONTEXT)
    assert "id" not in message


def test_dynamic_snapshot_and_extractors(tmp_path, monkeypatch):
    from engine.inference import loop_state
    saved = {}
    def save(root, sid, snapshot, **kwargs):
        saved.update(snapshot)
        return "snapshot"
    monkeypatch.setattr(loop_state, "save_context_snapshot", save)
    dynamic = {"id": "d", "role": "user", "content": "PRIVATE KNOWLEDGE",
               "message_type": MessageType.DYNAMIC_CONTEXT,
               "_meta": {"injected_hashes": {"skill:a": "hash"}}}
    pending = {**dynamic, "id": "", "content": "unpersisted"}
    user = {"role": "user", "content": "actual question"}
    feedback = {"role": "user", "content": "retry feedback", "_retry_hint": True}
    loop_state._persist_node_context(tmp_path, "s", "t", "n",
        [user, dynamic, pending, feedback], step_count=1)
    assert saved["messages"] == [user, dynamic, feedback]
    assert "PRIVATE KNOWLEDGE" not in _format_transcript_for_extract([user, dynamic])
    assert "PRIVATE KNOWLEDGE" not in _format_messages_for_summary([user, dynamic])
    assert "actual question" in _format_transcript_for_extract([user, dynamic])


def test_stored_instruction_metadata_is_not_replaced(tmp_path):
    user = {"id": "u", "role": "user", "content": "same", "_meta": {"source_task_id": "t"}}
    messages, _ = assemble_messages_with_injections(workspace_root=tmp_path,
        system_prompt=[{"role": "system", "content": "static"}], history=[user],
        instruction="same", inbound_message=user)
    assert messages[-1] is user
