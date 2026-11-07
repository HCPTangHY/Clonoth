"""MCP plugin migration checks (offline, no real MCP server, no network).

Covers exactly the surfaces migrated together:

* ``engine/builtin/mcp/mcp_runtime.py`` — config validation (unknown transport
  is an error, never stdio), model-facing redaction, stdio session reuse, idle
  TTL, schema cache, cancellation/timeout handling, "never replay a business
  call", HTTP client ownership.
* ``engine/builtin/mcp/__init__.py`` — single ``manage_mcp_client`` entry, one
  guarded write per mutation (absolute path), annotation-only ``pure``, schema
  pattern repair, bridge start/teardown/reload hand-off, admin endpoints.
* ``engine/faces/routes.py`` — resource-keyed de-duplication while same-owner
  same-prefix routers still coexist.
* ``engine/hooks/registry.py`` — keyed disposer archival and forgetting.

Run: python3.11 -B tests/test_engine_mcp_migration.py

Every MCP "server" below is an in-process fake injected over the module globals;
``stdio_client`` never spawns anything, no socket is opened, and every config
file lives in a tempdir. Secret strings are fabricated and only asserted absent
from model-facing output.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import yaml
from fastapi import APIRouter, FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.builtin.mcp as mcp_module  # noqa: E402
from engine.builtin.mcp import mcp_runtime  # noqa: E402
from engine.faces.routes import PluginRoutesFace, _iter_http_routes  # noqa: E402
from engine.hooks.registry import HookRegistry  # noqa: E402
from engine.registry_core import DisposalLedger  # noqa: E402


# ---------------------------------------------------------------------------
#  fixtures
# ---------------------------------------------------------------------------

LEGACY_MCP_TOOL_NAMES = (
    "create_or_update_mcp_client",
    "list_mcp_clients",
    "delete_mcp_client",
)

_ENV_SECRET = "ENV-SECRET-VALUE"
_HEADER_SECRET = "HEADER-SECRET-VALUE"
_ARG_SECRET = "ARG-SECRET-VALUE"
_URL_SECRET = "QUERY-SECRET-VALUE"
_URL_PASSWORD = "URL-PASSWORD-VALUE"
_ALL_SECRETS = (_ENV_SECRET, _HEADER_SECRET, _ARG_SECRET, _URL_SECRET, _URL_PASSWORD)


def write_config(root: Path, clients: dict) -> Path:
    path = root / "data" / "mcp_clients.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "clients": clients}, sort_keys=False), encoding="utf-8")
    return path


def read_config(root: Path) -> dict:
    return yaml.safe_load((root / "data" / "mcp_clients.yaml").read_text(encoding="utf-8")) or {}


def secret_config() -> dict:
    return {
        "local": {
            "transport": "stdio",
            "enabled": True,
            "command": "/opt/secret/bin/npx",
            "args": ["--token", _ARG_SECRET, "pkg"],
            "env": {"API_KEY": _ENV_SECRET},
        },
        "remote": {
            "transport": "streamable_http",
            "enabled": True,
            "url": f"https://user:{_URL_PASSWORD}@mcp.example.com/mcp?api_key={_URL_SECRET}",
            "headers": {"Authorization": f"Bearer {_HEADER_SECRET}"},
        },
    }


class _Server:
    """In-process stand-in for an MCP server (no process, no socket)."""

    def __init__(self) -> None:
        self.process_starts = 0
        self.process_exits = 0
        self.sessions = 0
        self.session_exits = 0
        self.initializes = 0
        self.list_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.tools: list[dict] = [
            {"name": "echo", "description": "Echo", "inputSchema": {"type": "object", "properties": {}}}
        ]
        self.call_exception: BaseException | None = None
        self.call_delay = 0.0
        self.list_delay = 0.0

    @property
    def live_processes(self) -> int:
        return self.process_starts - self.process_exits


class _Params:
    def __init__(self, *, command: str, args: list[str], env: dict) -> None:
        self.command = command
        self.args = args
        self.env = env


class _FakeStdio:
    def __init__(self, server: _Server) -> None:
        self.server = server
        self.params: _Params | None = None

    def __call__(self, params: _Params) -> "_FakeStdio":
        self.params = params
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
            self.read_stream = read_stream
            self.write_stream = write_stream

        async def __aenter__(self):
            server.sessions += 1
            return self

        async def __aexit__(self, *exc_info) -> bool:
            server.session_exits += 1
            return False

        async def initialize(self) -> None:
            server.initializes += 1

        async def list_tools(self) -> dict:
            server.list_calls += 1
            if server.list_delay:
                await asyncio.sleep(server.list_delay)
            return {"tools": [dict(tool) for tool in server.tools]}

        async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
            server.calls.append((name, dict(arguments or {})))
            if server.call_delay:
                await asyncio.sleep(server.call_delay)
            if server.call_exception is not None:
                exc = server.call_exception
                server.call_exception = None
                raise exc
            return {"content": [{"type": "text", "text": f"{name} ok"}], "isError": False}

    return _Session


@contextlib.contextmanager
def fake_sdk(server: _Server):
    """Patch the MCP SDK surface with in-process fakes."""
    with mock.patch.multiple(
        mcp_runtime,
        _MCP_IMPORT_ERROR=None,
        StdioServerParameters=_Params,
        stdio_client=_FakeStdio(server),
        ClientSession=_session_class(server),
    ):
        yield


class _GuardCtx:
    """Minimal ToolContext for request_guard: records every policy request."""

    def __init__(self, root: Path, *, safety_level: str = "auto", approval: str = "allowed") -> None:
        self.workspace_root = Path(root)
        self.safety_level = safety_level
        self.approval = approval
        self.requests: list[tuple[str, dict]] = []
        self.registry = None
        self.http = None
        self.supervisor_url = ""
        self.workspace = None
        self.task_id = ""
        self.session_id = "session"
        self.parent_session_id = ""

    async def request_op(self, op: str, parameters: dict) -> dict:
        self.requests.append((op, dict(parameters)))
        response = {"op": op, "safety_level": self.safety_level}
        if self.safety_level == "approval_required":
            response["approval_id"] = "approval-1"
        return response

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict:
        return {"status": self.approval}

    async def check_cancelled(self) -> bool:
        return False


class _ToolCallCtx:
    """Minimal context for a registered MCP tool closure."""

    async def check_cancelled(self) -> bool:
        return False


class _FakeToolRegistry:
    def __init__(self, root: Path) -> None:
        self.workspace_root = Path(root)
        self.tools: dict[str, dict] = {}

    def register_builtin_tool(self, name, description, input_schema, func, *, pure: bool = False):
        entry = {
            "description": description,
            "input_schema": input_schema,
            "func": func,
            "pure": bool(pure),
        }
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


class _PipeGone(ConnectionError):
    """Stands in for anyio's BrokenResourceError (a transport failure)."""


