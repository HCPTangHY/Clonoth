"""Offline run_ai_node startup checks with the real MCP discovery waiter."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from engine.builtin import mcp
from engine.hooks.registry import HookRegistry
from engine.inference import ai_step
from engine.node import Node, ToolAccess
from toolbox.registry import ToolRegistry


class CapturedLoop(Exception):
    """Stop after the real tool builder, before any provider request."""


@pytest.mark.parametrize("mode", ["fresh", "legacy", "fork"])
def test_initial_discovery_precedes_tool_snapshot(tmp_path, monkeypatch, mode):
    asyncio.run(_exercise(tmp_path, monkeypatch, mode=mode))


@pytest.mark.parametrize("outcome", ["failed", "timeout", "cancelled"])
def test_discovery_exit_paths(tmp_path, monkeypatch, outcome):
    asyncio.run(_exercise(tmp_path, monkeypatch, outcome=outcome))


async def _exercise(tmp_path, monkeypatch, *, mode="fresh", outcome="success"):
    state = {"generation": 0, "task": None, "stop": None}
    gate = asyncio.Event()
    entered = asyncio.Event()
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    hooks = HookRegistry()
    hooks.afire = AsyncMock(return_value=SimpleNamespace(action=None))
    monkeypatch.setattr(ai_step, "hook_registry", hooks)
    monkeypatch.setattr(mcp, "_bridge_state", lambda: state)
    monkeypatch.setattr(mcp, "_drop_bridge_ownership", AsyncMock())
    monkeypatch.setattr(mcp, "_config_mtime", lambda _: None)

    async def discover(*args, **kwargs):
        entered.set()
        await gate.wait()
        if outcome == "failed":
            raise ValueError("offline discovery failure")
        registry.register_builtin_tool(
            "mcp_probe_read", "Offline discovered tool",
            {"type": "object", "properties": {}}, AsyncMock(return_value={}),
        )

    monkeypatch.setattr(mcp, "refresh_mcp_tools", discover)
    monkeypatch.setattr(
        ai_step, "auto_discover_and_register",
        lambda *args, **kwargs: mcp._start_engine_bridge(registry, SimpleNamespace(hooks=None)),
    )
    monkeypatch.setattr(ai_step, "load_external_plugins", Mock())
    monkeypatch.setattr(ai_step, "load_runtime_config", lambda _: {})
    assemble = Mock(return_value=([{"role": "system", "content": "SYS"}], False, []))
    monkeypatch.setattr(ai_step, "assemble_initial_messages", assemble)
    resume = AsyncMock()
    monkeypatch.setattr(ai_step, "apply_resume_messages", resume)
    legacy_messages = [{"role": "user", "content": "retained legacy message"}]
    monkeypatch.setattr(ai_step, "load_context_snapshot", lambda *args: {"messages": legacy_messages})
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        raise CapturedLoop()

    monkeypatch.setattr(ai_step, "_build_loop_state", capture)
    real_wait = mcp.wait_mcp_initial_discovery
    if outcome == "timeout":
        # Shorten only the test deadline; exercise the actual shielding/wait logic.
        async def short_wait(*, registry):
            return await real_wait(timeout=.02, registry=registry)
        monkeypatch.setattr(mcp, "wait_mcp_initial_discovery", short_wait)

    inherited = [{"type": "function", "function": {
        "name": "parent_only", "description": "unchanged parent snapshot",
        "parameters": {"type": "object", "properties": {}},
    }}]
    task_context = {}
    if mode == "fork":
        snapshot_path = tmp_path / "fork.json"
        snapshot_path.write_text(json.dumps({"messages": legacy_messages, "openai_tools": inherited}))
        task_context = {"dispatch_origin": {"fork_snapshot_path": "fork.json"}}
    rctx = SimpleNamespace(workspace_root=tmp_path, task_context=task_context,
                           session_id="offline", workspace_name="")
    node = Node(id="probe", type="ai", tool_mode="native", tool_access=ToolAccess(mode="all"))
    task = asyncio.create_task(ai_step.run_ai_node(
        rctx=rctx, provider=SimpleNamespace(name="offline"), registry=registry,
        node=node, instruction="probe", history=[],
        context_ref="legacy" if mode == "legacy" else "",
        resume_data={"type": "tool_results"} if mode == "legacy" else None,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not captured, "Tool list was frozen before initial discovery"
        assert not task.done()
        ready = state["initial_discovery"][2]
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not ready.cancelled()
            assert not state["task"].done()
            assert not captured
            gate.set()
            assert await real_wait(timeout=1, registry=registry)
            assert registry.get_spec("mcp_probe_read") is not None
            return
        if outcome != "timeout":
            gate.set()
        with pytest.raises(CapturedLoop):
            await asyncio.wait_for(task, 1)
        names = {tool["function"]["name"] for tool in captured["openai_tools"]}
        if outcome == "success":
            assert "mcp_probe_read" in captured["allowed_real_tools"]
            if mode == "fork":
                assert captured["openai_tools"] == inherited
                assert names == {"parent_only"}
            else:
                assert "mcp_probe_read" in names
        else:
            assert "mcp_probe_read" not in names
        if mode == "legacy":
            assemble.assert_not_called()
            resume.assert_awaited_once()
            assert captured["messages"] == legacy_messages
        if outcome == "timeout":
            assert not ready.cancelled()
            assert not state["task"].done()
            gate.set()
            assert await real_wait(timeout=1, registry=registry)
        if outcome == "failed":
            assert state["retry_pending"]
    finally:
        gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await mcp.shutdown_bridge()
