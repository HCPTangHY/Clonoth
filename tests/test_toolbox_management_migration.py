"""Toolbox management migration regression checks.

Covers exactly the five surfaces migrated together:

* ``toolbox.registry``  — public names, AST-only/sorted loading, pure metadata,
  plugin-keyed disposal, script-subprocess runtime (JSON size, exit codes, env).
* ``toolbox.context``   — ``wait_for_approval`` retry/timeout/lease/cancel.
* ``toolbox.builtins.tool_manage``   — manage_tool upsert/reload + guards.
* ``toolbox.builtins.schedules``     — manage_schedule list/upsert/delete.
* ``toolbox.builtins.tasks``         — get_context_window.

Run: python3.11 -B tests/test_toolbox_management_migration.py

Every registry, schedule file and helper script lives in a tempdir. The only
subprocesses are the local, no-network fixture tools under those tempdirs.
No supervisor, real ``data/`` or provider API is touched; secrets used below
are fabricated strings and are never printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import toolbox.context as context_module  # noqa: E402
import toolbox.registry as registry_module  # noqa: E402
from engine.registry_core import DisposalLedger  # noqa: E402
from supervisor.scheduler import load_schedules, save_schedules  # noqa: E402
from toolbox import builtins as builtin_impls  # noqa: E402
from toolbox.builtins import RESERVED_TOOL_NAMES  # noqa: E402
from toolbox.builtins.schedules import manage_schedule  # noqa: E402
from toolbox.builtins.tasks import get_context_window  # noqa: E402
from toolbox.builtins.tool_manage import manage_tool  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Names that existed before the management-tool consolidation. None of them may
# survive as a registry entry, an alias, or an execution path.
LEGACY_TOOL_NAMES = (
    "create_or_update_tool",
    "reload_tools",
    "create_schedule",
    "list_schedules",
    "delete_schedule",
    "cancel_active_tasks",
    "cancel",
)


# ---------------------------------------------------------------------------
#  Fixture helpers
# ---------------------------------------------------------------------------

def _tool_source(spec: dict, body: list[str] | tuple[str, ...] = (), *, timeout: float | None = None) -> str:
    """Render a local external-tool fixture (SPEC is a literal, as AST requires)."""
    lines = [f"SPEC = {spec!r}", ""]
    if timeout is not None:
        lines += [f"TIMEOUT_SEC = {float(timeout)!r}", ""]
    lines += [
        'if __name__ == "__main__":',
        "    import json, os, sys",
        "    _raw = sys.stdin.read()",
        "    args = json.loads(_raw) if _raw.strip() else {}",
    ]
    lines += [f"    {line}" if line else "" for line in body]
    return "\n".join(lines) + "\n"


def _write_script_tool(
    tools_dir: Path,
    filename: str,
    spec: dict,
    body: list[str] | tuple[str, ...] = (),
    *,
    timeout: float | None = None,
) -> Path:
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / filename
    path.write_text(_tool_source(spec, body, timeout=timeout), encoding="utf-8")
    return path


def _write_raw_tool(tools_dir: Path, filename: str, text: str) -> Path:
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _response(url: str, payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", url))


class _ToolCtx:
    """Minimal context for direct registry executions (no supervisor calls)."""

    def __init__(
        self,
        root: Path,
        *,
        session_id: str = "sess-1",
        task_id: str = "task-1",
        supervisor_url: str = "http://supervisor.test",
    ) -> None:
        self.workspace_root = root
        self.workspace = None
        self.session_id = session_id
        self.task_id = task_id
        self.supervisor_url = supervisor_url

    async def check_cancelled(self) -> bool:
        return False


class _RecordingHttp:
    """Duck-typed httpx client that records kwargs for timeout assertions."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.get_calls.append((url, kwargs))
        if url.endswith("/cancelled"):
            return _response(url, {"cancelled": False})
        return _response(url, {"status": "allowed", "approval_id": "ap-1"})

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.post_calls.append((url, kwargs))
        return _response(url, {"ok": True})


# ---------------------------------------------------------------------------
#  Registry surface: new names only, no legacy alias path
# ---------------------------------------------------------------------------

class RegistrySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = ToolRegistry(workspace_root=self.root, tools_dir=self.root / "tools")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_public_surface_uses_new_names_only(self) -> None:
        names = [spec["name"] for spec in self.registry.list_specs()]
        for expected in ("manage_tool", "manage_schedule", "get_context_window"):
            self.assertIn(expected, names)
        # Worker control moved to an Engine pseudo-tool and must not be a registry tool.
        self.assertNotIn("manage_workers", names)
        for legacy in LEGACY_TOOL_NAMES:
            self.assertNotIn(legacy, names)
        for spec in self.registry.list_specs():
            self.assertFalse(
                {"alias", "aliases"} & set(spec),
                msg=f"spec {spec.get('name')!r} must not carry alias metadata",
            )

    def test_legacy_names_are_not_resolvable_through_any_entry_point(self) -> None:
        ctx = _ToolCtx(self.root)
        for legacy in LEGACY_TOOL_NAMES:
            with self.subTest(tool=legacy):
                self.assertIsNone(self.registry.get_spec(legacy))
                denied = asyncio.run(self.registry.authorize(name=legacy, arguments={}, ctx=ctx))
                self.assertIsInstance(denied, dict)
                self.assertFalse(denied["ok"])
                self.assertIn(f"tool not found: {legacy}", denied["error"])
                for authorized in (True, False):
                    result = asyncio.run(
                        self.registry.execute(
                            name=legacy, arguments={}, ctx=ctx, _authorized=authorized,
                        )
                    )
                    self.assertFalse(result["ok"])
                    self.assertIn(f"tool not found: {legacy}", result["error"])

    def test_management_schemas_expose_action_contract(self) -> None:
        tool_schema = self.registry.get_spec("manage_tool")["input_schema"]
        self.assertEqual(tool_schema["properties"]["action"]["enum"], ["upsert", "reload"])
        self.assertEqual(tool_schema["required"], ["action"])
        self.assertIn("pure", tool_schema["properties"])
        self.assertIn("async_mode", tool_schema["properties"])

        schedule_schema = self.registry.get_spec("manage_schedule")["input_schema"]
        self.assertEqual(
            schedule_schema["properties"]["action"]["enum"], ["list", "upsert", "delete"],
        )
        self.assertEqual(schedule_schema["required"], ["action"])


# ---------------------------------------------------------------------------
#  Loading: read_image fixture, AST-only + sorted order, pure, keyed disposal
# ---------------------------------------------------------------------------

class ToolLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self) -> ToolRegistry:
        registry = ToolRegistry(workspace_root=self.root, tools_dir=self.tools)
        return registry

    def test_reserved_and_retired_names_cannot_return_as_external_scripts(self) -> None:
        for name in RESERVED_TOOL_NAMES:
            _write_script_tool(self.tools, f"{name}.py", {
                "name": name, "description": "external override",
            }, ["print('{}')"])
        _write_script_tool(self.tools, "local_helper.py", {"name": "local_helper"}, ["print('{}')"])
        registry = self._load()
        self.assertIsNotNone(registry.get_spec("local_helper"))
        self.assertFalse(any(s["description"] == "external override" for s in registry.list_specs()))
        for name in ("manage_workers", "preempt_task", "cancel_active_tasks", "save_memory", "create_schedule"):
            self.assertIsNone(registry.get_spec(name), name)

    def test_read_image_fixture_registers_without_being_swallowed_by_read_media(self) -> None:
        _write_script_tool(self.tools, "read_image.py", {
            "name": "read_image",
            "description": "fixture image reader",
            "input_schema": {
                "type": "object",
                "properties": {"image_paths": {"type": "array", "items": {"type": "string"}}},
                "required": [],
            },
        })
        registry = self._load()
        spec = registry.get_spec("read_image")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["description"], "fixture image reader")
        self.assertEqual(list(spec["input_schema"]["properties"]), ["image_paths"])
        # No alias entry may be invented for the media tool.
        self.assertIsNone(registry.get_spec("read_media"))

        # With both fixtures present they stay distinct entries with their own metadata.
        _write_script_tool(self.tools, "read_media.py", {
            "name": "read_media",
            "description": "fixture media reader",
            "input_schema": {"type": "object", "properties": {"paths": {"type": "array"}}, "required": []},
            "pure": True,
        })
        registry.reload()
        image = registry.get_spec("read_image")
        media = registry.get_spec("read_media")
        self.assertEqual(len({id(image), id(media)}), 2)
        self.assertEqual(image["description"], "fixture image reader")
        self.assertEqual(media["description"], "fixture media reader")
        self.assertFalse(image["pure"])
        self.assertTrue(media["pure"])

        # Dropping read_media must not disturb the independent read_image entry.
        (self.tools / "read_media.py").unlink()
        registry.reload()
        self.assertIsNotNone(registry.get_spec("read_image"))
        self.assertIsNone(registry.get_spec("read_media"))

    def test_reload_is_sorted_and_ast_only(self) -> None:
        marker = self.root / "module-body-ran.txt"
        # Filename order differs from declared name order: sorted loading is observable.
        _write_script_tool(self.tools, "zz_last.py", {
            "name": "alpha_tool", "description": "zz file", "input_schema": {},
        })
        _write_script_tool(self.tools, "aa_first.py", {
            "name": "zeta_tool", "description": "aa file", "input_schema": {},
        })
        # AST-only: the module body must never execute (marker stays absent).
        _write_raw_tool(self.tools, "side_effect.py", "\n".join([
            "SPEC = {'name': 'side_effect_tool', 'description': 'ast only', 'input_schema': {}}",
            "from pathlib import Path",
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')",
            "",
        ]))
        # Unparseable / non-literal SPEC files are skipped without raising.
        _write_raw_tool(self.tools, "broken.py", "SPEC = {'name': 'broken'\n")
        _write_raw_tool(self.tools, "computed.py", "SPEC = dict(name='computed_tool')\n")

        registry = ToolRegistry(workspace_root=self.root, tools_dir=self.tools)
        self.assertFalse(marker.exists(), "reload must not execute fixture module bodies")

        original = registry_module._extract_tool_spec
        seen: list[str] = []

        def _spy(path: Path):
            seen.append(path.name)
            return original(path)

        with patch.object(registry_module, "_extract_tool_spec", side_effect=_spy):
            registry.reload()

        self.assertEqual(seen, sorted(seen))
        # Every non-__init__ file is inspected in filename order, including ones
        # whose SPEC is missing/non-literal and therefore skipped at registration.
        self.assertEqual(
            seen, ["aa_first.py", "broken.py", "computed.py", "side_effect.py", "zz_last.py"],
        )
        external = [
            spec["name"] for spec in registry.list_specs()
            if spec["name"] not in registry._builtin_specs
        ]
        # Discovery uses filename order; the public list uses tool-name order
        # so provider prompt prefixes do not depend on registration order.
        self.assertEqual(external, ["alpha_tool", "side_effect_tool", "zeta_tool"])
        for skipped in ("broken", "computed_tool"):
            self.assertIsNone(registry.get_spec(skipped))

    def test_pure_defaults_and_metadata_are_preserved(self) -> None:
        _write_script_tool(self.tools, "plain.py", {
            "name": "plain_tool", "description": "plain", "input_schema": {},
        })
        _write_script_tool(self.tools, "flagged.py", {
            "name": "flagged_tool",
            "description": "flagged",
            "input_schema": {},
            "pure": True,
            "async_mode": True,
            "result_format": "media",
        }, timeout=15)
        _write_script_tool(self.tools, "blank_format.py", {
            "name": "blank_format_tool",
            "description": "blank",
            "input_schema": {},
            "result_format": "  ",
        })

        registry = self._load()
        builtin_specs = {spec["name"]: spec for spec in registry.list_specs() if spec["name"] in registry._builtin_specs}
        for name in ("list_dir", "read_file", "get_context_window"):
            self.assertIs(builtin_specs[name]["pure"], True, msg=name)
        for name in ("write_file", "apply_diff", "execute_command", "grep", "manage_tool",
                     "manage_schedule", "set_workspace", "request_restart"):
            self.assertIs(builtin_specs[name]["pure"], False, msg=name)

        plain = registry.get_spec("plain_tool")
        self.assertIs(plain["pure"], False)
        self.assertIs(plain["async_mode"], False)
        self.assertNotIn("result_format", plain)

        flagged = registry.get_spec("flagged_tool")
        self.assertIs(flagged["pure"], True)
        self.assertIs(flagged["async_mode"], True)
        self.assertEqual(flagged["result_format"], "media")

        blank = registry.get_spec("blank_format_tool")
        self.assertNotIn("result_format", blank)

        # Reload rebuilds from the builtin snapshot: pure metadata must survive.
        _write_script_tool(self.tools, "flagged2.py", {
            "name": "flagged2_tool", "description": "f2", "input_schema": {}, "pure": True,
        })
        registry.reload()
        self.assertIs(registry.get_spec("read_file")["pure"], True)
        self.assertIs(registry.get_spec("flagged2_tool")["pure"], True)
        self.assertIs(registry.get_spec("write_file")["pure"], False)

    def test_plugin_keyed_disposal_survives_reload_and_unload(self) -> None:
        registry = self._load()
        ledger = DisposalLedger()
        registry.set_disposal_ledger(ledger)

        async def first(args, ctx):
            return {"ok": True, "data": {"result": "first"}}

        async def second(args, ctx):
            return {"ok": True, "data": {"result": "second"}}

        with ledger.collecting("demo_plugin"):
            stale_dispose = registry.register_builtin_tool(
                "plugin_demo", "first description", {"type": "object", "properties": {}},
                first, pure=True,
            )
            registry.register_builtin_tool(
                "plugin_demo", "second description", {"type": "object", "properties": {}},
                second, pure=True,
            )

        # Keyed registration replaces the archived disposer instead of stacking.
        self.assertEqual(ledger.counts(), {"demo_plugin": 1})
        registry.reload()
        spec = registry.get_spec("plugin_demo")
        self.assertIsNotNone(spec, "plugin builtin tools must survive reload()")
        self.assertEqual(spec["description"], "second description")
        self.assertIs(spec["pure"], True)

        # A stale disposer must not remove the later same-name replacement.
        stale_dispose()
        self.assertIsNotNone(registry.get_spec("plugin_demo"))
        registry.reload()
        self.assertIsNotNone(registry.get_spec("plugin_demo"))

        result = ledger.unload("demo_plugin")
        self.assertEqual(result["disposed"], 1)
        self.assertEqual(ledger.counts(), {})
        self.assertIsNone(registry.get_spec("plugin_demo"))
        registry.reload()
        self.assertIsNone(registry.get_spec("plugin_demo"), "reload must not resurrect disposed tools")

    def test_external_script_cannot_override_builtin_name(self) -> None:
        for name in ("read_file", "manage_tool", "get_context_window"):
            _write_script_tool(self.tools, f"shadow_{name}.py", {
                "name": name,
                "description": "external shadow",
                "input_schema": {"type": "object", "properties": {"pwn": {"type": "string"}}},
            })
        registry = self._load()
        for name in ("read_file", "manage_tool", "get_context_window"):
            spec = registry.get_spec(name)
            self.assertNotEqual(spec["description"], "external shadow")
            self.assertNotIn("pwn", spec["input_schema"].get("properties", {}))
        self.assertIs(registry._tool_funcs["read_file"], builtin_impls.read_file)