async def wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _request(root: Path):
    state = SimpleNamespace(workspace_root=root)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(state=state)))


# ---------------------------------------------------------------------------
#  Public surface: one entry, no re-registered legacy CRUD
# ---------------------------------------------------------------------------


class PublicSurfaceTests(unittest.TestCase):
    def test_plugin_meta_declares_only_the_unified_entry(self) -> None:
        tools = mcp_module.PLUGIN_META["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["manage_mcp_client"])
        self.assertIs(tools[0]["func"], mcp_module.manage_mcp_client)
        self.assertEqual(tools[0]["input_schema"]["required"], ["action"])
        self.assertEqual(
            tools[0]["input_schema"]["properties"]["action"]["enum"], ["list", "upsert", "delete"]
        )

    def test_legacy_crud_tool_names_are_gone_from_the_module(self) -> None:
        for name in LEGACY_MCP_TOOL_NAMES:
            self.assertFalse(hasattr(mcp_module, name), name)

    def test_transport_enum_advertises_aliases_only(self) -> None:
        enum = mcp_module.PLUGIN_META["tools"][0]["input_schema"]["properties"]["transport"]["enum"]
        self.assertIn("streamable-http", enum)
        self.assertIn("streamable_http", enum)
        self.assertNotIn("websocket", enum)

    def test_no_private_client_allow_list_in_the_runtime(self) -> None:
        # The migration must not decide behaviour by hard-coded client names.
        self.assertFalse(hasattr(mcp_runtime, "_DEFAULT_STATEFUL_CLIENTS"))
        source = Path(mcp_runtime.__file__).read_text(encoding="utf-8")
        for private_name in ("reverse-proxy", "chrome-devtools", "gelbooru", "danbooru", "context7"):
            self.assertNotIn(f'"{private_name}"', source)


# ---------------------------------------------------------------------------
#  Config validation
# ---------------------------------------------------------------------------


class ConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)

    def test_unknown_transport_is_rejected_instead_of_becoming_stdio(self) -> None:
        for bad in ("websocket", "sse2", "grpc", "STDIO/2"):
            with self.assertRaises(ValueError) as ctx:
                mcp_runtime.validate_client_spec(
                    {"id": "x", "transport": bad, "command": "npx"}
                )
            self.assertIn("unknown transport", str(ctx.exception))
        with self.assertRaises(ValueError):
            mcp_runtime.upsert_client(self.root, {"id": "x", "transport": "websocket", "command": "npx"})
        self.assertFalse(
            (self.root / "data" / "mcp_clients.yaml").exists(),
            "a rejected transport must not create the config file",
        )

    def test_transport_is_required_for_upsert(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            mcp_runtime.validate_client_spec({"id": "x", "command": "npx"})
        self.assertIn("transport is required", str(ctx.exception))

    def test_known_transport_aliases_normalize(self) -> None:
        self.assertEqual(mcp_runtime.normalize_transport("streamable-http"), "streamable_http")
        self.assertEqual(mcp_runtime.normalize_transport("HTTP"), "streamable_http")
        self.assertEqual(mcp_runtime.normalize_transport("stdio"), "stdio")
        # An entry without a transport key is a legacy stdio record, but only
        # when the caller does not require an explicit value.
        self.assertEqual(mcp_runtime.normalize_transport(None), "stdio")

    def test_field_validation(self) -> None:
        base = {"id": "x", "transport": "stdio", "command": "npx"}
        cases = [
            ({"id": "has space", **{k: v for k, v in base.items() if k != "id"}}, "invalid client id"),
            ({**base, "command": "   "}, "requires a non-empty command"),
            ({**base, "args": "not-a-list"}, "must be a list"),
            ({**base, "env": ["A=1"]}, "must be an object"),
            ({**base, "env": {"A": {"nested": 1}}}, "must be a scalar"),
            ({**base, "enabled": "yes"}, "must be a boolean"),
            ({**base, "idle_ttl_sec": -1}, "positive number"),
            ({**base, "idle_ttl_sec": float("nan")}, "finite"),
            ({**base, "idle_ttl_sec": float("inf")}, "finite"),
            ({"id": "x", "transport": "sse", "url": "  "}, "requires a non-empty url"),
            ({"id": "x", "transport": "sse", "url": "https://h", "stateful": True}, "only apply to stdio"),
        ]
        for spec, needle in cases:
            with self.assertRaises(ValueError, msg=str(spec)) as ctx:
                mcp_runtime.validate_client_spec(spec)
            self.assertIn(needle, str(ctx.exception), str(spec))

    def test_upsert_writes_validated_record_and_list_round_trips(self) -> None:
        spec = mcp_runtime.upsert_client(
            self.root,
            {
                "id": "demo",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "server"],
                "env": {"TOKEN": "abc"},
                "stateful": True,
            },
        )
        self.assertEqual(spec["id"], "demo")
        stored = read_config(self.root)["clients"]["demo"]
        self.assertEqual(stored["env"], {"TOKEN": "abc"})
        self.assertIs(stored["stateful"], True)
        self.assertEqual(mcp_runtime.resolve_idle_ttl(stored), mcp_runtime.STDIO_STATEFUL_IDLE_TTL_SEC)
        self.assertEqual(mcp_runtime.resolve_idle_ttl({}), mcp_runtime.STDIO_IDLE_TTL_SEC)
        self.assertEqual(mcp_runtime.resolve_idle_ttl({"idle_ttl_sec": 42}), 42.0)

    def test_unknown_transport_in_a_hand_edited_config_is_surfaced(self) -> None:
        write_config(self.root, {"broken": {"transport": "websocket", "enabled": True}})
        listing = mcp_runtime.list_clients(self.root)
        self.assertIn("transport_error", listing[0])
        with self.assertRaises(ValueError):
            mcp_runtime.get_client_spec(self.root, "broken")


