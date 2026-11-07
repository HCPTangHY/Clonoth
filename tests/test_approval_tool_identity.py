"""Outer tool identity survives preflight and inner operation guards."""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry


class ApprovalToolIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_preserve_identity_in_inner_guards(self):
        requests = []

        def handler(request):
            if request.url.path == "/v1/ops/request":
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"safety_level": "auto"})
            return httpx.Response(200, json={"cancelled": False})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            base = ToolContext(
                supervisor_url="http://test", session_id="session", run_id="run",
                worker_id="worker", workspace_root=Path.cwd(), http=http, registry=None,
            )
            registry = ToolRegistry.__new__(ToolRegistry)

            async def inner_guard(arguments, ctx):
                await asyncio.sleep(0)
                await ctx.request_op("write_file", {"path": arguments["path"]})
                return {"ok": True, "data": {"result": "done"}}

            registry._tool_funcs = {"apply_diff": inner_guard, "manage_skill": inner_guard}
            registry._tool_specs = {}
            await asyncio.gather(*(
                registry.execute(name=name, arguments={"path": name}, ctx=replace(base))
                for name in registry._tool_funcs
            ))

        self.assertEqual(len(requests), 4)
        for request in requests:
            expected = (request["parameters"]["tool_name"] if request["op"] == "tool_call"
                        else request["parameters"]["path"])
            self.assertEqual(request["tool_name"], expected)
        self.assertEqual(base.tool_name, "")


if __name__ == "__main__":
    unittest.main()
