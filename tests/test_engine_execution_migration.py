"""Offline regression tests for the engine execution-chain migration.

Covers only local fixtures and fakes: no network, no supervisor, no provider call.
Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest tests.test_engine_execution_migration -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.hooks.types import HookResult
from engine.inference import ai_step, llm_call
from engine.inference.pseudo_tools import (
    _dispatch_target_from_tool_name,
    _dispatch_tool_name,
    _filter_tool_specs,
    allowed_pseudo_names,
    build_node_tool_specs,
    is_tool_action_allowed,
    resolve_effective_action,
)
from engine.node import Node, ToolAccess
from engine.protocol import ACTION_FINISH, TaskAction
from providers.base import ProviderResponse, ToolCall
from providers.tool_format import create_tool_formatter


# ---------------------------------------------------------------------------
#  Fakes
# ---------------------------------------------------------------------------

class RecordingStore:
    """Minimal ConversationStore stand-in that keeps append order."""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    def append(self, session_id: str, message: SimpleNamespace) -> None:
        self.rows.append(message)

    def order(self) -> list[str]:
        """Message order as role:tool_call_id/name markers."""
        out = []
        for row in self.rows:
            if row.role == "tool":
                out.append(f"tool:{row.tool_call_id}:{row.name}")
            else:
                out.append(f"{row.role}:{','.join(str(c.get('id')) for c in (row.tool_calls or []))}")
        return out


class FakeFormatter:
    """Protocol-neutral formatter: keeps the loop assertions readable."""

    def __init__(self, mode: str = "native") -> None:
        self.mode = mode

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

    def parse_tool_calls(self, resp):
        return []

    def get_plain_text(self, resp):
        return resp.text or ""

    def build_retry_hint(self):
        return "retry"


class FakeRegistry:
    """Registry whose per-tool handlers record start order."""

    def __init__(self, specs: dict[str, dict], handlers: dict | None = None) -> None:
        self._specs = specs
        self._handlers = handlers or {}
        self.started: list[str] = []
        self.arguments: dict[str, dict] = {}

    def get_spec(self, name):
        return self._specs.get(name)

    def list_specs(self):
        return list(self._specs.values())

    async def execute(self, *, name, arguments, ctx, _authorized=False):
        self.started.append(name)
        self.arguments[name] = dict(arguments or {})
        handler = self._handlers.get(name)
        if handler is not None:
            result = handler(arguments, ctx)
            if hasattr(result, "__await__"):
                return await result
            return result
        return {"ok": True, "data": {"result": f"{name} ran"}}

def spec(name: str, **extra) -> dict:
    base = {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    base.update(extra)
    return base


def make_ls(
    tmp_path,
    *,
    registry=None,
    node=None,
    messages=None,
    allowed_real=(),
    allowed_pseudo=None,
    store=None,
) -> SimpleNamespace:
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
        conversation_store=store,
        response_info={},
        completed_steps=0,
    )
    return SimpleNamespace(
        rctx=rctx,
        node=node or Node(id="audit", type="ai", tool_access=ToolAccess(mode="all")),
        provider=SimpleNamespace(name="fake", model="fake-model"),
        response_provider=None,
        request_history=None,
        registry=registry if registry is not None else FakeRegistry({}),
        run_id="run",
        context_ref="",
        runtime_cfg={},
        streaming=False,
        messages=list(messages or []),
        system_prompt=[],
        is_block_mode=False,
        openai_tools=[],
        history=[],
        collected_attachments=[],
        tool_produced_attachments=[],
        formatter=FakeFormatter(),
        allowed_real_tools=set(allowed_real),
        allowed_pseudo_tools=allowed_pseudo,
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


def response(*calls: ToolCall, text: str = "") -> ProviderResponse:
    return ProviderResponse(ok=True, text=text, tool_calls=list(calls), usage={})


def no_hook(*args, **kwargs):
    return HookResult()


# ---------------------------------------------------------------------------
#  1. Declaration order: pseudo and real calls are interleaved as declared
# ---------------------------------------------------------------------------

class DeclarationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_and_pseudo_calls_run_in_declared_order(self):
        order: list[str] = []

        async def fake_pseudo(ls, call, step):
            order.append(f"pseudo:{call.name}")
            return None

        registry = FakeRegistry({}, {
            "alpha": lambda args, ctx: _record(order, "real:alpha", "alpha out"),
            "beta": lambda args, ctx: _record(order, "real:beta", "beta out"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(
                tmp, registry=registry, allowed_real={"alpha", "beta"},
                allowed_pseudo={"intermediate_reply"},
            )
            calls = response(
                tool_call("c1", "alpha"),
                tool_call("c2", "intermediate_reply", {"text": "working"}),
                tool_call("c3", "beta"),
            )
            with patch.object(ai_step, "_handle_pseudo_tool", side_effect=fake_pseudo), \
                    patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                self.assertIsNone(await ai_step._handle_tool_calls(ls, calls, 0))
            self.assertEqual(order, ["real:alpha", "pseudo:intermediate_reply", "real:beta"])

    async def test_finish_stays_last_even_when_declared_first(self):
        order: list[str] = []

        async def fake_pseudo(ls, call, step):
            order.append(f"pseudo:{call.name}")
            return TaskAction(action=ACTION_FINISH, node_id=ls.node.id, result={"text": "done"})

        registry = FakeRegistry({}, {
            "alpha": lambda args, ctx: _record(order, "real:alpha", "alpha out"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(
                tmp, registry=registry, allowed_real={"alpha"}, allowed_pseudo={"finish"},
            )
            calls = response(tool_call("c1", "finish", {"text": "done"}), tool_call("c2", "alpha"))
            with patch.object(ai_step, "_handle_pseudo_tool", side_effect=fake_pseudo), \
                    patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                action = await ai_step._handle_tool_calls(ls, calls, 0)
            self.assertEqual(order, ["real:alpha", "pseudo:finish"])
            self.assertIsNotNone(action)
            self.assertEqual(action.action, ACTION_FINISH)


def _record(order: list[str], marker: str, text: str):
    order.append(marker)
    return {"ok": True, "data": {"result": text}}


# ---------------------------------------------------------------------------
#  2. Async launch barrier
# ---------------------------------------------------------------------------

class AsyncBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_async_calls_launch_as_one_batch(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        seen: list[str] = []

        async def slow_async(args, ctx):
            # Only completes once the sibling call has also started: a sequential
            # loop would deadlock here and trip the timeout below.
            seen.append("async-1")
            await asyncio.wait_for(second_started.wait(), timeout=5)
            return {"ok": True, "data": {"result": "one"}}

        async def fast_async(args, ctx):
            seen.append("async-2")
            second_started.set()
            first_started.set()
            return {"ok": True, "data": {"result": "two"}}

        async def sync_tool(args, ctx):
            seen.append("sync")
            return {"ok": True, "data": {"result": "sync output"}}

        registry = FakeRegistry(
            {
                "async_one": spec("async_one", async_mode=True),
                "async_two": spec("async_two", async_mode=True),
                "sync_tool": spec("sync_tool"),
            },
            {"async_one": slow_async, "async_two": fast_async, "sync_tool": sync_tool},
        )
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, allowed_real={"async_one", "async_two", "sync_tool"})
            calls = response(
                tool_call("c1", "async_one"),
                tool_call("c2", "async_two"),
                tool_call("c3", "sync_tool"),
            )
            with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                await ai_step._handle_tool_calls(ls, calls, 0)
            # Both async calls had started before the synchronous one ran.
            self.assertEqual(seen[:2], ["async-1", "async-2"])
            self.assertEqual(seen, ["async-1", "async-2", "sync"])
            self.assertTrue(first_started.is_set())

    async def test_batching_uses_async_mode_not_pure(self):
        cases = (
            (None, False),
            (spec("plain"), False),
            (spec("pure", pure=True), False),
            (spec("sync", pure=True, async_mode=False), False),
            (spec("background", pure=False, async_mode=True), True),
        )
        for tool_spec, expected in cases:
            with self.subTest(spec=tool_spec):
                self.assertIs(ai_step._is_pure_tool_call("tool", {}, tool_spec), expected)

        order: list[str] = []

        async def run(args, ctx):
            order.append(f"start:{ctx.tool_call_id}")
            await asyncio.sleep(0)
            order.append(f"end:{ctx.tool_call_id}")
            return {"ok": True, "data": {"result": "done"}}

        registry = FakeRegistry(
            {"pure": spec("pure", pure=True), "background": spec("background", async_mode=True)},
            {"pure": run, "background": run},
        )
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, allowed_real={"pure", "background"})
            calls = response(
                tool_call("c1", "background"), tool_call("c2", "pure"),
                tool_call("c3", "pure"), tool_call("c4", "background"),
            )
            with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                await ai_step._handle_tool_calls(ls, calls, 0)
        self.assertEqual(order, [item for cid in ("c1", "c2", "c3", "c4") for item in (f"start:{cid}", f"end:{cid}")])

    async def test_shared_step_budget_spans_every_batch(self):
        registry = FakeRegistry({}, {
            "alpha": lambda args, ctx: _record([], "alpha", "0123456789"),
            "beta": lambda args, ctx: _record([], "beta", "0123456789"),
        })

        async def fake_pseudo(ls, call, step):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, allowed_real={"alpha", "beta"}, allowed_pseudo={"intermediate_reply"})
            calls = response(
                tool_call("c1", "alpha"),
                tool_call("c2", "intermediate_reply", {"text": "mid"}),
                tool_call("c3", "beta"),
            )
            with patch.object(ai_step, "_handle_pseudo_tool", side_effect=fake_pseudo), \
                    patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                await ai_step._handle_tool_calls(ls, calls, 0)
            self.assertEqual(ls._step_inline_state["used"], 20)


# ---------------------------------------------------------------------------
#  3. Shadow write buffer: paired, message-ordered, one row per message
# ---------------------------------------------------------------------------

class ShadowBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_flushes_in_final_message_order(self):
        store = RecordingStore()
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, store=store)
            ls._pending_shadow_writes = []
            ls._step_tool_calls = [tool_call("c1", "alpha"), tool_call("c2", "beta")]
            ls._step_tool_msg_start = 1
            ls.messages.append({"role": "assistant", "content": "", "tool_calls": []})
            # Appended out of declaration order, as an interrupted batch can do.
            second = ls.formatter.format_tool_result(tool_call("c2", "beta"), "beta out")
            first = ls.formatter.format_tool_result(tool_call("c1", "alpha"), "alpha out")
            ls.messages.extend([second, first])
            ai_step._shadow_write(ls, second, "tool_result")
            ai_step._shadow_write(ls, first, "tool_result")
            self.assertEqual(store.rows, [])  # buffered, not written yet
            ai_step._flush_step_shadow_writes(ls)
            self.assertEqual([row.tool_call_id for row in store.rows], ["c1", "c2"])
            tool_rows = [m for m in ls.messages if m.get("role") == "tool"]
            self.assertEqual([m["tool_call_id"] for m in tool_rows], ["c1", "c2"])

    async def test_explicit_write_inside_step_is_not_duplicated(self):
        store = RecordingStore()
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, store=store)
            ls._pending_shadow_writes = []
            assistant = {"role": "assistant", "content": "hi", "tool_calls": []}
            ls.messages.append(assistant)
            ai_step._shadow_write(ls, assistant, "assistant")
            ai_step._flush_step_shadow_writes(ls)
            self.assertEqual(len(store.rows), 1)

    async def test_cancelled_batch_keeps_placeholder_results(self):
        registry = FakeRegistry({"alpha": spec("alpha"), "beta": spec("beta")})
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, allowed_real={"alpha", "beta"})
            ls.rctx.check_cancelled.return_value = True
            with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                await ai_step._handle_tool_calls(
                    ls, response(tool_call("c1", "alpha"), tool_call("c2", "beta")), 0,
                )
            self.assertEqual(registry.started, [])
            results = [message for message in ls.messages if message.get("role") == "tool"]
            self.assertEqual([message["tool_call_id"] for message in results], ["c1", "c2"])
            self.assertTrue(all("Skipped" in message["content"] for message in results))

    async def test_shadow_write_preserves_multimodal_content(self):
        store = RecordingStore()
        content = [
            {"type": "text", "text": "result"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, store=store)
            ai_step._shadow_write(ls, {"role": "user", "content": content}, "tool_result_attachment")
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(store.rows[0].content, content)
        self.assertIsInstance(store.rows[0].content, list)

    async def test_interrupted_calls_get_paired_placeholder_results(self):
        store = RecordingStore()
        stop = TaskAction(action=ACTION_FINISH, node_id="audit", result={"text": "stop"})

        async def fake_pseudo(ls, call, step):
            return stop

        registry = FakeRegistry({}, {"alpha": lambda args, ctx: _record([], "alpha", "never")})
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(
                tmp, registry=registry, store=store,
                allowed_real={"alpha"}, allowed_pseudo={"intermediate_reply"},
            )
            calls = response(
                tool_call("c1", "intermediate_reply", {"text": "switch"}),
                tool_call("c2", "alpha"),
            )
            with patch.object(ai_step, "_handle_pseudo_tool", side_effect=fake_pseudo), \
                    patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                action = await ai_step._handle_tool_calls(ls, calls, 0)
            self.assertIs(action, stop)
            self.assertEqual(registry.started, [])
            tool_rows = [m for m in ls.messages if m.get("role") == "tool"]
            self.assertEqual([m["tool_call_id"] for m in tool_rows], ["c2"])
            self.assertIn("Skipped", tool_rows[0]["content"])
            # Every declared call id is paired with a stored tool result row.
            stored_tools = [row for row in store.rows if row.role == "tool"]
            self.assertEqual([row.tool_call_id for row in stored_tools], ["c2"])
            self.assertEqual(
                [row.role for row in store.rows],
                [m.get("role") for m in ls.messages],
            )


# ---------------------------------------------------------------------------
#  4. Action gating: illegal values, real defaults, selector fidelity
# ---------------------------------------------------------------------------

class ActionGateTests(unittest.TestCase):
    def test_canonical_name_and_verified_defaults(self):
        self.assertEqual(resolve_effective_action("manage_tool", {}), ("manage_tool", "reload"))
        self.assertEqual(
            resolve_effective_action("manage_tool", {"name": "demo"}), ("manage_tool", "upsert"),
        )
        self.assertEqual(resolve_effective_action("manage_schedule", {}), ("manage_schedule", "list"))
        # Knowledge tools default to list; only an explicit action requests a write.
        self.assertEqual(resolve_effective_action("manage_memory", {}), ("manage_memory", "list"))
        self.assertEqual(resolve_effective_action("manage_memory", {"content": "x"}), ("manage_memory", "list"))
        self.assertEqual(resolve_effective_action("manage_skill", {"name": "s"}), ("manage_skill", "list"))
        self.assertEqual(resolve_effective_action("manage_skill", {"name": "s", "content": "x"}), ("manage_skill", "list"))
        self.assertEqual(
            resolve_effective_action("manage_mcp_client", {"id": "m"}), ("manage_mcp_client", "list"),
        )
        # No verified default (external tool): the resolver must not invent one.
        self.assertEqual(resolve_effective_action("external_script", {"action": ""}), ("external_script", None))

    def test_explicit_action_is_normalized_like_the_tool(self):
        self.assertEqual(
            resolve_effective_action("manage_schedule", {"action": " UPSERT "}),
            ("manage_schedule", "upsert"),
        )
        # Empty action is absence, matching the tool's own `or ""` handling.
        self.assertEqual(resolve_effective_action("manage_schedule", {"action": "  "}), ("manage_schedule", "list"))

    def test_default_action_gate_is_separate_from_schema_validation(self):
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_schedule:list"]))
        self.assertFalse(is_tool_action_allowed(node, "manage_schedule", {}))
        self.assertTrue(is_tool_action_allowed(node, "manage_schedule", {"action": "upsert"}))
        # This API checks node rules only; it no longer accepts a tool schema.
        self.assertTrue(is_tool_action_allowed(node, "manage_schedule", {"action": "nuke"}))
        only_node = Node(id="limited", type="ai", tool_access=ToolAccess(mode="allowlist", allow=["manage_schedule:upsert"]))
        self.assertFalse(is_tool_action_allowed(only_node, "manage_schedule", {"action": "nuke"}))

    def test_unknown_default_is_not_invented_by_the_action_gate(self):
        # Production applies action restrictions only when an action is known.
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["external_script:upsert"]))
        self.assertTrue(is_tool_action_allowed(node, "external_script", {}))
        self.assertTrue(is_tool_action_allowed(node, "external_script", {"action": "run"}))
        self.assertFalse(is_tool_action_allowed(node, "external_script", {"action": "upsert"}))
        # A tool without action rules is unaffected.
        open_node = Node(id="n2", type="ai", tool_access=ToolAccess(mode="all"))
        self.assertTrue(is_tool_action_allowed(open_node, "external_script", {}))

    def test_grep_uses_real_mode_and_cannot_be_bypassed_by_action(self):
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["grep:replace"]))
        # action='search' next to mode='replace': the tool reads mode.
        self.assertEqual(
            resolve_effective_action("grep", {"action": "search", "mode": "replace"}),
            ("grep", "replace"),
        )
        self.assertFalse(is_tool_action_allowed(
            node, "grep", {"action": "search", "mode": "replace"},
        ))
        self.assertTrue(is_tool_action_allowed(node, "grep", {"query": "x"}))
        # Invalid mode values are not schema-validated by this gate.
        self.assertTrue(is_tool_action_allowed(node, "grep", {"mode": "bogus"}))

    def test_whole_tool_deny_and_allow_only(self):
        deny_node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_tool"]))
        self.assertFalse(is_tool_action_allowed(deny_node, "manage_tool", {"action": "upsert"}))
        only_node = Node(
            id="n2", type="ai",
            tool_access=ToolAccess(mode="allowlist", allow=["manage_tool:upsert"]),
        )
        self.assertTrue(is_tool_action_allowed(only_node, "manage_tool", {"name": "x"}))
        self.assertFalse(is_tool_action_allowed(only_node, "manage_tool", {}))

    def test_alias_rules_do_not_restore_retired_knowledge_action_inference(self):
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_memory:upsert", "delete_schedule"]))
        self.assertEqual(resolve_effective_action("save_memory", {}), ("manage_memory", "upsert"))
        self.assertFalse(is_tool_action_allowed(node, "save_memory", {}))
        self.assertFalse(is_tool_action_allowed(node, "manage_memory", {"action": "upsert"}))
        self.assertEqual(resolve_effective_action("manage_memory", {"content": "new memory"}), ("manage_memory", "list"))
        self.assertTrue(is_tool_action_allowed(node, "manage_memory", {"content": "new memory"}))
        # Invalid action values remain invalid; the real tool rejects them instead
        # of the resolver silently translating them into an authorized write.
        self.assertEqual(resolve_effective_action("manage_memory", {"action": "create"}), ("manage_memory", "create"))
        self.assertFalse(is_tool_action_allowed(
            Node(id="limited", type="ai", tool_access=ToolAccess(mode="allowlist", allow=["manage_memory:upsert"])),
            "manage_memory", {"action": "create"},
        ))
        self.assertFalse(is_tool_action_allowed(node, "manage_schedule", {"action": "remove"}))
        self.assertTrue(is_tool_action_allowed(node, "list_memories", {}))


class ActionExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_action_returns_a_paired_error_without_execution(self):
        node = Node(id="audit", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_schedule:list"]))
        registry = FakeRegistry({"manage_schedule": spec("manage_schedule")})
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, node=node, registry=registry, allowed_real={"manage_schedule", "list_schedules"})
            with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                self.assertIsNone(await ai_step._handle_tool_calls(
                    ls, response(tool_call("denied", "manage_schedule", {"action": "list"})), 0,
                ))
            self.assertEqual(registry.started, [])
            results = [message for message in ls.messages if message.get("role") == "tool"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["tool_call_id"], "denied")
            self.assertIn("Tool 'manage_schedule' does not exist", results[0]["content"])

    async def test_round_block_does_not_duplicate_unauthorized_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp)
            blocked = HookResult(block=True, reason="round_blocked", error_message="round denied")
            with patch.object(ai_step.hook_registry, "afire", return_value=blocked):
                self.assertIsNone(await ai_step._handle_tool_calls(
                    ls, response(tool_call("denied", "unknown_tool")), 0,
                ))
            results = [message for message in ls.messages if message.get("role") == "tool"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["tool_call_id"], "denied")
            self.assertEqual(results[0]["content"], "round denied")

    async def test_fourth_duplicate_round_is_blocked_until_new_user_input(self):
        registry = FakeRegistry({"lookup": spec("lookup")})
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, allowed_real={"lookup"})
            with patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                for step in range(4):
                    self.assertIsNone(await ai_step._handle_tool_calls(
                        ls, response(tool_call(f"call-{step}", "lookup", {"query": "same"})), step,
                    ))
                self.assertEqual(registry.started, ["lookup"] * 3)
                self.assertIn("DUPLICATE_CALL_BLOCKED", ls.messages[-1]["content"])
                self.assertEqual(ls.messages[-1]["_meta"]["tool_rejection_code"], "duplicate_tool_loop_blocked")
                ls.messages.append({"role": "user", "content": "Check again."})
                self.assertIsNone(await ai_step._handle_tool_calls(
                    ls, response(tool_call("call-new", "lookup", {"query": "same"})), 4,
                ))
                self.assertEqual(registry.started, ["lookup"] * 4)


# ---------------------------------------------------------------------------
#  5. Schema enum pruning
# ---------------------------------------------------------------------------

class SchemaPruneTests(unittest.TestCase):
    @staticmethod
    def sched_spec(**extra) -> dict:
        base = {
            "name": "manage_schedule",
            "description": "schedules",
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "upsert", "delete"],
                        "default": "list",
                    },
                },
                "required": [],
            },
        }
        base.update(extra)
        return base

    def test_denied_action_is_removed_from_the_enum(self):
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_schedule:delete"]))
        specs = _filter_tool_specs(node, [self.sched_spec()])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["input_schema"]["properties"]["action"]["enum"], ["list", "upsert"])

    def test_pruning_only_changes_enum_and_does_not_mutate_input(self):
        node = Node(
            id="n", type="ai",
            tool_access=ToolAccess(mode="allowlist", allow=["manage_schedule:upsert,delete"]),
        )
        original = self.sched_spec()
        before = copy.deepcopy(original)
        specs = _filter_tool_specs(node, [original])
        action = specs[0]["input_schema"]["properties"]["action"]
        self.assertEqual(action["enum"], ["upsert", "delete"])
        # Production prunes only the enum; it leaves defaults and required intact.
        self.assertEqual(action["default"], "list")
        self.assertEqual(specs[0]["input_schema"]["required"], [])
        self.assertEqual(original, before)
        self.assertIsNot(specs[0], original)

    def test_excluded_tool_and_emptied_enum_drop_the_spec(self):
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["manage_schedule"]))
        self.assertEqual(_filter_tool_specs(node, [self.sched_spec()]), [])
        only_node = Node(
            id="n2", type="ai",
            tool_access=ToolAccess(mode="allowlist", allow=["manage_schedule:list"]),
        )
        specs = _filter_tool_specs(only_node, [self.sched_spec()])
        self.assertEqual(specs[0]["input_schema"]["properties"]["action"]["enum"], ["list"])

    def test_grep_mode_enum_is_pruned(self):
        grep_spec = {
            "name": "grep",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["search", "replace"], "default": "search"},
                },
                "required": ["query"],
            },
        }
        node = Node(id="n", type="ai", tool_access=ToolAccess(mode="all", deny=["grep:replace"]))
        specs = _filter_tool_specs(node, [grep_spec])
        self.assertEqual(specs[0]["input_schema"]["properties"]["mode"]["enum"], ["search"])
        self.assertEqual(specs[0]["input_schema"]["properties"]["mode"]["default"], "search")


# ---------------------------------------------------------------------------
#  6. Aggregate dispatch schema, control gating, and worker tools
# ---------------------------------------------------------------------------

class DispatchSpecTests(unittest.TestCase):
    @staticmethod
    def tools(node):
        _, tools = build_node_tool_specs(registry=FakeRegistry({}), node=node)
        return {tool["function"]["name"]: tool["function"] for tool in tools}

    def test_aggregate_dispatch_preserves_exact_target_ids(self):
        node = Node(id="parent", type="ai", delegate_targets=["scout", "scout.x", "scout_x", "smith", "scout"])
        tools = self.tools(node)
        self.assertIn("dispatch_to", tools)
        self.assertFalse(any(name.startswith("dispatch_to_") for name in tools))
        parameters = tools["dispatch_to"]["parameters"]
        self.assertEqual(parameters["properties"]["target"]["enum"], ["scout", "scout.x", "scout_x", "smith"])
        self.assertEqual(parameters["required"], ["target", "instruction"])
        self.assertEqual(parameters["properties"]["context_mode"]["default"], "accumulate")
        other = Node(id="other", type="ai", delegate_targets=["intruder"])
        self.tools(other)
        self.assertNotIn("intruder", parameters["properties"]["target"]["enum"])

    def test_dispatch_respects_control_tool_selection(self):
        for controls, enabled in (("none", False), (["finish"], False), (["dispatch_to"], True)):
            with self.subTest(controls=controls):
                node = Node(id="parent", type="ai", delegate_targets=["scout"], control_tools=controls)
                self.assertEqual("dispatch_to" in self.tools(node), enabled)

    def test_legacy_dispatch_aliases_are_authorized_without_extra_schema(self):
        node = Node(id="parent", type="ai", delegate_targets=["migration.scout"], control_tools=["dispatch_to"])
        _, tools = build_node_tool_specs(registry=FakeRegistry({}), node=node)
        legacy_name = _dispatch_tool_name("migration.scout")
        self.assertIn(legacy_name, allowed_pseudo_names(tools, node))
        self.assertEqual(_dispatch_target_from_tool_name(legacy_name), "migration.scout")
        self.assertNotIn(legacy_name, [tool["function"]["name"] for tool in tools])
        self.assertEqual(_dispatch_target_from_tool_name("finish"), "")

    def test_worker_defaults_to_fork_and_ask_is_not_injected(self):
        node = Node(id="parent", type="ai")
        tools = self.tools(node)
        self.assertIn("spawn_worker", tools)
        self.assertNotIn("ask", tools)
        parameters = tools["spawn_worker"]["parameters"]
        self.assertEqual(parameters["required"], ["instruction"])
        self.assertEqual(parameters["properties"]["context_mode"]["enum"], ["fresh", "fork"])
        self.assertEqual(parameters["properties"]["context_mode"]["default"], "fork")
        node.control_tools = "none"
        self.assertNotIn("spawn_worker", self.tools(node))


# ---------------------------------------------------------------------------
#  7. Provider history contract is untouched
# ---------------------------------------------------------------------------

class ProviderHistoryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_history_still_strips_engine_only_fields(self):
        history = [
            {"role": "user", "content": "question", "response_info": {"ui": True}},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"q": 1}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "found"},
            {"role": "user", "content": "ephemeral", "_ephemeral": True},
        ]
        before = copy.deepcopy(history)
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp)
            with patch.object(llm_call.hook_registry, "afire", side_effect=no_hook):
                prepared, action = await llm_call.prepare_request_history(
                    history, node=ls.node, provider=ls.provider, rctx=ls.rctx, step=0,
                )
            self.assertIsNone(action)
            self.assertEqual(ls.messages if ls.messages else history, before)
            self.assertEqual(len(prepared), 3)
            self.assertNotIn("response_info", json.dumps(prepared))
            self.assertNotIn("_ephemeral", json.dumps(prepared))
            self.assertEqual([row["role"] for row in prepared], ["user", "assistant", "tool"])

    async def test_formatter_owned_messages_reach_the_provider_unchanged(self):
        formatter = create_tool_formatter("native")
        self.assertEqual(formatter.mode, "native")
        # The loop only appends what the formatter produced; it does not rewrite
        # provider-owned history shapes.
        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp)
            ls.formatter = formatter
            msg = formatter.format_tool_result(tool_call("c1", "lookup"), "found")
            self.assertEqual(msg.get("tool_call_id"), "c1")

    async def test_preempt_with_only_attachments_is_still_injectable(self):
        self.assertTrue(llm_call._has_injectable_preempt({"message": "", "attachment_count": 2}))
        self.assertTrue(llm_call._has_injectable_preempt({"attachments": [{"path": "a.png"}]}))
        self.assertFalse(llm_call._has_injectable_preempt({"message": "  "}))
        self.assertFalse(llm_call._has_injectable_preempt(None))


if __name__ == "__main__":
    unittest.main()
