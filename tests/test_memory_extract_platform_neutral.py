"""Platform-neutral memory transcripts; run with pytest on this file."""
from pathlib import Path

import yaml

from engine.builtin.dream import DreamHandler
from engine.builtin.memory_extract import _format_transcript_for_extract
from engine.conversation_store import Message, MessageType


WRAPPED_TEXT = (
    "【群聊上下文记录】\nuser-supplied text\n【当前消息引用】\nquoted line\n"
    "【当前用户指令】\nreal instruction"
)


def test_transcript_preserves_marker_like_user_text():
    messages = [
        Message(id="u", role="user", content=WRAPPED_TEXT),
        Message(id="a", role="assistant", content="answer"),
    ]
    assert _format_transcript_for_extract(messages) == (
        f"User: {WRAPPED_TEXT}\n\nAssistant: answer"
    )
    assert messages[0].content == WRAPPED_TEXT


def test_transcript_preserves_multimodal_text_without_attachment_payloads():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": WRAPPED_TEXT},
        {"type": "image_url", "image_url": {"url": "private-image"}},
    ]}]
    assert _format_transcript_for_extract(messages) == f"User: {WRAPPED_TEXT}"


def test_structured_filters_remain_platform_independent():
    messages = [
        {"role": "system", "content": "system-only"},
        {"role": "user", "content": "dynamic-only", "message_type": MessageType.DYNAMIC_CONTEXT},
        {"role": "tool", "content": "tool-only"},
        {"role": "user", "content": "tool-result-only", "message_type": "tool_result"},
        {"role": "user", "content": "retry-only", "message_type": "retry_hint"},
        {"role": "user", "content": "rejection-only", "message_type": "output_rejected"},
        {"role": "assistant", "content": "draft-only", "meta": {"rejected_draft": True}},
        {"role": "user", "content": "feedback-only", "_meta": {"is_rejection": True}},
        {"role": "user", "content": "ordinary request"},
        {"role": "assistant", "content": "ordinary answer"},
    ]
    assert _format_transcript_for_extract(messages) == (
        "User: ordinary request\n\nAssistant: ordinary answer"
    )


def test_existing_text_limits_remain_unchanged():
    text = "x" * 2100
    assert _format_transcript_for_extract([{"role": "user", "content": text}]) == (
        "User: " + "x" * 2000 + "...<truncated>"
    )
    assert _format_transcript_for_extract([
        {"role": "user", "content": "older text"},
        {"role": "user", "content": "new"},
    ], max_chars=9) == "User: new"


def test_dream_fallback_uses_same_platform_neutral_transcript(tmp_path):
    result = DreamHandler()._session_transcript(
        workspace_root=tmp_path,
        ctx={"session_messages": lambda session_id, limit: [
            {"role": "user", "content": WRAPPED_TEXT},
        ]},
        session_id="missing-local-session",
    )
    assert result == f"User: {WRAPPED_TEXT}"


def test_extractor_prompt_has_no_platform_or_community_assumption():
    path = Path(__file__).resolve().parents[1] / "engine/builtin/nodes/system.memory_extractor.yaml"
    node = yaml.safe_load(path.read_text(encoding="utf-8"))
    prompt = node["prompt"]
    for term in ("Discord", "research community", "community-owned", "群友", "身份组", '"4a"', '"fb"'):
        assert term not in prompt
    assert node["tool_access"] == {"mode": "allowlist", "allow": ["manage_memory"]}
    assert node["memories"] == {"mode": "constant"}
    assert '"scan_depth": always set to 0' in prompt
    assert "Max 3 memories per extraction" in prompt
