"""Audit-fix regression A: approval failures are never reported as a user denial.

Why this file exists: ``ToolContext.wait_for_approval`` can now end in a Supervisor
failure instead of a decision (``approval_unavailable`` after the consecutive
failure budget, ``approval_lost`` on HTTP 404, ``approval_error`` on another 4xx).
Every layer above it must pass that failure through as itself — with nothing
executed and nothing written — instead of claiming "user denied approval" or
letting a bare ``httpx.HTTPStatusError`` escape the tool call. A real human denial
and a real cancellation must keep their existing meaning.

Run: python3.11 -B tests/test_toolbox_failure_audit.py

No supervisor and no network: ``httpx.MockTransport`` plus a fake monotonic clock,
every workspace is a tempdir, and the only files touched are fixtures in it.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import toolbox.context as context_module  # noqa: E402
from toolbox._common import request_guard  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402


class _FakeClock:
    """Deterministic monotonic clock: each read advances by *step* seconds."""

    def __init__(self, step: float = 10.0, start: float = 1000.0) -> None:
        self.now = float(start)
        self.step = float(step)

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _supervisor(approval: Any):
    """MockTransport handler: policy asks for approval ap-1, then answers *approval*.

    *approval* is ``(status_code, json_body)`` or an exception instance to raise.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/ops/request":
            return httpx.Response(
                200,
                json={"safety_level": "approval_required", "approval_id": "ap-1"},
                request=request,
            )
        if path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False}, request=request)
        if path.endswith("/renew_lease"):
            return httpx.Response(200, json={"ok": True}, request=request)
        if "/v1/approvals/" in path:
            if isinstance(approval, BaseException):
                raise approval
            status, body = approval
            return httpx.Response(status, json=body, request=request)
        return httpx.Response(404, json={"error": "unexpected path"}, request=request)

    return handler


