"""Offline audit of pseudo-tool authorization after retiring preempt_task / cancel_active_tasks.

Two invariants are pinned here:

1. The retired names are not pseudo tools, are not authorized as aliases of
   manage_workers, and a model that hallucinates them never performs an HTTP
   mutation (they are rejected as nonexistent tools).
2. ``manage_workers:list`` authorization buys exactly the ``list`` action: it
   issues no HTTP and every mutating action is rejected before the handler.

Everything runs offline: httpx.MockTransport-style fake HTTP / fake ctx only,
no supervisor, no provider, no network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.inference import ai_step
from engine.inference import pseudo_handlers as handlers
from engine.inference import pseudo_tools
from engine.inference.pseudo_tools import (
    _PSEUDO_TOOL_NAMES,
    _RETIRED_PSEUDO_TOOL_NAMES,
    _is_pseudo_tool_name,
    allowed_pseudo_names,
    build_node_tool_specs,
    is_tool_action_allowed,
)
from engine.node import Node, ToolAccess
from test_engine_execution_migration import FakeRegistry, make_ls, no_hook, response, tool_call

FIRST = "12345678-1111-4111-8111-111111111111"
CALLER = "aaaaaaaa-3333-4333-8333-333333333333"
RETIRED = ("preempt_task", "cancel_active_tasks")


class RecordingHTTP:
    """HTTP stand-in that records calls; nothing here should ever need one."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status = status
        self.payload = {"ok": True} if payload is None else payload

    def _record(self, method: str, url: str) -> httpx.Response:
        self.calls.append((method, url))
        return httpx.Response(self.status, json=self.payload, request=httpx.Request(method, url))

    async def get(self, url, **kwargs):
        return self._record("GET", url)

    async def post(self, url, **kwargs):
        return self._record("POST", url)


def child(task_id: str = FIRST, node: str = "scout", status: str = "running") -> dict:
    return {"task_id": task_id, "node_id": node, "status": status,
            "created_at": "2026-01-01T00:00:00+00:00"}


def control_specs(node: Node) -> list[dict]:
    registry = SimpleNamespace(list_specs=lambda: [])
    _, specs = build_node_tool_specs(registry=registry, node=node)
    return specs


class RetiredNameTests(unittest.TestCase):
    def test_retired_names_are_plain_unknown_tools(self):
        for name in RETIRED:
            with self.subTest(name=name):
                self.assertFalse(_is_pseudo_tool_name(name))
                self.assertNotIn(name, _PSEUDO_TOOL_NAMES)
                self.assertIn(name, _RETIRED_PSEUDO_TOOL_NAMES)

    def test_manage_workers_grants_no_legacy_alias(self):
        node = Node(id="parent", type="ai", control_tools=["manage_workers"])
        names = allowed_pseudo_names(control_specs(node), node)
        self.assertIn("manage_workers", names)
        for name in RETIRED:
            self.assertNotIn(name, names)
        self.assertFalse(hasattr(pseudo_tools, "_preempt_task_spec"))
        self.assertFalse(hasattr(handlers, "_handle_pseudo_preempt_task"))

    def test_legacy_control_tool_selection_injects_nothing(self):
        for name in RETIRED:
            with self.subTest(name=name):
                node = Node(id="parent", type="ai", control_tools=[name])
                self.assertNotIn("manage_workers", allowed_pseudo_names(control_specs(node), node))

    def test_retired_names_stay_reserved_against_script_tools(self):
        from toolbox.builtins import RESERVED_TOOL_NAMES
        self.assertTrue({"manage_workers", *RETIRED} <= RESERVED_TOOL_NAMES)


class LegacyCallRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hallucinated_legacy_calls_never_reach_http(self):
        node = Node(id="parent", type="ai", control_tools=["manage_workers"])
        # Even a stale authorization set that still lists a retired name cannot run it.
        allowed = allowed_pseudo_names(control_specs(node), node) | {"preempt_task"}
        registry = FakeRegistry({})
        seen_pseudo_calls: list[str] = []

        async def record_pseudo(ls, call, step):
            seen_pseudo_calls.append(call.name)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            ls = make_ls(tmp, registry=registry, node=node, allowed_pseudo=allowed)
            ls.rctx.http = RecordingHTTP()
            calls = response(
                tool_call("c1", "preempt_task", {"task_id": FIRST, "message": "keep going"}),
                tool_call("c2", "cancel_active_tasks", {"target": FIRST}),
                tool_call("c3", "manage_workers", {"action": "list"}),
            )
            with patch.object(ai_step, "_handle_pseudo_tool", side_effect=record_pseudo), \
                    patch.object(ai_step.hook_registry, "afire", side_effect=no_hook):
                self.assertIsNone(await ai_step._handle_tool_calls(ls, calls, 0))

            self.assertEqual(seen_pseudo_calls, ["manage_workers"])
            self.assertEqual(ls.rctx.http.calls, [])
            self.assertEqual(registry.started, [])
            rejections = [m for m in ls.messages if m.get("role") == "tool"]
            self.assertEqual([m["content"].split("'")[1] for m in rejections], list(RETIRED))
            self.assertTrue(all("does not exist" in m["content"] for m in rejections))


class ManageWorkersActionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = RecordingHTTP()
        self.ctx = SimpleNamespace(
            supervisor_url="http://supervisor.invalid", session_id="branch",
            parent_session_id="parent", task_id=CALLER, http=self.http,
            list_active_child_tasks=AsyncMock(
                return_value=[child(), child(CALLER), child(status="done")]
            ),
            tool_call_log=[],
        )
        self.ls = SimpleNamespace(
            rctx=self.ctx, node=Node(id="parent", type="ai", tool_mode="native"), messages=[],
            formatter=SimpleNamespace(format_tool_result=lambda call, content: {
                "role": "tool", "tool_call_id": call.id, "content": content,
            }),
        )

    async def call(self, args):
        pseudo_call = SimpleNamespace(id="control-call", name="manage_workers", arguments=args)
        with patch("engine.inference.ai_step._shadow_write"):
            self.assertIsNone(await handlers._handle_pseudo_tool(self.ls, pseudo_call, 0))
        return json.loads(self.ls.messages[-1]["content"])

    def test_list_only_authorization_covers_exactly_list(self):
        node = Node(id="parent", type="ai",
                    tool_access=ToolAccess(mode="allowlist", allow=["manage_workers:list"]))
        self.assertTrue(is_tool_action_allowed(node, "manage_workers", {"action": "list"}))
        for action in ("message", "preempt", "cancel"):
            self.assertFalse(is_tool_action_allowed(node, "manage_workers", {"action": action}))

    async def test_list_reads_discovery_only_and_issues_no_http(self):
        result = await self.call({"action": "list"})
        self.assertTrue(result["success"])
        self.assertEqual(result["active_children"], 1)
        self.assertEqual(result["tasks"][0]["task_id"], FIRST[:12])
        self.ctx.list_active_child_tasks.assert_awaited_once()
        self.assertEqual(self.http.calls, [])

    async def test_direct_legacy_call_is_ignored_without_http(self):
        for name in RETIRED:
            with self.subTest(name=name):
                pseudo_call = SimpleNamespace(
                    id="legacy-call", name=name, arguments={"task_id": FIRST, "target": FIRST},
                )
                self.assertIsNone(await handlers._handle_pseudo_tool(self.ls, pseudo_call, 0))
        self.assertEqual(self.ls.messages, [])
        self.assertEqual(self.ctx.tool_call_log, [])
        self.assertEqual(self.http.calls, [])


if __name__ == "__main__":
    unittest.main()