# ---------------------------------------------------------------------------
#  Script tool runtime: JSON size/shape, exit codes, subprocess env
# ---------------------------------------------------------------------------

class ScriptToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir(parents=True)
        self.ctx = _ToolCtx(self.root)
        self.registry: ToolRegistry | None = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self) -> ToolRegistry:
        self.registry = ToolRegistry(workspace_root=self.root, tools_dir=self.tools)
        return self.registry

    async def _execute(self, name: str, arguments: dict | None = None) -> dict:
        assert self.registry is not None
        return await self.registry.execute(
            name=name, arguments=arguments or {}, ctx=self.ctx, _authorized=True,
        )

    async def test_large_structured_json_is_parsed_before_truncation(self) -> None:
        _write_script_tool(self.tools, "big_json.py", {
            "name": "big_json", "description": "big", "input_schema": {},
        }, body=[
            "payload = 'x' * 20000",
            "print(json.dumps({'ok': True, 'data': {'result': 'big', 'payload': payload, 'count': len(payload)}}))",
        ], timeout=30)
        self._load()
        result = await self._execute("big_json")
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["data"]["result"], "big")
        self.assertEqual(result["data"]["count"], 20000)
        self.assertEqual(result["data"]["payload"], "x" * 20000)

    async def test_plain_text_and_json_scalar_are_bounded(self) -> None:
        _write_script_tool(self.tools, "plain_text.py", {
            "name": "plain_text", "description": "plain", "input_schema": {},
        }, body=["print('z' * 20000)"], timeout=30)
        _write_script_tool(self.tools, "scalar_json.py", {
            "name": "scalar_json", "description": "scalar", "input_schema": {},
        }, body=["print(json.dumps('y' * 20000))"], timeout=30)
        self._load()

        plain = await self._execute("plain_text")
        self.assertTrue(plain["ok"])
        self.assertEqual(len(plain["data"]["result"]), 16000)
        self.assertEqual(plain["data"]["result"], plain["data"]["output"])
        self.assertEqual(plain["data"]["result"], "z" * 16000)

        scalar = await self._execute("scalar_json")
        self.assertTrue(scalar["ok"])
        self.assertEqual(len(scalar["data"]["result"]), 16000)
        self.assertEqual(scalar["data"]["value"], "y" * 20000)

    async def test_nonzero_exit_preserves_structured_error_payload(self) -> None:
        _write_script_tool(self.tools, "error_json.py", {
            "name": "error_json", "description": "err", "input_schema": {},
        }, body=[
            "print(json.dumps({'ok': False, 'error': 'boom', 'data': {'detail': 'kept'}}))",
            "sys.exit(7)",
        ], timeout=30)
        self._load()
        result = await self._execute("error_json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        self.assertEqual(result["data"]["detail"], "kept")
        self.assertEqual(result["data"]["result"], "ERROR: boom")

    async def test_nonzero_exit_without_json_keeps_exit_code(self) -> None:
        _write_script_tool(self.tools, "exit_only.py", {
            "name": "exit_only", "description": "exit", "input_schema": {},
        }, body=[
            "print('bad-news', file=sys.stderr)",
            "sys.exit(9)",
        ], timeout=30)
        self._load()
        result = await self._execute("exit_only")
        self.assertFalse(result["ok"])
        self.assertIn("script exited with code 9", result["error"])
        self.assertEqual(result["data"]["stderr"], "bad-news")

    async def test_malformed_json_output_keeps_exit_code(self) -> None:
        _write_script_tool(self.tools, "bad_json.py", {
            "name": "bad_json", "description": "bad", "input_schema": {},
        }, body=[
            "print('{not json')",
            "sys.exit(2)",
        ], timeout=30)
        self._load()
        result = await self._execute("bad_json")
        self.assertFalse(result["ok"])
        self.assertIn("script exited with code 2", result["error"])
        self.assertEqual(result["data"]["stdout"], "{not json")

    async def test_subprocess_env_replaces_stale_runtime_identity(self) -> None:
        env_report_keys = [
            "CLONOTH_WORKSPACE", "CLONOTH_SESSION_ID", "CLONOTH_TASK_ID",
            "CLONOTH_SUPERVISOR_URL", "CLONOTH_ADMIN_TOKEN", "OPENAI_API_KEY",
            "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            "MY_PROVIDER_API_KEY", "DEEPSEEK_API_KEY", "CLONOTH_UNRELATED",
            "CLONOTH_STALE_SENTINEL",
        ]
        _write_script_tool(self.tools, "env_report.py", {
            "name": "env_report", "description": "env", "input_schema": {},
        }, body=[
            f"keys = {env_report_keys!r}",
            "report = {'present': {key: (key in os.environ) for key in keys}}",
            "report['routing'] = {"
            "'workspace': os.environ.get('CLONOTH_WORKSPACE') == args['expect']['workspace'], "
            "'session': os.environ.get('CLONOTH_SESSION_ID') == args['expect']['session'], "
            "'task': os.environ.get('CLONOTH_TASK_ID') == args['expect']['task'], "
            "'supervisor': os.environ.get('CLONOTH_SUPERVISOR_URL') == args['expect']['supervisor']}",
            "report['admin_token_unchanged'] = os.environ.get('CLONOTH_ADMIN_TOKEN') == args['expect']['admin_token']",
            "report['unrelated_kept'] = os.environ.get('CLONOTH_UNRELATED') == 'keep-me'",
            "print(json.dumps(report))",
        ], timeout=30)
        self._load()

        fake_env = {
            # Fabricated credentials only; nothing real is read or printed.
            "CLONOTH_ADMIN_TOKEN": "fake-admin-token-not-real",
            "CLONOTH_SESSION_ID": "stale-session",
            "CLONOTH_TASK_ID": "stale-task",
            "CLONOTH_SUPERVISOR_URL": "http://stale.invalid",
            "CLONOTH_WORKSPACE": "/stale/install/root",
            "OPENAI_API_KEY": "fake-openai-key-not-real",
            "OPENAI_BASE_URL": "http://fake-provider.invalid",
            "ANTHROPIC_API_KEY": "fake-anthropic-key-not-real",
            "GEMINI_API_KEY": "fake-gemini-key-not-real",
            "MY_PROVIDER_API_KEY": "fake-provider-key-not-real",
            "CLONOTH_UNRELATED": "keep-me",
            "CLONOTH_STALE_SENTINEL": "stale",
        }
        ctx = _ToolCtx(
            self.root, session_id="sess-live", task_id="task-live",
            supervisor_url="http://supervisor.live",
        )
        arguments = {"expect": {
            "workspace": str(self.root),
            "session": "sess-live",
            "task": "task-live",
            "supervisor": "http://supervisor.live",
            "admin_token": "fake-admin-token-not-real",
        }}
        with patch.dict(os.environ, fake_env, clear=False):
            assert self.registry is not None
            result = await self.registry.execute(
                name="env_report", arguments=arguments, ctx=ctx, _authorized=True,
            )

        self.assertTrue(result["ok"], msg=result)
        report = result["data"]
        self.assertEqual(
            report["routing"],
            {"workspace": True, "session": True, "task": True, "supervisor": True},
            msg="child tools must inherit the current runtime identity, not a stale parent env",
        )
        for key in ("CLONOTH_WORKSPACE", "CLONOTH_SESSION_ID", "CLONOTH_TASK_ID",
                    "CLONOTH_SUPERVISOR_URL", "CLONOTH_ADMIN_TOKEN", "CLONOTH_UNRELATED"):
            self.assertTrue(report["present"][key], msg=key)
        self.assertTrue(report["admin_token_unchanged"])
        self.assertTrue(report["unrelated_kept"])
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
                    "GEMINI_API_KEY", "MY_PROVIDER_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertFalse(report["present"][key], msg=f"{key} must be filtered from tool env")

    async def test_subprocess_env_does_not_invent_admin_token_from_install_file(self) -> None:
        _write_script_tool(self.tools, "env_probe.py", {
            "name": "env_probe", "description": "probe", "input_schema": {},
        }, body=[
            "keys = ['CLONOTH_WORKSPACE', 'CLONOTH_SESSION_ID', 'CLONOTH_TASK_ID', "
            "'CLONOTH_SUPERVISOR_URL', 'CLONOTH_ADMIN_TOKEN']",
            "print(json.dumps({'present': {key: (key in os.environ) for key in keys}}))",
        ], timeout=30)
        self._load()

        data_dir = self.root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".admin_token").write_text("fake-install-file-token", encoding="utf-8")

        stale_env = {
            "CLONOTH_SESSION_ID": "stale-session",
            "CLONOTH_TASK_ID": "stale-task",
            "CLONOTH_SUPERVISOR_URL": "http://stale.invalid",
            "CLONOTH_WORKSPACE": "/stale/install/root",
        }
        empty_ctx = _ToolCtx(self.root, session_id="", task_id="", supervisor_url="")
        with patch.dict(os.environ, stale_env, clear=False):
            os.environ.pop("CLONOTH_ADMIN_TOKEN", None)
            assert self.registry is not None
            result = await self.registry.execute(
                name="env_probe", arguments={}, ctx=empty_ctx, _authorized=True,
            )

        self.assertTrue(result["ok"], msg=result)
        present = result["data"]["present"]
        self.assertTrue(present["CLONOTH_WORKSPACE"])
        for key in ("CLONOTH_SESSION_ID", "CLONOTH_TASK_ID", "CLONOTH_SUPERVISOR_URL"):
            self.assertFalse(present[key], msg=f"{key} must be removed when the context has no value")
        # The registry is not a credential loader: it never reads data/.admin_token.
        self.assertFalse(present["CLONOTH_ADMIN_TOKEN"])


