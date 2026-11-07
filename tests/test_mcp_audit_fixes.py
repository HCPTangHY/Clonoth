"""MCP audit fixes: concurrency, cancellation isolation and no-replay checks.

Companion to ``tests/test_engine_mcp_migration.py``. That file pins the public
surface; this one pins the *behaviour under concurrent use* that the audit
flagged, and every assertion is a real side-effect count, not a name check:

* a business call is executed exactly once whatever fails (dead pipe, tool
  error, timeout, cancellation);
* cancelling or timing out one request never breaks another caller of the same
  client: a request that was still queued is re-dispatched on the fresh session
  (it never reached the server, so that is not a replay), and a queued-only
  cancellation does not touch the session at all;
* one client's timeout leaves every other client's session alive;
* repeated bridge unload/load cycles leave no watcher task, no tool
  registration, no ledger entry and no child process behind.

Synchronisation is deterministic: the fake server exposes gates (asyncio.Event)
that the test opens explicitly, and queue-state helper below is polled with
``wait_until`` instead of sleeping for a fixed number of milliseconds.

Run (all temp files stay inside the task's temp dir)::

    TMPDIR=data/temp/audit-fixes-2ztfkgb3 PYTHONDONTWRITEBYTECODE=1 \
        python3.11 -B tests/test_mcp_audit_fixes.py

Every "server" below is an in-process fake installed over the runtime module
globals: ``stdio_client`` never spawns a process and no socket is opened.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.builtin.mcp as mcp_module  # noqa: E402
from engine.builtin.mcp import mcp_runtime  # noqa: E402
from engine.hooks.registry import HookRegistry  # noqa: E402

# Generous bound for condition polling; not a timing assumption, only a guard
# against a hung test.
_WAIT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
#  in-process fake MCP server / SDK
# ---------------------------------------------------------------------------


class _Server:
    """Fake MCP server with per-tool gates, one-shot failures and call log.

    ``gates`` are asyncio.Events the test opens (or never opens); a call blocks
    inside the "server" until its gate is set, which makes every concurrency
    scenario below deterministic instead of timing-based. ``started`` records
    the event for each tool so a test can wait for "the call really began".
    """

    def __init__(self, *, gates: dict[str, asyncio.Event] | None = None,
                 failures: dict[str, BaseException] | None = None) -> None:
        self.process_starts = 0
        self.process_exits = 0
        self.initializes = 0
        self.calls: list[tuple[str, dict]] = []
        self.tools = [{"name": "echo", "inputSchema": {"type": "object", "properties": {}}}]
        self.gates = dict(gates or {})
        self.failures = dict(failures or {})
        self.started = {name: asyncio.Event() for name in set(self.gates) | set(self.failures)}

    @property
    def live_processes(self) -> int:
        return self.process_starts - self.process_exits

    @property
    def called(self) -> list[str]:
        return [name for name, _arguments in self.calls]

    def started_event(self, name: str) -> asyncio.Event:
        return self.started.setdefault(name, asyncio.Event())


class _Params:
    def __init__(self, *, command: str, args: list[str], env: dict) -> None:
        self.command = command
        self.args = args
        self.env = env


class _FakeStdio:
    def __init__(self, server: _Server) -> None:
        self.server = server

    def __call__(self, params: _Params) -> "_FakeStdio":
        return self

    async def __aenter__(self):
        self.server.process_starts += 1
        return (object(), object())

    async def __aexit__(self, *exc_info) -> bool:
        self.server.process_exits += 1
        return False


def _session_class(server: _Server):
    class _Session:
        def __init__(self, read_stream, write_stream) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def initialize(self) -> None:
            server.initializes += 1

        async def list_tools(self) -> dict:
            return {"tools": [dict(tool) for tool in server.tools]}

        async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
            server.calls.append((name, dict(arguments or {})))
            server.started_event(name).set()
            gate = server.gates.get(name)
            if gate is not None:
                await gate.wait()
            failure = server.failures.pop(name, None)
            if failure is not None:
                raise failure
            return {"content": [{"type": "text", "text": f"{name} ok"}], "isError": False}

    return _Session


@contextlib.contextmanager
def fake_sdk(server: _Server):
    with mock.patch.multiple(
        mcp_runtime,
        _MCP_IMPORT_ERROR=None,
        StdioServerParameters=_Params,
        stdio_client=_FakeStdio(server),
        ClientSession=_session_class(server),
    ):
        yield


class _PipeGone(ConnectionError):
    """Stands in for anyio's BrokenResourceError (a transport failure)."""


def write_config(root: Path, clients: dict) -> None:
    path = root / "data" / "mcp_clients.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "clients": clients}, sort_keys=False), encoding="utf-8")