# ---------------------------------------------------------------------------
#  Redaction and admin endpoints
# ---------------------------------------------------------------------------


class RedactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_config(self.root, secret_config())

    async def test_list_hides_every_secret_shape(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client({"action": "list"}, ctx)
        self.assertTrue(result["ok"])
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in _ALL_SECRETS:
            self.assertNotIn(secret, rendered, secret)
        self.assertNotIn("/opt/secret/bin", rendered)
        self.assertEqual(ctx.requests, [], "list must not ask for approval")

        by_id = {client["id"]: client for client in result["data"]["clients"]}
        local = by_id["local"]
        self.assertEqual(local["command"], "npx")
        self.assertEqual(local["args_count"], 3)
        self.assertNotIn("args", local)
        self.assertEqual(local["env"], {"API_KEY": "***"})
        remote = by_id["remote"]
        self.assertEqual(remote["url"], "https://mcp.example.com/mcp")
        self.assertTrue(remote["url_query_hidden"])
        self.assertTrue(remote["url_userinfo_hidden"])
        self.assertEqual(remote["headers"], {"Authorization": "***"})
        self.assertTrue(all(client.get("redacted") for client in result["data"]["clients"]))

    async def test_admin_endpoints_keep_raw_values_for_authenticated_editing(self) -> None:
        request = _request(self.root)
        listing = mcp_module.list_mcp_clients_endpoint(request)
        blob = json.dumps(listing, ensure_ascii=False)
        self.assertIn(_ENV_SECRET, blob)
        self.assertIn(_HEADER_SECRET, blob)
        raw = mcp_module.get_mcp_clients_raw_endpoint(request)
        self.assertIn(_ENV_SECRET, raw["content"])
        self.assertIn(_URL_SECRET, raw["content"])

    async def test_raw_update_endpoint_still_writes(self) -> None:
        request = _request(self.root)
        payload = mcp_module.RawContent(content="version: 1\nclients:\n  only:\n    transport: stdio\n    command: npx\n")
        self.assertEqual(mcp_module.update_mcp_clients_raw_endpoint(payload, request), {"ok": True})
        self.assertEqual(sorted(read_config(self.root)["clients"]), ["only"])


# ---------------------------------------------------------------------------
#  Approval guarding for every mutation
# ---------------------------------------------------------------------------


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_config(self.root, {"keep": {"transport": "stdio", "enabled": True, "command": "npx"}})

    async def test_upsert_is_guarded_with_an_absolute_path(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client(
            {"action": "upsert", "id": "added", "transport": "stdio", "command": "npx"},
            ctx,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(ctx.requests), 1)
        op, parameters = ctx.requests[0]
        self.assertEqual(op, "write_file")
        self.assertTrue(Path(parameters["path"]).is_absolute())
        self.assertTrue(parameters["path"].endswith("data/mcp_clients.yaml"))
        self.assertEqual(parameters["mcp_client_id"], "added")
        self.assertIn("added", read_config(self.root)["clients"])

    async def test_upsert_answer_is_redacted(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client(
            {
                "action": "upsert",
                "id": "secretive",
                "transport": "stdio",
                "command": "npx",
                "args": ["--token", _ARG_SECRET],
                "env": {"API_KEY": _ENV_SECRET},
            },
            ctx,
        )
        self.assertTrue(result["ok"], result)
        blob = json.dumps(result, ensure_ascii=False)
        for secret in (_ARG_SECRET, _ENV_SECRET):
            self.assertNotIn(secret, blob, secret)
        self.assertEqual(result["data"]["client"]["env"], {"API_KEY": "***"})
        self.assertEqual(result["data"]["client"]["args_count"], 2)
        # The stored record still holds the real value for the runtime to use.
        self.assertEqual(
            read_config(self.root)["clients"]["secretive"]["env"], {"API_KEY": _ENV_SECRET}
        )

    async def test_delete_is_guarded_and_removes_the_entry(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client({"action": "delete", "id": "keep"}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(ctx.requests), 1)
        self.assertEqual(ctx.requests[0][1]["delete_mcp_client"], "keep")
        self.assertTrue(Path(ctx.requests[0][1]["path"]).is_absolute())
        self.assertEqual(read_config(self.root)["clients"], {})

    async def test_denied_approval_leaves_the_file_untouched(self) -> None:
        before = (self.root / "data" / "mcp_clients.yaml").read_text(encoding="utf-8")
        ctx = _GuardCtx(self.root, safety_level="approval_required", approval="denied")
        result = await mcp_module.manage_mcp_client(
            {"action": "upsert", "id": "blocked", "transport": "stdio", "command": "npx"},
            ctx,
        )
        self.assertFalse(result["ok"])
        self.assertIn("denied", result["error"])
        self.assertEqual((self.root / "data" / "mcp_clients.yaml").read_text(encoding="utf-8"), before)

    async def test_approval_service_failures_keep_the_structured_status(self) -> None:
        before = (self.root / "data" / "mcp_clients.yaml").read_bytes()
        for status in ("approval_lost", "approval_unavailable", "approval_error"):
            for action in ("upsert", "delete"):
                with self.subTest(status=status, action=action):
                    ctx = _GuardCtx(self.root, safety_level="approval_required", approval=status)
                    result = await mcp_module.manage_mcp_client(
                        {"action": action, "id": "keep", "transport": "stdio", "command": "npx"}, ctx,
                    )
                    self.assertFalse(result["ok"])
                    self.assertIs(result["denied"], False)
                    self.assertEqual(result["approval_status"], status)
                    self.assertEqual(result["approval_id"], "approval-1")
                    self.assertNotIn("user denied", result["error"])
                    self.assertEqual((self.root / "data" / "mcp_clients.yaml").read_bytes(), before)

    async def test_policy_deny_leaves_the_file_untouched(self) -> None:
        before = (self.root / "data" / "mcp_clients.yaml").read_text(encoding="utf-8")
        ctx = _GuardCtx(self.root, safety_level="deny")
        result = await mcp_module.manage_mcp_client(
            {"action": "delete", "id": "keep"},
            ctx,
        )
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "data" / "mcp_clients.yaml").read_text(encoding="utf-8"), before)

    async def test_invalid_input_is_rejected_before_any_approval_request(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client(
            {"action": "upsert", "id": "bad", "transport": "websocket", "command": "npx"},
            ctx,
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown transport", result["error"])
        self.assertEqual(ctx.requests, [])

    async def test_delete_of_a_missing_client_does_not_ask_for_approval(self) -> None:
        ctx = _GuardCtx(self.root)
        result = await mcp_module.manage_mcp_client({"action": "delete", "id": "ghost"}, ctx)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])
        self.assertEqual(ctx.requests, [])

    async def test_only_three_actions_and_omitted_action_means_list(self) -> None:
        ctx = _GuardCtx(self.root)
        # A missing action is always a listing: it must neither write nor ask
        # for an approval, even when the payload looks like an upsert.
        omitted = await mcp_module.manage_mcp_client(
            {"id": "inferred", "transport": "stdio", "command": "npx"}, ctx
        )
        self.assertTrue(omitted["ok"])
        self.assertNotIn("inferred", read_config(self.root)["clients"])
        self.assertEqual(ctx.requests, [], "a missing action must not trigger a write")
        # Aliases are rejected so a policy denying ':delete' cannot be bypassed
        # by 'remove' (or a create/update spelling).
        for alias in ("create", "update", "remove", "rename"):
            result = await mcp_module.manage_mcp_client({"action": alias, "id": "keep"}, ctx)
            self.assertFalse(result["ok"], alias)
            self.assertIn("invalid action", result["error"], alias)
        self.assertEqual(ctx.requests, [])
        self.assertIn("keep", read_config(self.root)["clients"])


# ---------------------------------------------------------------------------
#  stdio session runtime
# ---------------------------------------------------------------------------


class StdioRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_config(
            self.root,
            {"demo": {"transport": "stdio", "enabled": True, "command": "fake-mcp", "args": ["serve"]}},
        )

    async def asyncTearDown(self) -> None:
        await mcp_runtime.close_stdio_actors()

    async def test_session_is_reused_across_calls(self) -> None:
        server = _Server()
        with fake_sdk(server):
            first = await mcp_runtime.call_tool(self.root, "demo", "echo", {"n": 1})
            second = await mcp_runtime.call_tool(self.root, "demo", "echo", {"n": 2})
            self.assertTrue(first["ok"], first)
            self.assertTrue(second["ok"], second)
            self.assertEqual(server.process_starts, 1)
            self.assertEqual(server.initializes, 1)
            self.assertEqual(len(server.calls), 2)
            self.assertEqual(server.live_processes, 1)

    async def test_schema_is_cached_and_discovery_does_not_keep_a_server_resident(self) -> None:
        server = _Server()
        with fake_sdk(server):
            first = await mcp_runtime.list_tools(self.root, "demo")
            second = await mcp_runtime.list_tools(self.root, "demo")
            self.assertEqual([t["name"] for t in first["tools"]], ["echo"])
            self.assertEqual(second["tools"], first["tools"])
            self.assertEqual(server.list_calls, 1, "second discovery must hit the schema cache")
            self.assertEqual(server.process_starts, 1)
            self.assertEqual(server.live_processes, 0, "discovery alone must not keep a subprocess")

    async def test_idle_ttl_reaps_the_session(self) -> None:
        write_config(
            self.root,
            {
                "demo": {
                    "transport": "stdio",
                    "enabled": True,
                    "command": "fake-mcp",
                    "idle_ttl_sec": 0.15,
                }
            },
        )
        server = _Server()
        with fake_sdk(server):
            result = await mcp_runtime.call_tool(self.root, "demo", "echo", {})
            self.assertTrue(result["ok"], result)
            self.assertEqual(server.live_processes, 1)
            self.assertTrue(
                await wait_until(lambda: server.live_processes == 0, timeout=3.0),
                "idle session was not reaped",
            )
            state = mcp_runtime.shared_state()
            self.assertFalse(state["actors"]["demo"].is_alive())

    async def test_close_interrupts_a_blocked_call_immediately(self) -> None:
        server = _Server()
        server.call_delay = 30.0
        with fake_sdk(server):
            task = asyncio.ensure_future(mcp_runtime.call_tool(self.root, "demo", "blocked", {}))
            self.assertTrue(await wait_until(lambda: bool(server.calls)))
            started = time.monotonic()
            stopped = await mcp_runtime.close_stdio_actors()
            elapsed = time.monotonic() - started
            self.assertEqual(stopped, 1)
            self.assertLess(elapsed, 2.0, "close must interrupt the in-flight RPC")
            # The blocked caller gets one explicit failure quickly instead of
            # waiting for MCP_CALL_TIMEOUT.
            result = await asyncio.wait_for(task, timeout=2.0)
            self.assertFalse(result["ok"])
            self.assertIn("not retried", result["error"])
            self.assertEqual(server.live_processes, 0)

    async def test_close_during_a_list_call_never_restarts_the_actor(self) -> None:
        server = _Server()
        server.list_delay = 5.0
        with fake_sdk(server):
            task = asyncio.ensure_future(mcp_runtime.list_tools(self.root, "demo"))
            self.assertTrue(await wait_until(lambda: server.list_calls == 1))
            self.assertEqual(await mcp_runtime.close_stdio_actors(), 1)
            with self.assertRaises(mcp_runtime.MCPConnectionError):
                await asyncio.wait_for(task, timeout=3.0)
            # The listing retry must not respawn a session that left the pool.
            self.assertEqual(server.process_starts, 1, "a closed actor must not be restarted")
            self.assertEqual(server.live_processes, 0)
            self.assertEqual(mcp_runtime.shared_state()["actors"], {})

    async def test_manual_close_is_final_but_a_new_call_reconnects(self) -> None:
        server = _Server()
        with fake_sdk(server):
            actor = await mcp_runtime._get_stdio_actor(self.root, "demo")
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertTrue(await mcp_runtime.close_stdio_actor("demo"))
            self.assertTrue(actor.retired)
            with self.assertRaises(mcp_runtime.MCPConnectError):
                await actor.request("list", timeout=1.0)
            self.assertEqual(server.process_starts, 1, "a retired actor must not restart")
            # Closing one session does not break the client: the next call gets a
            # fresh pool actor.
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertEqual(server.process_starts, 2)

    async def test_idle_exit_keeps_the_actor_restartable(self) -> None:
        write_config(
            self.root,
            {
                "demo": {
                    "transport": "stdio",
                    "enabled": True,
                    "command": "fake-mcp",
                    "idle_ttl_sec": 0.1,
                }
            },
        )
        server = _Server()
        with fake_sdk(server):
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertTrue(await wait_until(lambda: server.live_processes == 0, timeout=3.0))
            actor = mcp_runtime.shared_state()["actors"]["demo"]
            self.assertFalse(actor.retired, "an idle exit must stay restartable")
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertEqual(server.process_starts, 2, "the next call reconnects")

    async def test_close_stdio_actors_is_idempotent(self) -> None:
        server = _Server()
        with fake_sdk(server):
            await mcp_runtime.call_tool(self.root, "demo", "echo", {})
            self.assertEqual(await mcp_runtime.close_stdio_actors(), 1)
            self.assertEqual(server.live_processes, 0)
            self.assertEqual(await mcp_runtime.close_stdio_actors(), 0)

    async def test_close_single_actor_stops_only_that_client(self) -> None:
        write_config(
            self.root,
            {
                "one": {"transport": "stdio", "enabled": True, "command": "fake-mcp"},
                "two": {"transport": "stdio", "enabled": True, "command": "fake-mcp"},
            },
        )
        server = _Server()
        with fake_sdk(server):
            await mcp_runtime.call_tool(self.root, "one", "echo", {})
            await mcp_runtime.call_tool(self.root, "two", "echo", {})
            self.assertEqual(server.live_processes, 2)
            self.assertTrue(await mcp_runtime.close_stdio_actor("one"))
            self.assertFalse(await mcp_runtime.close_stdio_actor("one"))
            self.assertEqual(server.live_processes, 1)
            self.assertIn("two", mcp_runtime.shared_state()["actors"])

    async def test_connection_failure_is_not_replayed(self) -> None:
        server = _Server()
        with fake_sdk(server):
            server.call_exception = _PipeGone("pipe gone")
            failed = await mcp_runtime.call_tool(self.root, "demo", "charge_card", {"amount": 10})
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["data"]["reason"], "connection_lost")
            self.assertIn("not retried", failed["error"])
            self.assertTrue(failed["data"]["result"].startswith("ERROR:"))
            self.assertEqual(
                server.calls,
                [("charge_card", {"amount": 10})],
                "a business call must be attempted exactly once",
            )
            # The next call may reconnect; that is a new call, not a replay.
            retried = await mcp_runtime.call_tool(self.root, "demo", "echo", {})
            self.assertTrue(retried["ok"], retried)
            self.assertEqual(server.process_starts, 2)
            self.assertEqual(len(server.calls), 2)

    async def test_business_error_is_reported_once_and_keeps_the_session(self) -> None:
        server = _Server()
        with fake_sdk(server):
            server.call_exception = RuntimeError("server said no")
            failed = await mcp_runtime.call_tool(self.root, "demo", "echo", {})
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["data"]["reason"], "tool_error")
            self.assertIn("server said no", failed["error"])
            self.assertEqual(len(server.calls), 1)
            self.assertTrue((await mcp_runtime.call_tool(self.root, "demo", "echo", {}))["ok"])
            self.assertEqual(server.process_starts, 1, "a business error must not drop the session")

    async def test_timeout_is_a_single_explicit_failure(self) -> None:
        server = _Server()
        server.call_delay = 5.0
        with fake_sdk(server):
            failed = await asyncio.wait_for(
                mcp_runtime.call_tool(self.root, "demo", "slow_tool", {}, timeout=0.05),
                timeout=3.0,
            )
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["data"]["reason"], "timeout")
            self.assertIn("timed out", failed["error"])
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(server.live_processes, 0, "a timed-out session must be dropped")

    async def test_cancellation_drops_the_session_and_leaves_no_dangling_await(self) -> None:
        server = _Server()
        server.call_delay = 5.0
        with fake_sdk(server):
            task = asyncio.ensure_future(mcp_runtime.call_tool(self.root, "demo", "slow_tool", {}))
            self.assertTrue(await wait_until(lambda: bool(server.calls)))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3.0)
            self.assertTrue(await wait_until(lambda: server.live_processes == 0, timeout=3.0))
            self.assertFalse(mcp_runtime.shared_state()["actors"]["demo"].is_alive())

    async def test_tool_wrapper_reports_cancellation_from_ctx(self) -> None:
        server = _Server()
        server.call_delay = 5.0

        class _CancellingCtx:
            def __init__(self) -> None:
                self.calls = 0

            async def check_cancelled(self) -> bool:
                self.calls += 1
                return self.calls >= 1

        with fake_sdk(server):
            func = mcp_module._make_mcp_tool(self.root, "demo", "slow_tool")
            result = await func({}, _CancellingCtx())
            self.assertFalse(result["ok"])
            self.assertTrue(result.get("cancelled"))
            self.assertTrue(await wait_until(lambda: server.live_processes == 0, timeout=3.0))

    async def test_streamable_http_client_is_owned_by_the_session(self) -> None:
        write_config(
            self.root,
            {
                "remote": {
                    "transport": "streamable_http",
                    "enabled": True,
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "Bearer T"},
                }
            },
        )
        server = _Server()
        created: list = []

        class _Client:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.closed = False
                created.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info) -> bool:
                self.closed = True
                return False

        class _Transport:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            def __call__(self, url, **kwargs):
                self.kwargs = {"url": url, **kwargs}
                return self

            async def __aenter__(self):
                return (object(), object(), None)

            async def __aexit__(self, *exc_info) -> bool:
                return False

        transport = _Transport()
        with mock.patch.multiple(
            mcp_runtime,
            _MCP_IMPORT_ERROR=None,
            ClientSession=_session_class(server),
            streamable_http_client=transport,
        ), mock.patch.object(httpx, "AsyncClient", _Client):
            async with mcp_runtime.open_session(self.root, "remote") as session:
                self.assertIsNotNone(session)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].closed, "the supplied http client must be closed")
            self.assertIs(transport.kwargs["http_client"], created[0])
            self.assertEqual(transport.kwargs["url"], "https://mcp.example.com/mcp")


