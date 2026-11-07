"""Offline regression tests for the engine read/write memory contract.

Why: the tool-call log used to carry a `legacy_name`, and only the memory-extract gate
ever read it, so the log, the gate and the real knowledge handler could disagree about
whether a finished turn wrote memory. How: drive the real ai_step logging path and the
real memory_extract entry-finish gate with local fakes. Purpose: pin canonical
(name, action) logging, the knowledge action contract and the extraction mutual
exclusion without any retired names.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B tests/test_engine_rw_memory.py -v
No network, no supervisor, no provider call.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.builtin import memory_extract as mem_extract
from engine.hooks.types import HookResult
from engine.inference import ai_step, pseudo_tools
from engine.inference.pseudo_tools import resolve_effective_action
from engine.node import Node, ToolAccess
from providers.base import ProviderResponse, ToolCall


# ---------------------------------------------------------------------------
#  Fakes
# ---------------------------------------------------------------------------

def temporary_workspace():
    directory = ROOT / 'data/temp'
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='rw-memory-', dir=directory)


class FakeRegistry:
    """Registry stand-in: no specs (every call runs serially), records executions."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def get_spec(self, name):
        return None

    def list_specs(self):
        return []

    async def execute(self, *, name, arguments, ctx, _authorized=False):
        self.started.append(name)
        return {"ok": True, "data": {"result": f"{name} ran"}}


class FakeFormatter:
    """Protocol-neutral formatter: keeps only what the loop touches."""

    def format_tool_result(self, call, content):
        return {
            "role": "tool",
            "tool_call_id": str(getattr(call, "id", "") or ""),
            "name": str(getattr(call, "name", "") or ""),
            "content": content,
        }

    def build_assistant_message(self, resp, text, tool_calls):
        return {
            "role": "assistant",
            "content": text or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": dict(tc.arguments or {})}
                for tc in (tool_calls or [])
            ],
        }


def make_ls(tmp_path, *, registry, node=None, allowed_real=()) -> SimpleNamespace:
    rctx = SimpleNamespace(
        workspace_root=Path(tmp_path),
        workspace=None,
        workspace_name="",
        task_id="task",
        session_id="session",
        child_session_id="",
        parent_session_id="",
        supervisor_url="http://supervisor",
        http=object(),
        worker_id="worker",
        session_generation=1,
        task_context={},
        emit_event=AsyncMock(),
        check_cancelled=AsyncMock(return_value=False),
        check_preempted=AsyncMock(return_value={}),
        consume_preempt=AsyncMock(),
        tool_call_log=[],
        total_usage={},
        current_llm_request_id="request",
        first_shadow_message_id="",
        last_shadow_message_id="",
        conversation_store=None,
        response_info={},
        completed_steps=0,
    )
    return SimpleNamespace(
        rctx=rctx,
        node=node or Node(id="audit", type="ai", tool_access=ToolAccess(mode="all")),
        provider=SimpleNamespace(name="fake", model="fake-model"),
        response_provider=None,
        request_history=None,
        registry=registry,
        run_id="run",
        context_ref="",
        runtime_cfg={},
        streaming=False,
        messages=[],
        system_prompt=[],
        is_block_mode=False,
        openai_tools=[],
        history=[],
        collected_attachments=[],
        tool_produced_attachments=[],
        formatter=FakeFormatter(),
        allowed_real_tools=set(allowed_real),
        allowed_pseudo_tools=set(),
        last_shadow_message_id="",
        plugin_data={},
        last_usage={},
        last_prompt_tokens=None,
        use_stream=False,
        compacted=False,
        preempt_after_step=False,
        preempt_inject_info=None,
    )


def tool_call(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=dict(arguments or {}))


def response(*calls: ToolCall) -> ProviderResponse:
    return ProviderResponse(ok=True, text="", tool_calls=list(calls), usage={})


def no_hook(*args, **kwargs):
    return HookResult()


async def produce_log(tmp, *, calls, allowed_real, node=None):
    """Run the real ai_step tool-call path and return (loop_state, registry)."""
    registry = FakeRegistry()
    ls = make_ls(tmp, registry=registry, allowed_real=allowed_real, node=node)
    with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
        await ai_step._handle_tool_calls(ls, response(*calls), 0)
    return ls, registry


def runner_tool_actions(log) -> list[dict]:
    """Mirror engine/runner.py's _tool_actions projection from rctx.tool_call_log."""
    return [
        {"name": entry["name"], "action": entry["action"]}
        for entry in log
        if entry.get("name") and entry.get("action")
    ]


# ---------------------------------------------------------------------------
#  Memory-extract entry gate harness
# ---------------------------------------------------------------------------