async def wait_until(predicate, *, timeout: float = _WAIT_TIMEOUT, interval: float = 0.005) -> bool:
    """Poll a real observable until it holds (deterministic, bounded)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _actor(client_id: str = "demo"):
    return mcp_runtime.shared_state()["actors"].get(client_id)


def _queued(actor) -> int:
    """White-box diagnostic: requests parked in the actor's queue right now."""
    queue = getattr(actor, "_queue", None)
    return queue.qsize() if queue is not None else 0


class _FakeToolRegistry:
    def __init__(self, root: Path) -> None:
        self.workspace_root = Path(root)
        self.tools: dict[str, dict] = {}

    def register_builtin_tool(self, name, description, input_schema, func, *, pure: bool = False):
        entry = {"description": description, "input_schema": input_schema, "func": func, "pure": bool(pure)}
        self.tools[name] = entry

        def _dispose() -> None:
            if self.tools.get(name) is entry:
                self.tools.pop(name, None)

        return _dispose


class _Faces:
    def __init__(self, faces: dict) -> None:
        self._faces = faces

    def get(self, name: str):
        return self._faces.get(name)


class _EngineCtx:
    def __init__(self, registry, hooks: HookRegistry) -> None:
        self.contributions = _Faces({"tools": registry})
        self.hooks = hooks


class _ToolCallCtx:
    async def check_cancelled(self) -> bool:
        return False


class _RuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Shared tempdir + global-state cleanup for the actor pool and bridge."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_config(
            self.root,
            {
                "demo": {"transport": "stdio", "enabled": True, "command": "fake-mcp"},
                "second": {"transport": "stdio", "enabled": True, "command": "fake-mcp"},
            },
        )

    async def asyncTearDown(self) -> None:
        with contextlib.suppress(Exception):
            await mcp_module.shutdown_bridge()
        with contextlib.suppress(Exception):
            await mcp_runtime.close_stdio_actors()


# ---------------------------------------------------------------------------
#  one business call = one attempt
# ---------------------------------------------------------------------------


class NoReplayTests(_RuntimeTest):
    async def test_business_error_is_attempted_once_and_session_survives(self) -> None:
        server = _Server(failures={"charge": RuntimeError("card declined")})
        with fake_sdk(server):
            failed = await mcp_runtime.call_tool(self.root, "demo", "charge", {"amount": 10})
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["data"]["reason"], "tool_error")
            self.assertIn("card declined", failed["error"])
            self.assertEqual(server.called, ["charge"], "a failing business call must run once")
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertEqual(server.process_starts, 1, "a tool error must not drop the session")

    async def test_transport_failure_is_reported_once_and_only_queued_calls_move_on(self) -> None:
        # The first call is inside the server (gated) while the second one is
        # still in the queue, i.e. it has never reached the server.
        gate: asyncio.Event = asyncio.Event()
        server = _Server(gates={"charge": gate}, failures={"charge": _PipeGone("pipe gone")})
        with fake_sdk(server):
            first = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "charge", {"amount": 10})
            )
            self.assertTrue(await wait_until(lambda: server.started["charge"].is_set()))

            queued = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "echo", {"n": 2})
            )
            self.assertTrue(
                await wait_until(lambda: _queued(_actor("demo")) >= 1),
                "the second call never reached the queue",
            )

            gate.set()  # the in-flight call now fails with a dead pipe
            failed = await asyncio.wait_for(first, timeout=_WAIT_TIMEOUT)

            self.assertFalse(failed["ok"])
            self.assertEqual(failed["data"]["reason"], "connection_lost")
            self.assertIn("not retried", failed["error"])
            result = await asyncio.wait_for(queued, timeout=_WAIT_TIMEOUT)
            self.assertTrue(result["ok"], result)

        self.assertEqual(server.called, ["charge", "echo"])
        self.assertEqual(
            server.calls[0], ("charge", {"amount": 10}),
            "the dead-pipe call must be attempted exactly once",
        )
        self.assertEqual(server.process_starts, 2, "the queued call reconnects on a new session")


# ---------------------------------------------------------------------------
#  cancellation isolation
# ---------------------------------------------------------------------------