# ---------------------------------------------------------------------------
#  Tool registration: pure, schema patterns, bridge lifecycle
# ---------------------------------------------------------------------------


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_config(
            self.root,
            {"demo": {"transport": "stdio", "enabled": True, "command": "fake-mcp"}},
        )

    async def asyncTearDown(self) -> None:
        await mcp_module.shutdown_bridge()

    async def test_pure_comes_only_from_annotations(self) -> None:
        server = _Server()
        server.tools = [
            {"name": "search_notes", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
            {
                "name": "prune_notes",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True, "destructiveHint": True},
            },
            {"name": "get_thing", "inputSchema": {"type": "object"}},
            {
                "name": "run_query",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            },
        ]
        registry = _FakeToolRegistry(self.root)
        with fake_sdk(server):
            count = await mcp_module.refresh_mcp_tools(registry)
        self.assertEqual(count, 4)
        self.assertTrue(registry.tools["mcp_demo_search_notes"]["pure"])
        self.assertFalse(registry.tools["mcp_demo_prune_notes"]["pure"], "destructiveHint vetoes pure")
        self.assertFalse(registry.tools["mcp_demo_get_thing"]["pure"], "no name guessing")
        self.assertTrue(registry.tools["mcp_demo_run_query"]["pure"])
        # async_mode is a separate flag and is never set by this plugin.
        for entry in registry.tools.values():
            self.assertNotIn("async_mode", entry)

    async def test_refresh_keeps_the_ledger_keyed_and_drops_removed_clients(self) -> None:
        server = _Server()
        server.tools = [
            {"name": "one", "inputSchema": {"type": "object"}},
            {"name": "two", "inputSchema": {"type": "object"}},
        ]
        registry = _FakeToolRegistry(self.root)
        hooks = HookRegistry()
        with fake_sdk(server):
            state = mcp_runtime.shared_state()
            state["hooks"] = hooks
            await mcp_module.refresh_mcp_tools(registry)
            await mcp_module.refresh_mcp_tools(registry)
        self.assertEqual(sorted(registry.tools), ["mcp_demo_one", "mcp_demo_two"])
        self.assertEqual(hooks.owned("mcp"), 2, "refreshes must not accumulate ledger entries")

        write_config(
            self.root,
            {"demo": {"transport": "stdio", "enabled": True, "command": "fake-mcp"}},
        )
        write_config(self.root, {})
        with fake_sdk(server):
            mcp_runtime.shared_state()["hooks"] = hooks
            await mcp_module.refresh_mcp_tools(registry)
        self.assertEqual(registry.tools, {})
        self.assertEqual(hooks.owned("mcp"), 0)

    async def test_disabled_client_is_not_registered(self) -> None:
        write_config(
            self.root,
            {"demo": {"transport": "stdio", "enabled": False, "command": "fake-mcp"}},
        )
        server = _Server()
        registry = _FakeToolRegistry(self.root)
        with fake_sdk(server):
            self.assertEqual(await mcp_module.refresh_mcp_tools(registry), 0)
        self.assertEqual(server.process_starts, 0)

    def test_schema_patterns_repair_only_provable_defects(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                # Valid ECMA-262, unsupported by Python's re: must be preserved.
                "unicode_class": {"type": "string", "pattern": r"\p{L}+"},
                "named_group": {"type": "string", "pattern": "(?<name>x)"},
                # Markdown escaping leaked into a machine field: identical match.
                "escaped": {"type": "string", "pattern": r"^agent\_run\_$"},
                # Structurally impossible in ECMA-262 as well.
                "unbalanced": {"type": "string", "pattern": "[unclosed"},
                "nested": {
                    "type": "object",
                    "properties": {"deep": {"type": "string", "pattern": r"a\+b"}},
                },
            },
        }
        mcp_module._sanitize_schema_patterns(schema)
        props = schema["properties"]
        self.assertEqual(props["unicode_class"]["pattern"], r"\p{L}+")
        self.assertEqual(props["named_group"]["pattern"], "(?<name>x)")
        self.assertEqual(props["escaped"]["pattern"], "^agent_run_$")
        self.assertNotIn("pattern", props["unbalanced"])
        self.assertEqual(props["nested"]["properties"]["deep"]["pattern"], r"a\+b")

    async def test_teardown_closes_bridge_and_sessions(self) -> None:
        server = _Server()
        registry = _FakeToolRegistry(self.root)
        hooks = HookRegistry()
        with fake_sdk(server):
            plugin = mcp_module.McpPlugin(_EngineCtx(registry, hooks))
            self.assertTrue(await wait_until(lambda: bool(registry.tools)))
            func = registry.tools["mcp_demo_echo"]["func"]
            self.assertTrue((await func({}, _ToolCallCtx()))["ok"])
            self.assertEqual(server.live_processes, 1)

            task = plugin.teardown()
            self.assertIsNotNone(task)
            await asyncio.wait_for(task, timeout=5.0)
            state = mcp_runtime.shared_state()
            self.assertIsNone(state["task"])
            self.assertEqual(registry.tools, {})
            self.assertEqual(hooks.owned("mcp"), 0)
            self.assertEqual(server.live_processes, 0)

    async def test_bridge_reload_leaks_no_task_tool_or_session(self) -> None:
        server = _Server()
        registry = _FakeToolRegistry(self.root)
        hooks = HookRegistry()
        with fake_sdk(server):
            mcp_module.McpPlugin(_EngineCtx(registry, hooks))
            self.assertTrue(await wait_until(lambda: bool(registry.tools)))
            func = registry.tools["mcp_demo_echo"]["func"]
            self.assertTrue((await func({}, _ToolCallCtx()))["ok"])
            state = mcp_runtime.shared_state()
            first_task = state["task"]
            self.assertIsInstance(first_task, asyncio.Task)

            # Re-execute the plugin package, exactly like load_single_builtin().
            reloaded = importlib.reload(mcp_module)
            second_registry = _FakeToolRegistry(self.root)
            second_hooks = HookRegistry()
            reloaded.McpPlugin(_EngineCtx(second_registry, second_hooks))

            self.assertTrue(await wait_until(lambda: first_task.done()), "old bridge task still alive")
            self.assertTrue(await wait_until(lambda: bool(second_registry.tools)))
            self.assertTrue(await wait_until(lambda: registry.tools == {}), "old tools not disposed")
            self.assertEqual(server.live_processes, 0, "old stdio session was not closed")
            self.assertIsNot(state["task"], first_task)
            self.assertIsNotNone(state["task"])

            # Only one bridge task may exist after any number of reloads.
            live = [task for task in asyncio.all_tasks() if task.get_name() == "mcp-bridge"]
            self.assertEqual(len(live), 1)