class ApprovalFailureMappingTests(unittest.IsolatedAsyncioTestCase):
    """request_guard must keep the failure shape and never fake a denial."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _guard(self, approval: Any, *, budget_clock: _FakeClock | None = None):
        handler = _supervisor(approval)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ToolContext(
                supervisor_url="http://supervisor.test",
                session_id="sess-1",
                run_id="run-1",
                worker_id="worker-1",
                workspace_root=self.root,
                http=http,
                registry=None,
                task_id="task-1",
                approval_poll_interval_sec=0.001,
            )
            with patch.object(context_module, "_monotonic", budget_clock or _FakeClock(step=10.0)):
                op_res, err = await request_guard(ctx, "write_file", {"path": "out.txt"})
            return ctx, op_res, err

    async def test_supervisor_outage_is_approval_unavailable_and_never_authorizes(self) -> None:
        ctx, op_res, err = await self._guard((503, {"error": "unavailable"}))

        self.assertEqual(op_res.get("safety_level"), "approval_required")
        self.assertIsNotNone(err)
        assert err is not None
        self.assertFalse(err.get("ok", False))
        self.assertEqual(err["approval_status"], "approval_unavailable")
        self.assertFalse(err["denied"], "an outage is not a human denial")
        self.assertNotEqual(err["error"], "user denied approval")
        self.assertIn("unavailable", err["error"])
        self.assertEqual(err["approval_id"], "ap-1")
        # [Critical] An unanswered approval must not leave an approved fingerprint
        # behind: nothing downstream may treat "could not check" as authorised.
        self.assertFalse(getattr(ctx, "_approved_guard_fps", set()))

    async def test_lost_approval_is_reported_as_approval_lost(self) -> None:
        ctx, _op_res, err = await self._guard((404, {"error": "no such approval"}))

        assert err is not None
        self.assertEqual(err["approval_status"], "approval_lost")
        self.assertFalse(err["denied"])
        self.assertIn("no longer exists", err["error"])
        self.assertIn("HTTP 404", err["error"])
        self.assertFalse(getattr(ctx, "_approved_guard_fps", set()))

    async def test_other_4xx_is_an_explicit_error_not_a_bare_exception(self) -> None:
        ctx, _op_res, err = await self._guard((403, {"error": "forbidden"}))

        assert err is not None
        self.assertEqual(err["approval_status"], "approval_error")
        self.assertFalse(err["denied"])
        self.assertIn("HTTP 403", err["error"])
        self.assertFalse(getattr(ctx, "_approved_guard_fps", set()))

    async def test_real_denial_keeps_its_meaning(self) -> None:
        ctx, _op_res, err = await self._guard((200, {"status": "denied"}))

        assert err is not None
        self.assertEqual(err["error"], "user denied approval")
        self.assertTrue(err["denied"])
        self.assertNotIn("approval_status", err)
        self.assertFalse(getattr(ctx, "_approved_guard_fps", set()))

    async def test_cancellation_during_the_wait_still_wins(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/ops/request":
                return httpx.Response(
                    200,
                    json={"safety_level": "approval_required", "approval_id": "ap-1"},
                    request=request,
                )
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": True}, request=request)
            return httpx.Response(200, json={"status": "pending"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ToolContext(
                supervisor_url="http://supervisor.test", session_id="sess-1",
                run_id="run-1", worker_id="worker-1", workspace_root=self.root,
                http=http, registry=None, task_id="task-1",
                approval_poll_interval_sec=0.001,
            )
            _op_res, err = await request_guard(ctx, "write_file", {"path": "out.txt"})

        assert err is not None
        self.assertTrue(err["cancelled"])
        self.assertEqual(err["error"], "task cancelled")
        self.assertNotIn("denied", err)


class RegistryApprovalFailureTests(unittest.IsolatedAsyncioTestCase):
    """registry.authorize() passthrough + no execution on an undelivered approval."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = ToolRegistry(workspace_root=self.root, tools_dir=self.root / "tools")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _execute(self, approval: Any, tmp_workspace: Path) -> dict[str, Any]:
        handler = _supervisor(approval)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ToolContext(
                supervisor_url="http://supervisor.test",
                session_id="sess-1",
                run_id="run-1",
                worker_id="worker-1",
                workspace_root=tmp_workspace,
                http=http,
                registry=self.registry,
                task_id="task-1",
                approval_poll_interval_sec=0.001,
            )
            with patch.object(context_module, "_monotonic", _FakeClock(step=10.0)):
                return await self.registry.execute(
                    name="write_file",
                    arguments={"path": "out.txt", "content": "written\n"},
                    ctx=ctx,
                )

    async def test_authorize_passes_approval_lost_through_with_denied_false(self) -> None:
        handler = _supervisor((404, {"error": "no such approval"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ToolContext(
                supervisor_url="http://supervisor.test", session_id="sess-1",
                run_id="run-1", worker_id="worker-1", workspace_root=self.root,
                http=http, registry=self.registry, task_id="task-1",
                approval_poll_interval_sec=0.001,
            )
            denied = await self.registry.authorize(
                name="write_file", arguments={"path": "out.txt", "content": "x"}, ctx=ctx,
            )

        self.assertIsNotNone(denied)
        assert denied is not None
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["approval_status"], "approval_lost")
        self.assertFalse(denied["denied"])
        self.assertFalse(denied["cancelled"])
        self.assertNotIn("user denied approval", denied["error"])

    async def test_outage_never_writes_and_is_not_reported_as_a_denial(self) -> None:
        workspace = self.root / "ws-outage"
        workspace.mkdir()
        res = await self._execute((503, {"error": "unavailable"}), workspace)

        self.assertFalse(res["ok"], res)
        self.assertIn("unavailable", res["error"])
        self.assertNotIn("user denied approval", res["error"])
        self.assertFalse((workspace / "out.txt").exists(), "an undelivered approval must not write")

    async def test_real_denial_still_writes_nothing_and_says_denied(self) -> None:
        workspace = self.root / "ws-denied"
        workspace.mkdir()
        res = await self._execute((200, {"status": "denied"}), workspace)

        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"], "user denied approval")
        self.assertFalse((workspace / "out.txt").exists())

    async def test_granted_approval_still_writes_exactly_once(self) -> None:
        workspace = self.root / "ws-allowed"
        workspace.mkdir()
        res = await self._execute((200, {"status": "allowed"}), workspace)

        self.assertTrue(res["ok"], res)
        self.assertEqual((workspace / "out.txt").read_text(encoding="utf-8"), "written\n")
        self.assertEqual(res["data"]["bytes"], len("written\n".encode("utf-8")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