class CancellationIsolationTests(_RuntimeTest):
    async def test_cancelling_inflight_call_does_not_lose_a_queued_call(self) -> None:
        gate: asyncio.Event = asyncio.Event()  # never opened: the call blocks
        server = _Server(gates={"charge": gate})
        with fake_sdk(server):
            in_flight = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "charge", {"n": 1})
            )
            self.assertTrue(await wait_until(lambda: server.started["charge"].is_set()))
            queued = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "echo", {"n": 2})
            )
            self.assertTrue(
                await wait_until(lambda: _queued(_actor("demo")) >= 1),
                "the second call never reached the queue",
            )

            in_flight.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(in_flight, timeout=_WAIT_TIMEOUT)

            result = await asyncio.wait_for(queued, timeout=_WAIT_TIMEOUT)
            self.assertTrue(result["ok"], result)

        self.assertEqual(
            server.called, ["charge", "echo"],
            "the cancelled call must stay executed-once and the queued call must still run",
        )
        self.assertEqual(server.calls[0], ("charge", {"n": 1}))

    async def test_cancelling_a_queued_call_never_sends_it_and_keeps_the_session(self) -> None:
        gate: asyncio.Event = asyncio.Event()
        server = _Server(gates={"charge": gate})
        with fake_sdk(server):
            in_flight = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "charge", {})
            )
            self.assertTrue(await wait_until(lambda: server.started["charge"].is_set()))
            queued = asyncio.ensure_future(
                mcp_runtime.call_tool(self.root, "demo", "refund", {})
            )
            self.assertTrue(
                await wait_until(lambda: _queued(_actor("demo")) >= 1),
                "the second call never reached the queue",
            )

            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(queued, timeout=_WAIT_TIMEOUT)

            gate.set()
            self.assertTrue((await asyncio.wait_for(in_flight, timeout=_WAIT_TIMEOUT))["ok"])
            # Let the worker drain the (already cancelled) queue entry.
            self.assertTrue(await wait_until(lambda: _queued(_actor("demo")) == 0))

            self.assertEqual(server.called, ["charge"], "a cancelled queued call must never be sent")
            self.assertEqual(server.process_starts, 1, "a queued-only cancellation keeps the session")
            self.assertTrue(_actor("demo").is_alive())

    async def test_one_clients_timeout_leaves_another_clients_session_alive(self) -> None:
        gate: asyncio.Event = asyncio.Event()  # never opened: the call cannot finish
        server = _Server(gates={"hang": gate})
        with fake_sdk(server):
            self.assertTrue((await mcp_runtime.call_tool(self.root, "second", "echo", {}))["ok"])
            other_actor = _actor("second")
            self.assertTrue(other_actor.is_alive())

            timed_out = await mcp_runtime.call_tool(self.root, "demo", "hang", {}, timeout=0.2)
            self.assertFalse(timed_out["ok"])
            self.assertEqual(timed_out["data"]["reason"], "timeout")

            self.assertTrue(other_actor.is_alive(), "another client's session must not be stopped")
            self.assertTrue((await mcp_runtime.call_tool(self.root, "second", "echo", {}))["ok"])
            self.assertFalse(_actor("demo").is_alive())

        self.assertEqual(server.process_starts, 2, "two clients, two sessions, no extra restart")


# ---------------------------------------------------------------------------
#  bridge lifecycle
# ---------------------------------------------------------------------------


class BridgeLifecycleTests(_RuntimeTest):
    async def test_repeated_unload_and_load_cycles_leave_nothing_behind(self) -> None:
        server = _Server()
        with fake_sdk(server):
            for _cycle in range(2):
                registry = _FakeToolRegistry(self.root)
                hooks = HookRegistry()
                plugin = mcp_module.McpPlugin(_EngineCtx(registry, hooks))
                self.assertTrue(await wait_until(lambda: bool(registry.tools)))
                func = registry.tools["mcp_demo_echo"]["func"]
                self.assertTrue((await func({}, _ToolCallCtx()))["ok"])
                self.assertTrue(server.live_processes >= 1)

                task = plugin.teardown()
                self.assertIsNotNone(task)
                await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)

                self.assertEqual(registry.tools, {}, "unload must dispose the registered tools")
                self.assertEqual(hooks.owned("mcp"), 0, "unload must clear the ledger entries")
                self.assertEqual(server.live_processes, 0, "unload must close the child process")
                self.assertIsNone(mcp_runtime.shared_state()["task"])

        self.assertEqual(mcp_runtime.shared_state()["actors"], {})
        self.assertEqual(
            [task for task in asyncio.all_tasks() if task.get_name().startswith("mcp-bridge")],
            [],
            "no bridge watcher or teardown task may survive an unload",
        )

    async def test_reload_hands_the_bridge_over_without_a_second_watcher(self) -> None:
        server = _Server()
        with fake_sdk(server):
            first_registry = _FakeToolRegistry(self.root)
            first_hooks = HookRegistry()
            mcp_module.McpPlugin(_EngineCtx(first_registry, first_hooks))
            self.assertTrue(await wait_until(lambda: bool(first_registry.tools)))
            first_task = mcp_runtime.shared_state()["task"]

            second_registry = _FakeToolRegistry(self.root)
            second_hooks = HookRegistry()
            mcp_module.McpPlugin(_EngineCtx(second_registry, second_hooks))

            self.assertTrue(await wait_until(lambda: first_task.done()))
            self.assertTrue(await wait_until(lambda: bool(second_registry.tools)))
            self.assertTrue(await wait_until(lambda: first_registry.tools == {}))
            self.assertEqual(first_hooks.owned("mcp"), 0)
            self.assertTrue(
                await wait_until(lambda: server.live_processes == 0),
                "the replaced bridge left a child process behind",
            )
            self.assertEqual(
                len([t for t in asyncio.all_tasks() if t.get_name() == "mcp-bridge"]), 1,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
