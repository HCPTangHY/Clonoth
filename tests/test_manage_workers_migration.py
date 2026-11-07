"""Offline checks for production-style worker control. No live Supervisor calls."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.context import RunContext
from engine.inference import pseudo_handlers as handlers
from engine.inference.pseudo_tools import (
    _is_pseudo_tool_name, _manage_workers_spec, allowed_pseudo_names, build_node_tool_specs,
)
from engine.node import Node, ToolAccess

FIRST = "12345678-1111-4111-8111-111111111111"
SECOND = "87654321-2222-4222-8222-222222222222"
CALLER = "aaaaaaaa-3333-4333-8333-333333333333"


def task(tid=FIRST, node="scout", status="running", **extra):
    return {"task_id": tid, "node_id": node, "status": status,
            "created_at": "2026-01-01T00:00:00+00:00", **extra}


class WorkerSpecsTests(unittest.TestCase):
    def names(self, *, controls="all", deny=()):
        node = Node(id="parent", type="ai", control_tools=controls,
                    tool_access=ToolAccess(mode="all", deny=list(deny)))
        registry = SimpleNamespace(list_specs=lambda: [])
        _, specs = build_node_tool_specs(registry=registry, node=node)
        return allowed_pseudo_names(specs, node)

    def test_schema_matches_production_actions_and_target_fields(self):
        spec = _manage_workers_spec()["function"]
        self.assertEqual(spec["name"], "manage_workers")
        schema = spec["parameters"]
        self.assertEqual(schema["required"], ["action"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["list", "message", "preempt", "cancel"])
        self.assertEqual(set(schema["properties"]), {"action", "target", "task_id", "node_id", "message"})
        self.assertIn("omit", spec["description"])

    def test_control_tool_is_available_without_active_children(self):
        self.assertIn("manage_workers", self.names())
        self.assertIn("manage_workers", self.names(controls=["manage_workers"]))

    def test_control_none_and_explicit_exclusion_are_honored(self):
        self.assertNotIn("manage_workers", self.names(controls="none"))
        self.assertNotIn("manage_workers", self.names(controls=["finish"]))
        self.assertNotIn("manage_workers", self.names(deny=["manage_workers"]))

    def test_removed_names_have_no_schema_or_pseudo_alias(self):
        for name in ("preempt_task", "cancel_active_tasks"):
            self.assertFalse(_is_pseudo_tool_name(name))
            self.assertNotIn(name, self.names())
            self.assertNotIn("manage_workers", self.names(controls=[name]))
        self.assertFalse(hasattr(handlers, "_handle_pseudo_preempt_task"))

    def test_elapsed_time_handles_invalid_naive_and_future_dates(self):
        self.assertEqual(handlers._task_elapsed_seconds("bad"), 0)
        self.assertEqual(handlers._task_elapsed_seconds("2999-01-01T00:00:00Z"), 0)
        self.assertGreater(handlers._task_elapsed_seconds("2020-01-01T00:00:00"), 0)


class WorkerHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.payload = {"ok": True, "cancelled_count": 1}
        self.failure = None
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        self.ctx = SimpleNamespace(
            supervisor_url="http://supervisor.invalid", session_id="branch_parent",
            parent_session_id="parent", task_id=CALLER, http=self.http,
            list_active_child_tasks=AsyncMock(return_value=[task()]), tool_call_log=[],
        )
        self.ls = SimpleNamespace(
            rctx=self.ctx, node=Node(id="parent", type="ai", tool_mode="native"), messages=[],
            formatter=SimpleNamespace(format_tool_result=lambda call, content: {
                "role": "tool", "tool_call_id": call.id, "content": content,
            }),
        )

    async def asyncTearDown(self):
        await self.http.aclose()

    def transport(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return httpx.Response(self.status, json=self.payload, request=request)

    async def call(self, args):
        call = SimpleNamespace(id="control-call", name="manage_workers", arguments=args)
        with patch("engine.inference.ai_step._shadow_write") as write:
            self.assertIsNone(await handlers._handle_pseudo_tool(self.ls, call, 0))
            write.assert_called_once()
        result = self.ls.messages[-1]
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "control-call")
        self.assertEqual(self.ctx.tool_call_log[-1]["name"], "manage_workers")
        return json.loads(result["content"])

    async def test_list_has_ids_timing_and_filters_caller_and_terminal_tasks(self):
        self.ctx.list_active_child_tasks.return_value = [task(), task(CALLER), task(SECOND, status="done")]
        result = await self.call({"action": "list"})
        self.assertEqual(result["active_children"], 1)
        self.assertEqual(result["tasks"][0]["task_id"], FIRST[:12])
        self.assertIsInstance(result["tasks"][0]["elapsed_sec"], int)
        self.assertEqual(self.requests, [])

    async def test_empty_list_and_node_filter(self):
        self.assertEqual((await self.call({"action": "list", "node_id": "absent"}))["active_children"], 0)
        self.ctx.list_active_child_tasks.return_value = []
        self.assertEqual((await self.call({"action": "list"}))["tasks"], [])

    async def test_message_resolves_id_prefix_and_delivers_instruction(self):
        result = await self.call({"action": "message", "target": "12345678", "message": "new direction"})
        self.assertTrue(result["success"])
        self.assertEqual(self.requests[-1].url.path, f"/v1/tasks/{FIRST}/preempt")
        self.assertEqual(json.loads(self.requests[-1].content), {"message": "new direction"})

    async def test_preempt_without_message_requests_stop(self):
        self.assertTrue((await self.call({"action": "preempt", "node_id": "scout"}))["success"])
        self.assertEqual(self.requests[-1].url.path, f"/v1/tasks/{FIRST}/preempt")
        self.assertEqual(self.requests[-1].content, b"")

    async def test_preempt_with_message_preserves_production_continue_semantics(self):
        result = await self.call({"action": "preempt", "task_id": FIRST, "message": "continue differently"})
        self.assertTrue(result["success"])
        self.assertIn("Instruction injected", result["message"])
        self.assertEqual(json.loads(self.requests[-1].content), {"message": "continue differently"})

    async def test_missing_message_or_target_never_sends_request(self):
        for args in ({"action": "message"}, {"action": "message", "target": FIRST},
                     {"action": "preempt"}, {}, {"action": "unknown"}):
            with self.subTest(args=args):
                self.assertFalse((await self.call(args))["success"])
        self.assertEqual(self.requests, [])

    async def test_unknown_target_and_foreign_task_have_no_global_fallback(self):
        for target in (SECOND, "foreign_node"):
            result = await self.call({"action": "cancel", "target": target})
            self.assertFalse(result["success"])
            self.assertIn("current conversation", result["error"])
        self.assertEqual(self.requests, [])

    async def test_ambiguous_node_is_rejected_instead_of_cancelling_everything(self):
        self.ctx.list_active_child_tasks.return_value = [task(), task(SECOND)]
        result = await self.call({"action": "cancel", "target": "scout"})
        self.assertFalse(result["success"])
        self.assertIn("ambiguous", result["error"])
        self.assertEqual(self.requests, [])

    async def test_ambiguous_id_prefix_is_rejected(self):
        self.ctx.list_active_child_tasks.return_value = [task(), task("12345678-4444-4444-8444-444444444444", "other")]
        self.assertFalse((await self.call({"action": "message", "target": "12345678", "message": "text"}))["success"])
        self.assertEqual(self.requests, [])

    async def test_explicit_node_filter_cannot_target_another_node(self):
        result = await self.call({"action": "cancel", "target": FIRST, "node_id": "other"})
        self.assertFalse(result["success"])
        self.assertEqual(self.requests, [])

    async def test_caller_cannot_be_cancelled_even_if_discovery_includes_it(self):
        self.ctx.list_active_child_tasks.return_value = [task(CALLER)]
        self.assertFalse((await self.call({"action": "cancel", "target": CALLER}))["success"])
        self.assertEqual(self.requests, [])

    async def test_targeted_cancel_uses_task_endpoint(self):
        self.assertTrue((await self.call({"action": "cancel", "target": FIRST}))["success"])
        self.assertEqual(self.requests[-1].url.path, f"/v1/tasks/{FIRST}/cancel")

    async def test_cancel_all_preserves_production_omission_contract(self):
        result = await self.call({"action": "cancel"})
        self.assertTrue(result["success"])
        self.assertEqual(result["cancelled"], "all")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/v1/sessions/parent/cancel_active_tasks")
        self.assertEqual(dict(request.url.params), {"exclude_task_id": CALLER})
        self.ctx.list_active_child_tasks.assert_not_awaited()

    async def test_cancel_by_node_is_scoped_and_excludes_caller(self):
        result = await self.call({"action": "cancel", "node_id": "scout"})
        self.assertTrue(result["success"])
        self.assertEqual(dict(self.requests[-1].url.params), {"exclude_task_id": CALLER, "node_id": "scout"})

    async def test_session_wide_cancel_requires_caller_identity(self):
        self.ctx.task_id = ""
        self.assertFalse((await self.call({"action": "cancel"}))["success"])
        self.assertEqual(self.requests, [])

    async def test_missing_session_rejects_actions(self):
        self.ctx.parent_session_id = self.ctx.session_id = ""
        self.assertFalse((await self.call({"action": "cancel"}))["success"])
        self.assertEqual(self.requests, [])

    async def test_http_and_transport_errors_are_failures(self):
        for status in (401, 404, 500):
            self.status = status
            self.assertFalse((await self.call({"action": "cancel", "target": FIRST}))["success"])
        self.failure = httpx.ConnectError("offline")
        self.assertFalse((await self.call({"action": "cancel"}))["success"])

    async def test_http_200_false_payload_is_not_reported_as_success(self):
        self.payload = {"ok": False, "error": "refused"}
        for args in ({"action": "cancel"}, {"action": "cancel", "target": FIRST}):
            result = await self.call(args)
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "refused")

    async def test_discovery_exception_is_reported(self):
        self.ctx.list_active_child_tasks.side_effect = RuntimeError("discovery unavailable")
        self.assertFalse((await self.call({"action": "list"}))["success"])
        self.assertEqual(self.requests, [])


class WorkerDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def discover(self, running, children, *, running_status=200):
        seen = []
        def transport(request):
            seen.append(request)
            if request.url.path.endswith("/running_tasks"):
                return httpx.Response(running_status, json={"tasks": running})
            if request.url.path.endswith("/children"):
                return httpx.Response(200, json=children)
            raise AssertionError(f"Unexpected endpoint: {request.url.path}")
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ctx = RunContext(workspace_root=Path("/unused"), supervisor_url="http://supervisor.invalid",
                             session_id="branch", parent_session_id="parent", task_id=CALLER,
                             worker_id="worker", http=http, llm_http=http)
            result = await ctx.list_active_child_tasks()
        self.assertEqual({r.url.path for r in seen}, {
            "/v1/sessions/parent/running_tasks", "/v1/sessions/parent/children",
        })
        self.assertTrue(all(r.extensions["timeout"]["read"] == 3.0 for r in seen))
        return result

    async def test_combines_and_deduplicates_scoped_sources(self):
        rows = await self.discover([task(), task(CALLER), task("entry", is_user_entry=True)],
                                   [task(), task(SECOND, "writer", "suspended"), task("done", status="done")])
        self.assertEqual({r["task_id"] for r in rows}, {FIRST, SECOND})

    async def test_one_endpoint_failure_still_returns_other_source(self):
        rows = await self.discover([], [task(SECOND)], running_status=500)
        self.assertEqual([r["task_id"] for r in rows], [SECOND])

    async def test_empty_sources_return_empty_list(self):
        self.assertEqual(await self.discover([], []), [])


if __name__ == "__main__":
    unittest.main()
