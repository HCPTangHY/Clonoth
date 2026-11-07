"""Reserved plugin names must be rejected before external tool writes."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from engine.builtin.agent_manage import PLUGIN_META
from toolbox.builtins import RESERVED_TOOL_NAMES
from toolbox.builtins.tool_manage import manage_tool
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry


def test_create_agent_is_a_declared_builtin_and_reserved():
    declaration = next(item for item in PLUGIN_META["tools"] if item["name"] == "create_agent")
    assert callable(declaration["func"])
    assert "create_agent" in RESERVED_TOOL_NAMES


def test_create_agent_is_rejected_before_write_or_reload(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    target = tools / "create_agent.py"
    registry = SimpleNamespace(tools_dir=tools)
    ctx = SimpleNamespace(registry=registry)
    for existing in (False, True):
        if existing:
            target.write_bytes(b"# existing local script\n")
        with patch("toolbox.builtins.write_file.write_file", new_callable=AsyncMock) as write:
            result = asyncio.run(manage_tool({
                "action": "upsert", "name": "create_agent", "script": "output('replacement')",
            }, ctx))
        assert result["ok"] is False
        assert result["error"] == "reserved tool name: create_agent"
        write.assert_not_awaited()
        if existing:
            assert target.read_bytes() == b"# existing local script\n"
        else:
            assert not target.exists()


def test_reserved_script_is_not_loaded_without_plugin(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    target = tools / "create_agent.py"
    original = b"SPEC = {'name': 'create_agent', 'description': 'shadow', 'input_schema': {}}\n"
    target.write_bytes(original)
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools)
    assert registry.get_spec("create_agent") is None
    assert target.read_bytes() == original


def test_valid_tool_still_writes_and_registers(tmp_path: Path):
    async def run():
        def respond(request):
            if request.url.path == "/v1/ops/request":
                return httpx.Response(200, json={"safety_level": "auto"})
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False})
            raise AssertionError(f"unexpected request: {request.url.path}")

        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            ctx = ToolContext(
                supervisor_url="http://supervisor.test", session_id="s", run_id="r",
                worker_id="w", workspace_root=tmp_path, http=http, registry=registry,
            )
            result = await manage_tool({
                "action": "upsert", "name": "valid_local_tool", "description": "valid",
                "script": "output('ok')",
            }, ctx)
        assert result["ok"] is True, result
        assert (tmp_path / "tools" / "valid_local_tool.py").is_file()
        assert registry.get_spec("valid_local_tool")["description"] == "valid"

    asyncio.run(run())
