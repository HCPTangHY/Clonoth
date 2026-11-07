"""Dynamic rows stay in storage/compaction, not user history or turn summaries."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.conversation_store import ConversationStore, Message, MessageType, is_dynamic_context
from supervisor.api import _conversation_message_to_compact_dict
from supervisor.session import SessionMixin
from supervisor.task_router import _format_task_messages_for_turn_summary


def row(mid, content, *, role="user", task="first", dynamic=False):
    return Message(
        id=mid, role=role, content=content, source_task_id=task,
        created_at=mid, message_type=MessageType.DYNAMIC_CONTEXT if dynamic else "",
        meta={"injected_hashes": {"skill:example": "digest"}} if dynamic else {},
    )


@pytest.fixture
def history(tmp_path):
    store = ConversationStore(tmp_path / "data" / "conversations")
    state = SimpleNamespace(
        workspace_root=tmp_path, _lock=RLock(),
        _entry_branch_ids_for_parent_locked=lambda sid: ["branch"] if sid == "parent" else [],
    )
    parent = [row("01", "question"), row("02", "PRIVATE_DYNAMIC", dynamic=True),
              row("03", "answer", role="assistant")]
    branch = parent + [row("04", "follow up", task="second"),
                       row("05", "BRANCH_DYNAMIC", task="second", dynamic=True),
                       row("06", "final answer", role="assistant", task="second")]
    # A legacy dynamic row must also remain hidden, irrespective of its role.
    branch.append(Message(id="07", role="system", content="LEGACY_DYNAMIC", meta={"_dynamic": True}))
    store.append_batch("parent", parent)
    store.append_batch("branch", branch)
    return state, store


def test_simple_history_filters_parent_and_branch_before_limit(history):
    state, _ = history
    assert SessionMixin.session_messages(state, session_id="parent", limit=0) == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": "final answer"},
    ]
    assert SessionMixin.session_messages(state, session_id="parent", limit=1) == [
        {"role": "assistant", "content": "final answer"},
    ]


def test_structured_history_keeps_task_filter_and_normal_metadata(history):
    state, store = history
    store.append("branch", Message(
        id="08", role="assistant", content="tool call", source_task_id="second", created_at="08",
        tool_calls=[{"id": "call", "name": "read_file", "arguments": {}}],
        response_info={"provider": "example"},
    ))
    store.append("branch", Message(
        id="09", role="tool", content="result", source_task_id="second", created_at="09",
        tool_call_id="call", name="read_file",
    ))
    result = SessionMixin.session_history_structured(state, session_id="parent", limit=0, task_id="second")
    assert [m["id"] for m in result] == ["04", "06", "08", "09"]
    assert result[2]["response_info"] == {"provider": "example"}
    assert result[2]["tool_calls"][0]["id"] == "call"
    assert result[3]["tool_call_id"] == "call"
    assert [m["id"] for m in SessionMixin.session_history_structured(
        state, session_id="parent", limit=1, task_id="first",
    )] == ["01", "03"]  # Structured service stays unbounded; API applies pagination.


def test_display_does_not_rewrite_storage_or_remove_replay_rows(history):
    state, store = history
    paths = list((state.workspace_root / "data" / "conversations").glob("*.jsonl"))
    before = {p: p.read_bytes() for p in paths}
    SessionMixin.session_messages(state, session_id="parent")
    SessionMixin.session_history_structured(state, session_id="parent")
    assert {p: p.read_bytes() for p in paths} == before
    assert sum(is_dynamic_context(m) for m in store.load("branch")) == 3
    converted = _conversation_message_to_compact_dict(store.load("parent")[1])
    assert is_dynamic_context(converted)
    assert converted["_meta"]["injected_hashes"] == {"skill:example": "digest"}
    assert converted["_meta"]["source_task_id"] == "first"


@pytest.mark.parametrize("dynamic", [
    row("dyn", "HIDDEN", dynamic=True),
    {"role": "user", "content": "HIDDEN", "message_type": "dynamic_context"},
    {"role": "system", "content": "HIDDEN", "_dynamic": True},
    {"role": "user", "content": "HIDDEN", "meta": {"_dynamic": True}},
    {"role": "user", "content": "HIDDEN", "_meta": {"message_type": "dynamic_context"}},
])
def test_turn_summary_filters_before_serialization_without_mutating_input(dynamic):
    visible = [
        {"role": "user", "content": "literal dynamic_context is normal user text"},
        row("answer", "Delivered answer", role="assistant"),
    ]
    messages = [visible[0], dynamic, visible[1]]
    before = deepcopy(messages)
    assert _format_task_messages_for_turn_summary(messages) == _format_task_messages_for_turn_summary(visible)
    assert "HIDDEN" not in _format_task_messages_for_turn_summary(messages)
    assert "literal dynamic_context" in _format_task_messages_for_turn_summary(messages)
    assert messages == before
    assert _format_task_messages_for_turn_summary([dynamic]) == _format_task_messages_for_turn_summary([])
