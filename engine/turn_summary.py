"""P3 Turn Summary — generate a brief summary after task completion.

Inline LLM call (not dispatched via supervisor) using a lightweight model.
The summary is stored in TaskRecord.summary for later consumption by
Dream, Extractor, and Compactor.

Created: 2026-04-25
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from engine.conversation_store import ConversationStore, Message

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = """Summarize this task execution concisely in 200-400 tokens.
Include:
- What the user requested
- What tools were used and key outcomes
- What was accomplished or failed
- Any important decisions or findings

Output only the summary text. No tags, no headers, no formatting."""


def _format_task_messages(messages: list[Message]) -> str:
    """Format task messages into plain text for the summary LLM."""
    parts: list[str] = []
    for m in messages:
        content = m.content or ""
        if len(content) > 5000:
            content = content[:5000] + "\n...[truncated]"
        parts.append(f"[{m.role}]\n{content}")
    return "\n\n---\n\n".join(parts)


async def generate_turn_summary(
    *,
    conv_store: ConversationStore,
    session_id: str,
    task_id: str,
    llm_http: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    model: str,
    timeout_sec: float = 30.0,
) -> str:
    """Generate a brief summary for a completed task's message chain.

    Returns summary string, or empty string on failure (non-critical).
    """
    try:
        # Load task messages from ConversationStore
        all_messages = conv_store.load(session_id)
        task_messages = [m for m in all_messages if m.source_task_id == task_id]
        if not task_messages:
            return ""

        formatted = _format_task_messages(task_messages)
        if not formatted.strip():
            return ""

        # Truncate if too long (keep under ~30K chars for flash model)
        if len(formatted) > 30000:
            formatted = formatted[:30000] + "\n...[truncated]"

        # LLM call
        url = (base_url.rstrip("/") if base_url else "https://api.openai.com/v1") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": formatted},
            ],
            "max_tokens": 600,
            "temperature": 0.3,
        }

        resp = await llm_http.post(url, json=payload, headers=headers, timeout=timeout_sec)
        if resp.status_code != 200:
            log.warning("Turn summary LLM returned %d", resp.status_code)
            return ""

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return ""

        summary = (choices[0].get("message", {}).get("content") or "").strip()
        if len(summary) < 50:
            log.warning("Turn summary too short (%d chars), discarding", len(summary))
            return ""

        log.info("Turn summary generated for task %s: %d chars", task_id[:12], len(summary))
        return summary

    except Exception as e:
        log.warning("Turn summary generation failed for task %s: %s", task_id[:12], e)
        return ""
