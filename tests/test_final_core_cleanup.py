"""Offline regression checks for final core metadata and prompt cleanup."""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from engine import data_cleanup
from engine.builtin import preempt
from engine.compact import split_task_segments
from engine.conversation_store import ConversationStore, Message


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("with_tools", [False, True])
def test_zero_delta_preempt_matches_stored_segments(tmp_path, with_tools):
    store = ConversationStore(tmp_path / "conversations")
    messages = [
        {"role": "user", "content": "initial", "_meta": {"source_task_id": "task"}},
        {"role": "assistant", "content": "answer", "_meta": {"source_task_id": "task"}},
    ]
    for i, row in enumerate(messages):
        store.append("session", Message(id=str(i), role=row["role"], content=row["content"], source_task_id="task"))
    loop = SimpleNamespace(
        messages=messages,
        preempt_inject_info={"message": "follow-up", "attachments": []},
        rctx=SimpleNamespace(
            workspace_root=tmp_path, session_id="session", task_id="task",
            child_session_id="", conversation_store=store, task_context={},
            consume_preempt=AsyncMock(), emit_event=AsyncMock(),
        ),
        node=SimpleNamespace(id="node"), runtime_cfg={}, history=[],
        is_block_mode=False, system_prompt="",
    )
    with patch.object(preempt, "rebuild_dynamic_context"):
        asyncio.run(preempt._inject_preempt_message(SimpleNamespace(step=1), loop))
    assert messages[-1]["_meta"]["source_task_id"] == "task"
    assert store.load("session")[-1].meta == messages[-1]["_meta"]
    # Real live assistant/tool rows can lack the source_task_id stored by shadow write.
    tail = [{"role": "assistant", "content": "continued"}]
    if with_tools:
        tail.extend([{"role": "tool", "content": "result"}, {"role": "assistant", "content": "done"}])
    for i, row in enumerate(tail):
        messages.append(row)
        store.append("session", Message(id=f"tail-{i}", role=row["role"], content=row["content"], source_task_id="task"))
    live = split_task_segments(messages)[1]
    stored = split_task_segments(store.load("session"))[1]
    assert [[row["content"] for row in seg] for seg in live] == [[row.content for row in seg] for seg in stored]
    assert len(live) == 2


def test_task_inference_preserves_explicit_other_task_and_unknown_user_boundary():
    rows = [
        {"role": "user", "content": "first", "_meta": {"source_task_id": "a"}},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "unknown task"},
        {"role": "assistant", "content": "unknown answer"},
        {"role": "assistant", "content": "other task", "_meta": {"source_task_id": "b"}},
    ]
    assert [[r["content"] for r in seg] for seg in split_task_segments(rows)[1]] == [
        ["first", "answer"], ["unknown task", "unknown answer"], ["other task"],
    ]


@pytest.mark.parametrize("tty", [False, True])
def test_cleanup_stdout_only_for_tty_and_file_always_present(tmp_path, tty):
    class Output(io.StringIO):
        def isatty(self):
            return tty

    output = Output()
    root = logging.getLogger()
    log_file = tmp_path / "logs" / "cleanup.log"
    with patch.object(data_cleanup, "LOG_FILE", log_file), patch.object(data_cleanup.sys, "stdout", output), \
            patch.object(root, "handlers", []), patch.object(root, "level", logging.WARNING):
        try:
            data_cleanup._log_init()
            handlers = tuple(root.handlers)
            data_cleanup._log_init()
            assert tuple(root.handlers) == handlers, "repeated initialization must not attach duplicate handlers"
            assert len(handlers) == (2 if tty else 1)
            assert isinstance(handlers[0], logging.FileHandler)
            logging.info("cleanup-test-record")
            for handler in handlers:
                handler.flush()
            assert "cleanup-test-record" in log_file.read_text()
            assert ("cleanup-test-record" in output.getvalue()) is tty
        finally:
            for handler in root.handlers:
                handler.close()


def test_system_prompts_are_platform_neutral_without_policy_or_limit_changes():
    paths = [ROOT / "engine/builtin/nodes" / f"system.{name}.yaml" for name in ("dream", "compactor", "turn_summarizer")]
    for path in paths:
        node = yaml.safe_load(path.read_text())
        prompt = node["prompt"]
        assert not any(term in prompt for term in ("群友", "本群", "身份组", "Discord"))
        assert "拒绝" in prompt
    dream = yaml.safe_load(paths[0].read_text())
    assert dream["memories"]["mode"] == "constant"
    assert "manage_skill:delete" in dream["tool_access"]["deny"]
    for policy in ("30 天", "7 天", "单次最多提升 3 条", "单轮最多执行 35 条", "scan_depth", "constant: true"):
        assert policy in dream["prompt"]
    source = (ROOT / "engine/inference/ai_step.py").read_text()
    worker_prompt = source.split("_worker_task_prompt = (", 1)[1].split("if attachments:", 1)[0]
    assert "Discord" not in worker_prompt and "[SPLIT]" not in worker_prompt
    assert "Do NOT roleplay as the chat persona" in worker_prompt
    assert "switch_node" in worker_prompt and "task-relevant results" in worker_prompt
