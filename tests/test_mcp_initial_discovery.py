"""Offline generation-scoped startup discovery checks; no external MCP services."""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.builtin import mcp


class InitialDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {"generation": 0, "task": None, "stop": None}
        self.registry = SimpleNamespace(workspace_root=Path("."))
        self.tasks = []
        self.patches = [
            patch.object(mcp, "_bridge_state", return_value=self.state),
            patch.object(mcp, "_drop_bridge_ownership", new=AsyncMock()),
            patch.object(mcp, "_config_mtime", return_value=None),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        await mcp.shutdown_bridge()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for p in reversed(self.patches):
            p.stop()

    def start(self, *, replace_registry=False):
        if replace_registry:
            self.registry = SimpleNamespace(workspace_root=Path("."))
        mcp._start_engine_bridge(self.registry, SimpleNamespace(hooks=None))
        self.tasks.append(self.state["task"])

    async def test_not_started_and_wrong_registry(self):
        self.assertFalse(await mcp.wait_mcp_initial_discovery())
        with patch.object(mcp, "refresh_mcp_tools", new=AsyncMock(return_value=0)):
            self.start()
            self.assertFalse(await mcp.wait_mcp_initial_discovery(registry=object()))
            self.assertTrue(await mcp.wait_mcp_initial_discovery(registry=self.registry))

    async def test_waits_until_discovery_has_registered_tools(self):
        gate = asyncio.Event()
        specs = []

        async def discover(*args, **kwargs):
            await gate.wait()
            specs.append("mcp_probe_read")

        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover):
            self.start()
            waiter = asyncio.create_task(mcp.wait_mcp_initial_discovery())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.assertEqual(specs, [])
            gate.set()
            self.assertTrue(await waiter)
            self.assertEqual(specs, ["mcp_probe_read"])

    async def test_reentry_shares_initial_future_before_and_during_discovery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover(*args, **kwargs):
            entered.set()
            await release.wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover) as refresh:
            self.start()
            task, initial = self.state["task"], self.state["initial_discovery"]
            self.start()  # Before the scheduled task has ever run.
            self.assertIs(self.state["task"], task)
            self.assertIs(self.state["initial_discovery"], initial)
            await entered.wait()
            self.start()
            self.assertIs(self.state["task"], task)
            self.assertFalse(initial[2].done())
            release.set()
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))
            self.start()  # Healthy idle watcher also survives node reentry.
            self.assertIs(self.state["task"], task)
            refresh.assert_awaited_once()

    async def test_finished_watcher_is_restarted(self):
        with patch.object(mcp, "refresh_mcp_tools", new=AsyncMock(return_value=0)):
            self.start()
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))
            old_task = self.state["task"]
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
            self.start()
            self.assertIsNot(self.state["task"], old_task)
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))

    async def test_failed_attempt_releases_waiters_for_normal_retry(self):
        with patch.object(mcp, "refresh_mcp_tools", new=AsyncMock(side_effect=ValueError("offline"))):
            self.start()
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))
            self.assertTrue(self.state["retry_pending"])

    async def test_timeout_does_not_cancel_shared_discovery(self):
        gate = asyncio.Event()
        async def discover(*args, **kwargs):
            await gate.wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover):
            self.start()
            ready = self.state["initial_discovery"][2]
            self.assertFalse(await mcp.wait_mcp_initial_discovery(timeout=.01))
            self.assertFalse(ready.cancelled())
            self.assertFalse(self.state["task"].done())
            gate.set()
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))

    async def test_caller_cancel_does_not_cancel_other_waiter(self):
        gate = asyncio.Event()
        async def discover(*args, **kwargs):
            await gate.wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover):
            self.start()
            first = asyncio.create_task(mcp.wait_mcp_initial_discovery())
            second = asyncio.create_task(mcp.wait_mcp_initial_discovery())
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(self.state["initial_discovery"][2].cancelled())
            gate.set()
            self.assertTrue(await second)

    async def test_shutdown_releases_waiter_before_slow_discovery_stops(self):
        async def discover(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover), patch.object(mcp, "_BRIDGE_STOP_GRACE_SEC", .01):
            self.start()
            waiter = asyncio.create_task(mcp.wait_mcp_initial_discovery())
            await asyncio.sleep(0)
            stopped = asyncio.create_task(mcp.shutdown_bridge())
            self.assertFalse(await asyncio.wait_for(waiter, .2))
            await stopped

    async def test_reload_old_generation_cannot_complete_new_future(self):
        gates = [asyncio.Event(), asyncio.Event()]
        started = asyncio.Event()
        count = 0
        async def discover(*args, **kwargs):
            nonlocal count
            n = count
            count += 1
            started.set()
            await gates[n].wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover), patch.object(mcp, "_BRIDGE_STOP_GRACE_SEC", .01):
            self.start()
            await started.wait()
            old_ready = self.state["initial_discovery"][2]
            self.start(replace_registry=True)
            new_ready = self.state["initial_discovery"][2]
            self.assertFalse(old_ready.result())
            await asyncio.sleep(.03)
            self.assertFalse(new_ready.done())
            gates[1].set()
            self.assertTrue(await mcp.wait_mcp_initial_discovery(timeout=1))

    async def test_reload_does_not_reset_waiter_deadline(self):
        async def discover(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(mcp, "refresh_mcp_tools", side_effect=discover), patch.object(mcp, "_BRIDGE_STOP_GRACE_SEC", .001):
            self.start()
            waiter = asyncio.create_task(mcp.wait_mcp_initial_discovery(timeout=.03))
            for _ in range(5):
                await asyncio.sleep(.01)
                self.start(replace_registry=True)
            self.assertTrue(waiter.done())
            self.assertFalse(await waiter)

    async def test_superseded_refresh_does_not_publish_tools(self):
        self.state.update(disposers={}, specs={}, failed_clients=set(),
                          registry=self.registry, workspace_root=Path("."))
        entered = asyncio.Event()
        release = asyncio.Event()
        async def list_tools(*args):
            entered.set()
            await release.wait()
            return {"ok": True, "tools": [{"name": "old"}]}
        with patch.object(mcp.mcp_runtime, "list_clients", return_value=[{"id": "probe"}]), \
                patch.object(mcp.mcp_runtime, "reconcile_stdio_actors", new=AsyncMock()), \
                patch.object(mcp.mcp_runtime, "list_tools", side_effect=list_tools):
            refresh = asyncio.create_task(mcp.refresh_mcp_tools(self.registry))
            await entered.wait()
            self.state["generation"] += 1
            release.set()
            self.assertEqual(await refresh, 0)
            self.assertEqual(self.state["disposers"], {})

    async def test_other_loop_future_is_not_awaited(self):
        loop = asyncio.new_event_loop()
        try:
            self.state["initial_discovery"] = (0, self.registry, loop.create_future())
            self.assertFalse(await mcp.wait_mcp_initial_discovery())
        finally:
            self.state.pop("initial_discovery")
            loop.close()


if __name__ == "__main__":
    unittest.main()