# ---------------------------------------------------------------------------
#  Routes face: keyed de-duplication without collapsing routers
# ---------------------------------------------------------------------------


async def _ping() -> dict:
    return {"ok": True}


class RoutesFaceKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.face = PluginRoutesFace()
        self.ledger = DisposalLedger()
        self.face.set_disposal_ledger(self.ledger)
        self.app = FastAPI()

    def _paths(self) -> set[str]:
        # This FastAPI version mounts include_router() as a lazy wrapper, so the
        # mounted paths come from the face's own expansion helper.
        return {
            route.path
            for route in _iter_http_routes(self.app.router)
            if getattr(route, "methods", None)
        }

    def _router(self, path: str) -> APIRouter:
        router = APIRouter()
        router.add_api_route(path, _ping, methods=["GET"])
        return router

    def test_key_dedupes_but_same_owner_prefix_routers_coexist(self) -> None:
        with self.ledger.collecting("mcp"):
            self.face.register(self._router("/a"), mount="admin/config", key="mcp:config")
            # Same owner, same prefix, no key: both must stay mounted.
            self.face.register(self._router("/b"), mount="admin/config")
        self.face.attach(self.app)
        self.assertEqual(
            {path for path in self._paths() if path.startswith("/v1/admin/config")},
            {"/v1/admin/config/a", "/v1/admin/config/b"},
        )

        with self.ledger.collecting("mcp"):
            self.face.register(self._router("/a2"), mount="admin/config", key="mcp:config")
        self.assertEqual(
            {path for path in self._paths() if path.startswith("/v1/admin/config")},
            {"/v1/admin/config/b", "/v1/admin/config/a2"},
            "re-registering a keyed resource must replace, not duplicate",
        )
        self.assertEqual(len(self.face.list_routes(owner="mcp")), 2)
        keyed = [record for record in self.face.list_routes(owner="mcp") if "/a2" in str(record["routes"])]
        self.assertEqual(len(keyed), 1)

    def test_unload_unmounts_every_declaration(self) -> None:
        with self.ledger.collecting("mcp"):
            self.face.register(self._router("/a"), mount="admin/config", key="mcp:config")
            self.face.register(self._router("/b"), mount="admin/config")
        self.face.attach(self.app)
        self.ledger.unload("mcp")
        self.assertEqual(
            {path for path in self._paths() if path.startswith("/v1/admin/config")},
            set(),
        )
        self.assertEqual(self.face.list_routes(), [])

    def test_mcp_admin_and_panel_routers_are_registerable_repeatedly(self) -> None:
        with self.ledger.collecting("mcp"):
            mcp_module._register_admin_routes(self.face)
        self.face.attach(self.app)
        first = {path for path in self._paths() if path.startswith("/v1/admin/config/mcp-clients")}
        self.assertTrue(first)
        with self.ledger.collecting("mcp"):
            mcp_module._register_admin_routes(self.face)
        second = {path for path in self._paths() if path.startswith("/v1/admin/config/mcp-clients")}
        self.assertEqual(first, second)
        self.assertEqual(len(self.face.list_routes(owner="mcp")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