RUNTIME = {
    "routing": {"entry_node_id": "entry"},
    "memory": {"auto_extract": {
        "enabled": True,
        "min_increment": 3,
        "min_messages": 1,
        "min_fallback_tasks": 1,
        "idle_fallback_delay_sec": 60,
    }},
}


def gate_suppressed(result) -> bool:
    """Run the real entry-finish gate; return True when extraction was skipped."""
    handler = mem_extract.MemoryExtractHandler()
    task = SimpleNamespace(
        task_id="t1",
        node_id="entry",
        kind="node",
        session_id="sess-1",
        session_generation=1,
        caller_task_id=None,
        input={},
        result=result,
    )
    with temporary_workspace() as tmp:
        ctx = {
            "session_id": "sess-1",
            "workspace_root": Path(tmp),
            "task": task,
            "create_task": lambda **kwargs: None,
        }
        with patch.object(mem_extract, "load_runtime_config", lambda _root: RUNTIME), \
                patch.object(handler, "_prepare_memory_extract_pending_locked", lambda **kwargs: None):
            handler.on_task_complete(ctx)
    # The gate counts the turn only after it decided not to suppress it.
    return "sess-1" not in handler._memory_extract_task_counts


def finished(result_extra: dict | None = None) -> dict:
    result = {"action": "finish"}
    if result_extra is not None:
        result.update(result_extra)
    return result


# ---------------------------------------------------------------------------
#  1. What ai_step records in tool_call_log
# ---------------------------------------------------------------------------

class ToolCallLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_name_and_action_are_recorded(self):
        with temporary_workspace() as tmp:
            ls, _registry = await produce_log(
                tmp, allowed_real={"manage_memory"},
                calls=[tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            )
        self.assertEqual(len(ls.rctx.tool_call_log), 1)
        entry = ls.rctx.tool_call_log[0]
        self.assertEqual((entry["name"], entry["action"]), ("manage_memory", "upsert"))
        self.assertIn("args_summary", entry)
        self.assertNotIn("legacy_name", entry)

    async def test_action_less_tools_record_no_action(self):
        with temporary_workspace() as tmp:
            ls, _registry = await produce_log(
                tmp, allowed_real={"read_file"}, calls=[tool_call("c1", "read_file", {"path": "a.txt"})],
            )
        self.assertEqual(ls.rctx.tool_call_log[0]["name"], "read_file")
        self.assertIsNone(ls.rctx.tool_call_log[0]["action"])
        # runner drops action-less entries, so the gate never sees an invented action.
        self.assertEqual(runner_tool_actions(ls.rctx.tool_call_log), [])

    async def test_content_without_action_is_recorded_as_read(self):
        with temporary_workspace() as tmp:
            ls, _registry = await produce_log(
                tmp, allowed_real={"manage_memory"},
                calls=[tool_call("c1", "manage_memory", {"content": "x"})],
            )
        self.assertEqual(ls.rctx.tool_call_log[0]["action"], "list")

    async def test_retired_words_are_not_recorded_as_writes(self):
        for word in ("save", "create", "update", "remove"):
            with self.subTest(word=word), temporary_workspace() as tmp:
                ls, _registry = await produce_log(
                    tmp, allowed_real={"manage_memory"},
                    calls=[tool_call("c1", "manage_memory", {"action": word, "id": "m", "content": "x"})],
                )
                action = ls.rctx.tool_call_log[0]["action"]
                self.assertEqual(action, word)
                self.assertNotIn(action, ("upsert", "delete"))

    async def test_legacy_tool_name_is_recorded_as_its_canonical_pair(self):
        with temporary_workspace() as tmp:
            ls, _registry = await produce_log(
                tmp, allowed_real={"save_memory"},
                calls=[tool_call("c1", "save_memory", {"id": "m", "content": "x"})],
            )
        entry = ls.rctx.tool_call_log[0]
        self.assertEqual((entry["name"], entry["action"]), ("manage_memory", "upsert"))

    async def test_real_tool_is_authorized_without_its_retired_alias(self):
        # The actual tool name is sufficient; its retired alias is not required.
        with temporary_workspace() as tmp:
            ls, registry = await produce_log(
                tmp, allowed_real={"manage_memory"},
                calls=[tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            )
        self.assertEqual(registry.started, ["manage_memory"])
        self.assertEqual(len(ls.rctx.tool_call_log), 1)

    async def test_denied_action_is_not_logged(self):
        node = Node(id="limited", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_memory:upsert"]))
        with temporary_workspace() as tmp:
            ls, registry = await produce_log(
                tmp, node=node, allowed_real={"manage_memory"},
                calls=[tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            )
        self.assertEqual(registry.started, [])
        self.assertEqual(ls.rctx.tool_call_log, [])

    async def test_unlisted_tool_is_rejected_and_not_logged(self):
        with temporary_workspace() as tmp:
            ls, registry = await produce_log(
                tmp, allowed_real=set(),
                calls=[tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            )
        self.assertEqual(registry.started, [])
        self.assertEqual(ls.rctx.tool_call_log, [])


# ---------------------------------------------------------------------------
#  2. Logged action matches what the knowledge handler really does
# ---------------------------------------------------------------------------

class KnowledgeActionContractTests(unittest.TestCase):
    def test_resolver_matches_knowledge_handler(self):
        from engine.builtin import knowledge_inject

        cases = (
            {},
            {"content": "x"},
            {"action": "list"},
            {"action": "upsert", "id": "m", "content": "x"},
            {"action": "delete", "id": "m"},
            {"action": "save", "id": "m", "content": "x"},
            {"action": "remove", "id": "m"},
            {"action": " UPSERT "},
        )
        for tool in ("manage_memory", "manage_skill"):
            for args in cases:
                with self.subTest(tool=tool, args=args):
                    self.assertEqual(
                        resolve_effective_action(tool, args)[1],
                        knowledge_inject._resolve_action(args),
                    )

    def test_retired_alias_helper_is_gone(self):
        self.assertFalse(hasattr(pseudo_tools, "resolve_legacy_action_alias"))


# ---------------------------------------------------------------------------
#  3. Entry-finish gate consumes the logged actions
# ---------------------------------------------------------------------------

class MemoryExtractGateTests(unittest.IsolatedAsyncioTestCase):
    async def _gate_for(self, calls, *, allowed_real):
        with temporary_workspace() as tmp:
            ls, _registry = await produce_log(tmp, allowed_real=allowed_real, calls=calls)
        return gate_suppressed(finished({"_tool_actions": runner_tool_actions(ls.rctx.tool_call_log)}))

    async def test_upsert_turn_suppresses_extraction(self):
        self.assertTrue(await self._gate_for(
            [tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            allowed_real={"manage_memory"},
        ))

    async def test_delete_turn_suppresses_extraction(self):
        self.assertTrue(await self._gate_for(
            [tool_call("c1", "manage_memory", {"action": "delete", "id": "m"})],
            allowed_real={"manage_memory"},
        ))

    async def test_legacy_alias_write_turn_suppresses_extraction(self):
        self.assertTrue(await self._gate_for(
            [tool_call("c1", "save_memory", {"id": "m", "content": "x"})],
            allowed_real={"save_memory"},
        ))

    async def test_list_turn_does_not_suppress_extraction(self):
        self.assertFalse(await self._gate_for(
            [tool_call("c1", "manage_memory", {"action": "list"})],
            allowed_real={"manage_memory"},
        ))

    async def test_content_without_action_does_not_suppress_extraction(self):
        self.assertFalse(await self._gate_for(
            [tool_call("c1", "manage_memory", {"content": "x"})],
            allowed_real={"manage_memory"},
        ))

    async def test_retired_action_word_does_not_suppress_extraction(self):
        for word in ("save", "create", "update", "remove", "frobnicate"):
            with self.subTest(word=word):
                self.assertFalse(await self._gate_for(
                    [tool_call("c1", "manage_memory", {"action": word, "id": "m", "content": "x"})],
                    allowed_real={"manage_memory"},
                ))

    async def test_unrelated_tool_does_not_suppress_extraction(self):
        self.assertFalse(await self._gate_for(
            [tool_call("c1", "grep", {"query": "x"})],
            allowed_real={"grep"},
        ))

    async def test_turn_without_tools_does_not_suppress_extraction(self):
        self.assertFalse(await self._gate_for([], allowed_real={"manage_memory"}))
        self.assertFalse(gate_suppressed(finished()))

    async def test_unauthorized_write_does_not_suppress_extraction(self):
        self.assertFalse(await self._gate_for(
            [tool_call("c1", "manage_memory", {"action": "upsert", "id": "m", "content": "x"})],
            allowed_real=set(),
        ))

    async def test_retired_tool_names_are_ignored(self):
        self.assertFalse(gate_suppressed(finished({"_tool_names": ["save_memory", "delete_memory"]})))

    async def test_malformed_actions_do_not_suppress_extraction(self):
        malformed = (
            "upsert",
            {"name": "manage_memory", "action": "upsert"},
            [None, "manage_memory", 7],
            [{"name": "manage_memory"}],
            [{"action": "upsert"}],
            [{"name": "manage_memory", "action": {}}],
            [{"name": "manage_memory", "action": "LIST"}],
            [{"name": "manage_skill", "action": "upsert"}],
            [],
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertFalse(gate_suppressed(finished({"_tool_actions": payload})))

    def test_mutation_helper_ignores_non_dict_results(self):
        for result in (None, "finish", [], 7):
            self.assertFalse(mem_extract._memory_mutation_happened(result))


if __name__ == "__main__":
    unittest.main()