# ---------------------------------------------------------------------------
#  ToolContext.wait_for_approval
# ---------------------------------------------------------------------------

class _FakeClock:
    """Deterministic monotonic clock: every read advances by *step* seconds.

    [Audit A] The approval failure budget is time based, so these tests drive
    ``toolbox.context._monotonic`` instead of sleeping for real.
    """

    def __init__(self, step: float = 10.0, start: float = 1000.0) -> None:
        self.now = float(start)
        self.step = float(step)

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class ApprovalWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, http, *, task_id: str = "task-1", parent_session_id: str = "") -> ToolContext:
        return ToolContext(
            supervisor_url="http://supervisor.test",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=self.root,
            http=http,
            registry=None,
            task_id=task_id,
            parent_session_id=parent_session_id,
            approval_poll_interval_sec=0.001,
        )

    async def test_transient_connection_and_timeout_errors_are_retried(self) -> None:
        calls = {"approvals": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            calls["approvals"] += 1
            if calls["approvals"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
            if calls["approvals"] == 2:
                raise httpx.ReadTimeout("read timed out", request=request)
            return httpx.Response(200, json={"approval_id": "ap-1", "status": "allowed"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await self._ctx(http).wait_for_approval("ap-1")

        self.assertEqual(result["status"], "allowed")
        self.assertEqual(calls["approvals"], 3, "transient transport failures must be retried")

    async def test_5xx_is_retried_but_4xx_is_a_structured_error(self) -> None:
        calls = {"approvals": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            calls["approvals"] += 1
            if calls["approvals"] <= 2:
                return httpx.Response(503, json={"error": "unavailable"}, request=request)
            return httpx.Response(200, json={"status": "allowed"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as http:
            result = await self._ctx(http).wait_for_approval("ap-1")
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(calls["approvals"], 3)

        seen = {"approvals": 0}

        def forbidden(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            seen["approvals"] += 1
            return httpx.Response(403, json={"error": "forbidden"}, request=request)

        # [Audit A] Old contract: a bare httpx.HTTPStatusError escaped the tool
        # call. New contract: a non-retryable, structured object — still failing
        # fast (exactly one poll, no retry) but never a naked transport exception.
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
            result = await self._ctx(http).wait_for_approval("ap-1")
        self.assertEqual(
            result,
            {
                "approval_id": "ap-1",
                "status": "approval_error",
                "http_status": 403,
                "error": "approval ap-1 poll rejected (HTTP 403)",
            },
        )
        self.assertEqual(seen["approvals"], 1, "4xx must fail fast instead of retrying")

    async def test_unusable_approval_responses_have_a_bounded_failure(self) -> None:
        for status, payload in ((200, {"unexpected": "shape"}), (302, {"status": "allowed"})):
            with self.subTest(status=status):
                renewals = []

                def malformed(request: httpx.Request) -> httpx.Response:
                    if request.url.path.endswith("/cancelled"):
                        return httpx.Response(200, json={"cancelled": False})
                    if request.url.path.endswith("/renew_lease"):
                        renewals.append(request.extensions.get("timeout"))
                        return httpx.Response(200, json={"ok": True})
                    return httpx.Response(status, json=payload)

                async with httpx.AsyncClient(transport=httpx.MockTransport(malformed), timeout=None) as http:
                    with patch.object(context_module, "_monotonic", _FakeClock(step=40)):
                        result = await self._ctx(http).wait_for_approval("ap-malformed")
                self.assertEqual(result["status"], "approval_unavailable")
                self.assertGreaterEqual(result["failed_for_sec"], 120)
                self.assertTrue(renewals)
                self.assertTrue(all(all(value == 5 for value in timeouts.values()) for timeouts in renewals))

    async def test_missing_approval_reports_approval_lost(self) -> None:
        seen = {"approvals": 0}

        def gone(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            seen["approvals"] += 1
            return httpx.Response(404, json={"error": "no such approval"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(gone)) as http:
            result = await self._ctx(http).wait_for_approval("ap-1")

        # A lost approval is its own terminal state, not a denial and not a raise.
        self.assertEqual(result["status"], "approval_lost")
        self.assertEqual(result["approval_id"], "ap-1")
        self.assertEqual(result["http_status"], 404)
        self.assertIn("no longer exists", result["error"])
        self.assertEqual(seen["approvals"], 1, "404 must not be retried")

    async def test_consecutive_supervisor_failures_end_in_approval_unavailable(self) -> None:
        attempts = {"approvals": 0}

        def broken(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/renew_lease"):
                return httpx.Response(200, json={"ok": True}, request=request)
            if request.url.path.endswith("/cancelled"):
                # the cancel probe is down too: no verified cancel state either
                raise httpx.ConnectError("supervisor down", request=request)
            attempts["approvals"] += 1
            if attempts["approvals"] % 2:
                raise httpx.ReadTimeout("supervisor down", request=request)
            return httpx.Response(503, json={"error": "unavailable"}, request=request)

        clock = _FakeClock(step=10.0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
            with patch.object(context_module, "_monotonic", clock):
                result = await self._ctx(http).wait_for_approval("ap-1")

        self.assertEqual(result["status"], "approval_unavailable")
        self.assertEqual(result["approval_id"], "ap-1")
        self.assertEqual(result["failure_budget_sec"], 120.0)
        self.assertGreaterEqual(result["failed_for_sec"], 120.0)
        self.assertIn("unavailable", result["error"])
        self.assertIn("Supervisor", result["error"])
        self.assertNotIn("denied", result["error"].lower())
        # Bounded: ~120s of failures at a 10s fake step, not an endless retry loop.
        self.assertGreaterEqual(attempts["approvals"], 12)
        self.assertLessEqual(attempts["approvals"], 20)
        self.assertEqual(context_module._APPROVAL_FAILURE_BUDGET_SEC, 120.0)

    async def test_pending_approvals_do_not_consume_the_failure_budget(self) -> None:
        plan = ["503", "503"] + ["pending"] * 10 + ["allowed"]
        calls = {"approvals": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/renew_lease"):
                return httpx.Response(200, json={"ok": True}, request=request)
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            kind = plan[min(calls["approvals"], len(plan) - 1)]
            calls["approvals"] += 1
            if kind == "503":
                return httpx.Response(503, json={"error": "unavailable"}, request=request)
            return httpx.Response(200, json={"status": kind}, request=request)

        # 30s per poll: if a pending answer counted toward the budget, the run of
        # ten pending polls alone (300s) would abort this legitimate human wait.
        clock = _FakeClock(step=30.0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with patch.object(context_module, "_monotonic", clock):
                result = await self._ctx(http).wait_for_approval("ap-1")

        self.assertEqual(result["status"], "allowed")
        self.assertEqual(calls["approvals"], len(plan), "every poll must run; nothing aborts the wait")

    async def test_unverifiable_cancel_check_does_not_abort_a_pending_wait(self) -> None:
        polls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                polls.append("cancel")
                raise httpx.ConnectError("cancel endpoint down", request=request)
            if request.url.path.endswith("/renew_lease"):
                return httpx.Response(200, json={"ok": True}, request=request)
            polls.append("approval")
            status = "pending" if polls.count("approval") == 1 else "allowed"
            return httpx.Response(200, json={"status": status}, request=request)

        clock = _FakeClock(step=600.0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with patch.object(context_module, "_monotonic", clock):
                result = await self._ctx(http).wait_for_approval("ap-1")

        # An unanswered cancel probe is "unknown", never "cancelled" and never an
        # implicit authorisation: the approval decision itself still decides.
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(polls, ["cancel", "approval", "cancel", "approval"])

    async def test_approval_poll_uses_five_second_timeout(self) -> None:
        http = _RecordingHttp()
        result = await self._ctx(http).wait_for_approval("ap-1")
        self.assertEqual(result["status"], "allowed")
        approval_calls = [call for call in http.get_calls if "/v1/approvals/" in call[0]]
        self.assertEqual(len(approval_calls), 1)
        self.assertEqual(approval_calls[0][1]["timeout"], 5.0)
        cancel_calls = [call for call in http.get_calls if call[0].endswith("/cancelled")]
        self.assertEqual(cancel_calls[0][1]["timeout"], 2.0)

    async def test_lease_renewal_during_pending_approval(self) -> None:
        posts: list[tuple[str, dict]] = []
        approvals = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": False}, request=request)
            if request.url.path.endswith("/renew_lease"):
                posts.append((request.url.path, json.loads(request.content)))
                return httpx.Response(200, json={"ok": True}, request=request)
            approvals["count"] += 1
            status = "pending" if approvals["count"] == 1 else "allowed"
            return httpx.Response(200, json={"status": status}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await self._ctx(http, task_id="task-7").wait_for_approval("ap-1")

        self.assertEqual(result["status"], "allowed")
        self.assertTrue(posts, "a pending approval must renew the task lease")
        self.assertTrue(all(path.endswith("/v1/tasks/task-7/renew_lease") for path, _ in posts))
        self.assertEqual(posts[0][1]["worker_id"], "worker-1")
        self.assertEqual(posts[0][1]["lease_sec"], 300.0)

    async def test_cancel_returns_cancelled_without_polling_approval(self) -> None:
        seen = {"approvals": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cancelled"):
                return httpx.Response(200, json={"cancelled": True}, request=request)
            seen["approvals"] += 1
            return httpx.Response(200, json={"status": "pending"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await self._ctx(http, task_id="task-8").wait_for_approval("ap-9")

        self.assertEqual(result, {"approval_id": "ap-9", "status": "cancelled"})
        self.assertEqual(seen["approvals"], 0)

    async def test_session_level_cancel_routes_to_parent_session(self) -> None:
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(request.url.path)
            return httpx.Response(200, json={"cancelled": True}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = self._ctx(http, task_id="", parent_session_id="parent-42")
            result = await ctx.wait_for_approval("ap-10")

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(urls, ["/v1/sessions/parent-42/cancelled"])


# ---------------------------------------------------------------------------
#  manage_tool: registry.tools_dir target, metadata, guards
# ---------------------------------------------------------------------------

class ManageToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.install_root = base / "install"
        self.install_root.mkdir()
        self.tools_dir = self.install_root / "tools"
        self.deny = False
        self.ops: list[dict] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.registry = ToolRegistry(workspace_root=self.install_root, tools_dir=self.tools_dir)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self._tmp.cleanup()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/request":
            self.ops.append(json.loads(request.content))
            if self.deny:
                return httpx.Response(
                    200, json={"safety_level": "deny", "reason": "policy denies"}, request=request,
                )
            return httpx.Response(200, json={"safety_level": "auto"}, request=request)
        if request.url.path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False}, request=request)
        return httpx.Response(404, json={}, request=request)

    def _ctx(self, *, workspace: Path | None = None, task_id: str = "task-1") -> ToolContext:
        return ToolContext(
            supervisor_url="http://supervisor.test",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=self.install_root,
            http=self.http,
            registry=self.registry,
            task_id=task_id,
            workspace=workspace,
        )

    def _tool_files(self) -> list[str]:
        return sorted(path.name for path in self.tools_dir.glob("*.py"))

    async def test_upsert_creates_metadata_and_reloads(self) -> None:
        result = await manage_tool({
            "action": "upsert",
            "name": "temp_echo",
            "description": "echo args",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": [],
            },
            "script": "output({'echo': args.get('value')})",
            "timeout_sec": 12,
            "pure": True,
            "async_mode": True,
        }, self._ctx())

        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["data"]["path"], "tools/temp_echo.py")
        self.assertEqual(result["data"]["reloaded"], len(self.registry.list_specs()))
        target = self.tools_dir / "temp_echo.py"
        self.assertTrue(target.exists())
        rendered = target.read_text(encoding="utf-8")
        self.assertIn("SPEC = ", rendered)
        self.assertIn("TIMEOUT_SEC = 12.0", rendered)
        self.assertIn("    output({'echo': args.get('value')})", rendered)

        spec = self.registry.get_spec("temp_echo")
        self.assertEqual(spec["description"], "echo args")
        self.assertEqual(spec["input_schema"]["properties"], {"value": {"type": "string"}})
        self.assertIs(spec["pure"], True)
        self.assertIs(spec["async_mode"], True)

        executed = await self.registry.execute(
            name="temp_echo", arguments={"value": "hi"}, ctx=self._ctx(), _authorized=True,
        )
        self.assertTrue(executed["ok"], msg=executed)
        self.assertEqual(executed["data"]["echo"], "hi")

    async def test_update_replaces_metadata(self) -> None:
        await manage_tool({
            "action": "upsert", "name": "temp_echo", "description": "first",
            "script": "output('v1')", "pure": True,
        }, self._ctx())
        self.assertIs(self.registry.get_spec("temp_echo")["pure"], True)

        result = await manage_tool({
            "action": "upsert", "name": "temp_echo", "description": "second",
            "script": "output('v2')",
        }, self._ctx())

        self.assertTrue(result["ok"], msg=result)
        spec = self.registry.get_spec("temp_echo")
        self.assertEqual(spec["description"], "second")
        self.assertIs(spec["pure"], False, msg="omitted flags must fall back to their defaults")
        self.assertIs(spec["async_mode"], False)
        rendered = (self.tools_dir / "temp_echo.py").read_text(encoding="utf-8")
        self.assertIn("second", rendered)
        self.assertIn("output('v2')", rendered)

    async def test_omitted_action_defaults_to_upsert_then_reload(self) -> None:
        created = await manage_tool({
            "name": "default_upsert", "script": "output('d')",
        }, self._ctx())
        self.assertTrue(created["ok"], msg=created)
        self.assertTrue((self.tools_dir / "default_upsert.py").exists())

        reloaded = await manage_tool({}, self._ctx())
        self.assertTrue(reloaded["ok"], msg=reloaded)
        self.assertIn("reloaded", reloaded["data"]["result"].lower())
        self.assertEqual(reloaded["data"]["tools"], len(self.registry.list_specs()))

    async def test_invalid_tool_timeout_is_rejected_before_write_approval(self) -> None:
        before = self._tool_files()
        for timeout in ("bad", [], {}, float("inf"), float("nan"), 0, -1):
            with self.subTest(timeout=timeout):
                result = await manage_tool({
                    "action": "upsert", "name": "bad_timeout", "script": "output({})",
                    "timeout_sec": timeout,
                }, self._ctx())
                self.assertFalse(result["ok"])
                self.assertIn("timeout_sec", result["error"])
        self.assertEqual(self._tool_files(), before)
        self.assertEqual(self.ops, [])

    async def test_invalid_action_is_rejected_without_writing(self) -> None:
        before = self._tool_files()
        for action in ("delete", "remove", "create", "reload_all"):
            with self.subTest(action=action):
                result = await manage_tool({
                    "action": action, "name": "should_not_exist", "script": "output('x')",
                }, self._ctx())
                self.assertFalse(result["ok"])
                self.assertIn("invalid action", result["error"])
        self.assertEqual(self._tool_files(), before)
        self.assertEqual(self.ops, [], "an invalid action must not reach the approval guard")

    async def test_invalid_and_reserved_names_are_rejected_without_writing(self) -> None:
        before = self._tool_files()
        cases = {
            "bad-name": "invalid tool name",
            "1tool": "invalid tool name",
            "a" * 65: "invalid tool name",
        }
        for name, message in cases.items():
            with self.subTest(name=name):
                result = await manage_tool({
                    "action": "upsert", "name": name, "script": "output('x')",
                }, self._ctx())
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

        for name in ("manage_tool", "read_file", "get_context_window"):
            self.assertIn(name, RESERVED_TOOL_NAMES)
            with self.subTest(reserved=name):
                result = await manage_tool({
                    "action": "upsert", "name": name, "script": "output('x')",
                }, self._ctx())
                self.assertFalse(result["ok"])
                self.assertIn("reserved tool name", result["error"])

        result = await manage_tool({"action": "upsert", "name": "names_only"}, self._ctx())
        self.assertFalse(result["ok"])
        self.assertIn("'script' is required", result["error"])

        self.assertEqual(self._tool_files(), before)
        self.assertEqual(self.ops, [])

    async def test_denied_write_does_not_land(self) -> None:
        self.deny = True
        before = self._tool_files()
        result = await manage_tool({
            "action": "upsert", "name": "denied_tool", "script": "output('x')",
        }, self._ctx())

        self.assertFalse(result["ok"])
        self.assertIn("denies", (result.get("error") or "").lower())
        self.assertEqual(self._tool_files(), before)
        self.assertIsNone(self.registry.get_spec("denied_tool"))
        self.assertEqual(len(self.ops), 1)

    async def test_active_workspace_mismatch_still_writes_registry_tools_dir(self) -> None:
        project = Path(self._tmp.name) / "project"
        project.mkdir()
        result = await manage_tool({
            "action": "upsert", "name": "ws_tool", "script": "output('w')",
        }, self._ctx(workspace=project))

        self.assertTrue(result["ok"], msg=result)
        self.assertTrue((self.tools_dir / "ws_tool.py").exists())
        self.assertFalse((project / "tools").exists(), "tools belong to the installation registry")
        self.assertIsNotNone(self.registry.get_spec("ws_tool"))


# ---------------------------------------------------------------------------
#  manage_schedule: list/upsert/delete against a tempdir schedules.yaml
# ---------------------------------------------------------------------------

class ManageScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.deny = False
        self.on_guard = None
        self.ops: list[dict] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self._tmp.cleanup()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/request":
            body = json.loads(request.content)
            self.ops.append(body)
            if self.on_guard is not None:
                self.on_guard(body)
            if self.deny:
                return httpx.Response(
                    200, json={"safety_level": "deny", "reason": "policy denies"}, request=request,
                )
            return httpx.Response(200, json={"safety_level": "auto"}, request=request)
        if request.url.path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False}, request=request)
        return httpx.Response(404, json={}, request=request)

    def _ctx(self, *, conversation_key: str = "", task_id: str = "task-1") -> ToolContext:
        # A fresh context per tool call mirrors the engine: request_guard caches
        # approved guard fingerprints per context.
        return ToolContext(
            supervisor_url="http://supervisor.test",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=self.root,
            http=self.http,
            registry=None,
            task_id=task_id,
            conversation_key=conversation_key,
        )

    def _seed(self, schedules: list[dict]) -> None:
        save_schedules(self.root, schedules)

    def _schedules_file(self) -> Path:
        return self.root / "data" / "schedules.yaml"

    async def test_partial_update_preserves_existing_fields(self) -> None:
        original = {
            "id": "daily",
            "cron": "0 1 * * *",
            "conversation_key": "ck-old",
            "enabled": False,
            "once": True,
            "type": "script",
            "command": "echo old",
            "timeout": 120,
            "silent": False,
            "text": "prefix",
            "entry_node_id": "worker",
            "workflow_id": "wf-1",
        }
        self._seed([dict(original)])
        repo_schedule = REPO_ROOT / "data" / "schedules.yaml"
        repo_before = repo_schedule.read_text(encoding="utf-8") if repo_schedule.exists() else None

        result = await manage_schedule(
            {"action": "upsert", "id": "daily", "cron": "30 2 * * *"}, self._ctx(),
        )

        self.assertTrue(result["ok"], msg=result)
        stored = load_schedules(self.root)
        self.assertEqual(len(stored), 1)
        entry = stored[0]
        self.assertEqual(entry["cron"], "30 2 * * *")
        self.assertEqual(entry["enabled"], False)
        self.assertEqual(entry["once"], True)
        self.assertEqual(entry["timeout"], 120)
        self.assertEqual(entry["silent"], False)
        self.assertEqual(entry["entry_node_id"], "worker")
        self.assertEqual(entry["workflow_id"], "wf-1")
        self.assertEqual(entry["command"], "echo old")
        self.assertEqual(entry["type"], "script")
        self.assertEqual(entry["conversation_key"], "ck-old")
        self.assertEqual(result["data"]["schedule"], entry)
        self.assertIs(result["data"]["replaced"], True)

        # The real schedule storage stays inside the tempdir.
        self.assertEqual(self._schedules_file().parent.parent, self.root)
        repo_after = repo_schedule.read_text(encoding="utf-8") if repo_schedule.exists() else None
        self.assertEqual(repo_after, repo_before, "tests must never touch the real schedules.yaml")

    async def test_upsert_changes_are_read_after_approval(self) -> None:
        self._seed([{
            "id": "daily", "cron": "0 1 * * *", "conversation_key": "ck",
            "enabled": False, "once": False, "type": "message", "text": "old text",
        }])

        concurrent = {
            "id": "daily", "cron": "0 9 * * *", "conversation_key": "ck",
            "enabled": True, "once": False, "type": "message", "text": "concurrent text",
        }

        def _other_writer(_body: dict) -> None:
            save_schedules(self.root, [dict(concurrent)])

        self.on_guard = _other_writer
        result = await manage_schedule(
            {"action": "upsert", "id": "daily", "text": "mine"}, self._ctx(),
        )

        self.assertTrue(result["ok"], msg=result)
        stored = load_schedules(self.root)[0]
        # Fields the caller did not touch come from the post-approval read, not
        # from the pre-approval snapshot.
        self.assertEqual(stored["cron"], "0 9 * * *")
        self.assertEqual(stored["enabled"], True)
        self.assertEqual(stored["text"], "mine", "explicit upsert fields win over concurrent edits")

    async def test_list_summary_is_readable_without_text_or_command(self) -> None:
        self._seed([
            {
                "id": "msg_job", "cron": "*/5 * * * *", "type": "message",
                "text": "SECRET-MESSAGE-TEXT", "enabled": True, "once": False,
                "conversation_key": "chan:7",
            },
            {
                "id": "script_job", "cron": "0 3 * * *", "type": "script",
                "command": "SECRET-COMMAND-LINE", "timeout": 30, "silent": True,
                "enabled": False, "once": True, "conversation_key": "chan:7",
            },
        ])

        result = await manage_schedule({"action": "list"}, self._ctx())

        self.assertTrue(result["ok"], msg=result)
        summary = result["data"]["result"]
        self.assertIn("2 schedules", summary)
        self.assertIn("msg_job", summary)
        self.assertIn("script_job", summary)
        self.assertIn("cron '*/5 * * * *'", summary)
        self.assertIn("enabled", summary)
        self.assertIn("disabled, once", summary)
        self.assertIn("ck=chan:7", summary)
        self.assertIn("read_file data/schedules.yaml", summary)
        self.assertNotIn("SECRET-MESSAGE-TEXT", summary)
        self.assertNotIn("SECRET-COMMAND-LINE", summary)
        self.assertEqual(len(result["data"]["schedules"]), 2)

    async def test_conversation_key_inheritance_and_fallback(self) -> None:
        base = {"cron": "0 4 * * *", "type": "message"}

        await manage_schedule(
            {"action": "upsert", "id": "inherit_job", "text": "hello", **base},
            self._ctx(conversation_key="chan:42"),
        )
        await manage_schedule(
            {"action": "upsert", "id": "explicit_job", "text": "hello", **base,
             "conversation_key": "explicit-key"},
            self._ctx(conversation_key="chan:42"),
        )
        await manage_schedule(
            {"action": "upsert", "id": "fallback_job", "text": "hello", **base},
            self._ctx(conversation_key=""),
        )

        stored = {entry["id"]: entry for entry in load_schedules(self.root)}
        self.assertEqual(stored["inherit_job"]["conversation_key"], "chan:42")
        self.assertEqual(stored["explicit_job"]["conversation_key"], "explicit-key")
        self.assertEqual(stored["fallback_job"]["conversation_key"], "scheduler:fallback_job")

    async def test_omitted_action_defaults_to_upsert_then_list(self) -> None:
        created = await manage_schedule(
            {"id": "default_job", "cron": "0 7 * * *", "text": "hi"}, self._ctx(),
        )
        self.assertTrue(created["ok"], msg=created)
        self.assertEqual([entry["id"] for entry in load_schedules(self.root)], ["default_job"])

        listed = await manage_schedule({}, self._ctx())
        self.assertTrue(listed["ok"], msg=listed)
        self.assertIn("1 schedules", listed["data"]["result"])
        self.assertIn("default_job", listed["data"]["result"])

    async def test_invalid_action_does_not_read_or_write(self) -> None:
        self._seed([{
            "id": "daily", "cron": "0 1 * * *", "type": "message", "text": "x",
        }])
        before = self._schedules_file().read_text(encoding="utf-8")

        with patch("supervisor.scheduler.load_schedules",
                   side_effect=AssertionError("invalid action must not read")), \
             patch("supervisor.scheduler.save_schedules",
                   side_effect=AssertionError("invalid action must not write")):
            for action in ("remove", "create", "update", "read", "show", "del"):
                with self.subTest(action=action):
                    result = await manage_schedule(
                        {"action": action, "id": "daily", "text": "y"}, self._ctx(),
                    )
                    self.assertFalse(result["ok"])
                    self.assertIn("invalid action", result["error"])

        self.assertEqual(self._schedules_file().read_text(encoding="utf-8"), before)
        self.assertEqual(self.ops, [])

    async def test_deny_does_not_write(self) -> None:
        self._seed([{
            "id": "daily", "cron": "0 1 * * *", "type": "message", "text": "keep me",
        }])
        before = self._schedules_file().read_text(encoding="utf-8")
        self.deny = True

        upsert = await manage_schedule(
            {"action": "upsert", "id": "daily", "text": "overwritten"}, self._ctx(),
        )
        delete = await manage_schedule({"action": "delete", "id": "daily"}, self._ctx())

        self.assertFalse(upsert["ok"])
        self.assertIn("policy denies", upsert["error"])
        self.assertFalse(delete["ok"])
        self.assertIn("policy denies", delete["error"])
        self.assertEqual(self._schedules_file().read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.ops), 2)
        self.assertEqual(load_schedules(self.root)[0]["text"], "keep me")

    async def test_invalid_delete_id_is_rejected_before_approval(self) -> None:
        self._seed([{"id": "keep", "cron": "0 4 * * *", "text": "unchanged"}])
        before = self._schedules_file().read_bytes()
        for sid in ("../escape", "bad id"):
            result = await manage_schedule({"action": "delete", "id": sid}, self._ctx())
            self.assertFalse(result["ok"])
            self.assertIn("invalid schedule id", result["error"])
        self.assertEqual(self.ops, [])
        self.assertEqual(self._schedules_file().read_bytes(), before)

    async def test_delete_removes_only_target_schedule(self) -> None:
        self._seed([
            {"id": "keep", "cron": "0 1 * * *", "type": "message", "text": "a"},
            {"id": "drop", "cron": "0 2 * * *", "type": "message", "text": "b"},
        ])

        missing = await manage_schedule({"action": "delete", "id": "absent"}, self._ctx())
        self.assertFalse(missing["ok"])
        self.assertIn("schedule not found: absent", missing["error"])
        self.assertEqual(len(load_schedules(self.root)), 2)

        no_id = await manage_schedule({"action": "delete"}, self._ctx())
        self.assertFalse(no_id["ok"])
        self.assertIn("empty schedule id", no_id["error"])

        result = await manage_schedule({"action": "delete", "id": "drop"}, self._ctx())
        self.assertTrue(result["ok"], msg=result)
        self.assertIs(result["data"]["deleted"], True)
        self.assertEqual([entry["id"] for entry in load_schedules(self.root)], ["keep"])

    async def test_invalid_timeout_is_reported_without_writing(self) -> None:
        self._seed([{
            "id": "scripty", "cron": "0 1 * * *", "type": "script",
            "command": "echo x", "timeout": 30, "silent": True, "enabled": True,
        }])
        before = self._schedules_file().read_text(encoding="utf-8")

        for bad in ("abc", "12x", float("inf"), float("nan"), ["x"], {"sec": 10}):
            with self.subTest(timeout=repr(bad)):
                result = await manage_schedule(
                    {"action": "upsert", "id": "scripty", "timeout": bad}, self._ctx(),
                )
                # A bad timeout must surface as a tool error, never as a crash.
                self.assertFalse(result["ok"])
                self.assertIn("timeout must be an integer", result["error"])

        self.assertEqual(self._schedules_file().read_text(encoding="utf-8"), before)
        self.assertEqual(load_schedules(self.root)[0]["timeout"], 30)

    async def test_script_defaults_and_timeout_clamp(self) -> None:
        created = await manage_schedule({
            "action": "upsert", "id": "fresh_script", "cron": "*/5 * * * *",
            "type": "script", "command": "echo hi", "timeout": 999,
        }, self._ctx())
        self.assertTrue(created["ok"], msg=created)
        entry = load_schedules(self.root)[0]
        self.assertEqual(entry["timeout"], 300)
        self.assertIs(entry["silent"], True)
        self.assertIs(entry["enabled"], True)
        self.assertIs(entry["once"], False)
        self.assertEqual(entry["conversation_key"], "scheduler:fresh_script")

        lowered = await manage_schedule(
            {"action": "upsert", "id": "fresh_script", "timeout": 1}, self._ctx(),
        )
        self.assertTrue(lowered["ok"], msg=lowered)
        self.assertEqual(load_schedules(self.root)[0]["timeout"], 5)

        invalid_cron = await manage_schedule(
            {"action": "upsert", "id": "fresh_script", "cron": "0 1 * *"}, self._ctx(),
        )
        self.assertFalse(invalid_cron["ok"])
        self.assertIn("cron must be 5 fields", invalid_cron["error"])

        invalid_type = await manage_schedule(
            {"action": "upsert", "id": "fresh_script", "type": "http"}, self._ctx(),
        )
        self.assertFalse(invalid_type["ok"])
        self.assertIn("invalid type", invalid_type["error"])

        missing_message_text = await manage_schedule(
            {"action": "upsert", "id": "message_job", "cron": "0 6 * * *"}, self._ctx(),
        )
        self.assertFalse(missing_message_text["ok"])
        self.assertIn("empty text", missing_message_text["error"])
        self.assertEqual(load_schedules(self.root)[0]["timeout"], 5)


# ---------------------------------------------------------------------------
#  get_context_window
# ---------------------------------------------------------------------------

class ContextWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self, http, *, session_id: str = "sess-1", parent_session_id: str = "") -> ToolContext:
        return ToolContext(
            supervisor_url="http://supervisor.test",
            session_id=session_id,
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=self.root,
            http=http,
            registry=None,
            parent_session_id=parent_session_id,
        )

    async def test_model_visible_summary_and_parent_route(self) -> None:
        urls: list[str] = []
        payload = {
            "effective_tokens": 4321,
            "prompt_tokens": 9999,
            "estimated_tokens": 8888,
            "compact_threshold": 20000,
            "utilization": 0.216,
            "source": "llm",
            "message_count": 17,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(request.url.path)
            return httpx.Response(200, json=payload, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = self._ctx(http, session_id="branch-1", parent_session_id="parent-9")
            result = await get_context_window({}, ctx)

        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(urls, ["/v1/sessions/parent-9/context_window"])
        summary = result["data"]["result"]
        self.assertIn("4,321 / 20,000 tokens", summary)
        self.assertIn("21.6%", summary)
        self.assertIn("remaining: 15,679 tokens", summary)
        self.assertIn("source=llm", summary)
        self.assertIn("messages=17", summary)
        # Structured stats stay model-visible alongside the summary.
        for key, value in payload.items():
            self.assertEqual(result["data"][key], value)

    async def test_zero_effective_tokens_is_not_replaced_by_fallback(self) -> None:
        payload = {
            "effective_tokens": 0,
            "prompt_tokens": 9999,
            "estimated_tokens": 8888,
            "compact_threshold": 1000,
            "utilization": 0.0,
            "source": "llm",
            "message_count": 3,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await get_context_window({}, self._ctx(http))

        self.assertTrue(result["ok"], msg=result)
        self.assertIn("0 / 1,000 tokens", result["data"]["result"])
        self.assertNotIn("9,999", result["data"]["result"])
        self.assertEqual(result["data"]["effective_tokens"], 0)

    async def test_missing_effective_tokens_uses_prompt_fallback(self) -> None:
        payload = {
            "prompt_tokens": 1234,
            "compact_threshold": 5000,
            "utilization": 0.25,
            "source": "estimate",
            "message_count": 2,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await get_context_window({}, self._ctx(http))

        self.assertTrue(result["ok"], msg=result)
        self.assertIn("1,234 / 5,000 tokens", result["data"]["result"])

    async def test_http_error_returns_error_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "unavailable"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await get_context_window({}, self._ctx(http))

        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertIn("503", result["error"])
        self.assertTrue(result["data"]["result"].startswith("ERROR: "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
