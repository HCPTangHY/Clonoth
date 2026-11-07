"""Admin tool migration: clear_context / clonoth_debug + supervisor reset exclusions.

Why: both tools moved from the production tree into this repo and the reset
endpoint had to learn "do not kill the caller". How: every case runs the tool
scripts through ``runpy`` with a mocked httpx layer (no real http call, no real
session cleared), plus an in-process ASGI app whose ``app.state.state`` is a
fake SupervisorState bound to real methods in a temp workspace. Purpose: prove
auth headers, env wiring, reset exclusion semantics, honest partial-failure
reporting and debug read-only/passthrough behaviour without touching production.

Run: python3.11 -B -m unittest tests.test_admin_tools_migration -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CLEAR_CONTEXT = REPO / "tools" / "clear_context.py"
DEBUG_TOOL = REPO / "tools" / "clonoth_debug.py"

CHANNEL_ID = "123456789012345678"
ADMIN_TOKEN = "fake-admin-token-for-tool-tests"


# ── tool runner (runpy + mocked httpx) ───────────────────────────────────────


class _Resp:
    """Minimal httpx.Response stand-in: status, .json(), .text only."""

    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("response is not JSON")
        return self._payload


def _clean_env(**overrides) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLONOTH_")}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _invoke(script: Path, args: dict, env: dict[str, str], *,
            post=None, request=None) -> tuple[int, dict, io.StringIO]:
    """Run a tool script as __main__ with stdin args and mocked httpx calls."""
    stdout = io.StringIO()
    patches = [
        patch.dict(os.environ, env, clear=True),
        patch.object(sys, "stdin", io.StringIO(json.dumps(args))),
    ]
    if post is not None:
        patches.append(patch("httpx.post", post))
    if request is not None:
        patches.append(patch("httpx.request", request))
    code = 0
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(stdout))
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:  # tools always exit explicitly
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, json.loads(stdout.getvalue()), stdout


class ClearContextToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _env(self, **overrides) -> dict[str, str]:
        base = {
            "CLONOTH_WORKSPACE": str(self.ws),
            "CLONOTH_SUPERVISOR_URL": "http://supervisor.test:9999",
            "CLONOTH_ADMIN_TOKEN": ADMIN_TOKEN,
            "CLONOTH_TASK_ID": "task-caller-1",
            "CLONOTH_SESSION_ID": "branch_9",
        }
        base.update(overrides)
        return _clean_env(**base)

    def test_env_supervisor_url_bearer_and_reset_exclusions(self) -> None:
        calls: list[dict] = []

        def _post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if "/discord" in url:
                return _Resp(200, {"ok": True})
            return _Resp(200, {
                "ok": True,
                "old_session_id": "sess_old",
                "cleared_children": 2,
                "cancelled_tasks": 1,
                "context_files_cleaned": 3,
            })

        code, payload, stdout = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, self._env(), post=_post)

        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["result"], "Context cleared")
        self.assertEqual(data["conversation_key"], f"discord:{CHANNEL_ID}")
        # existing UI fields preserved
        for key in ("session_reset", "old_session_id", "cleared_children",
                    "cancelled_tasks", "context_files_cleaned", "bot_history"):
            self.assertIn(key, data)
        self.assertEqual(data["old_session_id"], "sess_old")
        self.assertEqual(data["cancelled_tasks"], 1)
        self.assertNotIn(ADMIN_TOKEN, stdout.getvalue(), "token must never be printed")

        reset_calls = [c for c in calls if c["url"].endswith("/v1/conversations/reset")]
        self.assertEqual(len(reset_calls), 1)
        reset = reset_calls[0]
        self.assertEqual(reset["url"], "http://supervisor.test:9999/v1/conversations/reset")
        self.assertEqual(reset["headers"]["Authorization"], f"Bearer {ADMIN_TOKEN}")
        self.assertEqual(reset["json"]["conversation_key"], f"discord:{CHANNEL_ID}")
        self.assertEqual(reset["json"]["exclude_task_id"], "task-caller-1")
        self.assertEqual(reset["json"]["exclude_session_id"], "branch_9")

        # bot bridge receives the numeric key, never the raw string / quotes
        bridge = [c for c in calls if "/discord" in c["url"]][0]
        self.assertEqual(bridge["url"], "http://127.0.0.1:8768/discord")
        self.assertIn(f"_channel_history[{CHANNEL_ID}]", bridge["json"]["code"])
        self.assertNotIn('"', bridge["json"]["code"])

    def test_bearer_falls_back_to_workspace_admin_token_file(self) -> None:
        (self.ws / "data").mkdir(parents=True, exist_ok=True)
        (self.ws / "data" / ".admin_token").write_text("file-token\n", encoding="utf-8")
        seen: list[dict] = []

        def _post(url, **kwargs):
            seen.append({"url": url, **kwargs})
            return _Resp(200, {"ok": True})

        env = self._env()
        env.pop("CLONOTH_ADMIN_TOKEN")
        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, env, post=_post)

        self.assertEqual(code, 0, payload)
        reset = [c for c in seen if c["url"].endswith("/reset")][0]
        self.assertEqual(reset["headers"]["Authorization"], "Bearer file-token")

    def test_missing_credentials_send_no_auth_header(self) -> None:
        seen: list[dict] = []

        def _post(url, **kwargs):
            seen.append({"url": url, **kwargs})
            return _Resp(200, {"ok": True})

        env = self._env()
        env.pop("CLONOTH_ADMIN_TOKEN")
        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, env, post=_post)

        self.assertEqual(code, 0, payload)
        reset = [c for c in seen if c["url"].endswith("/reset")][0]
        self.assertNotIn("Authorization", reset.get("headers") or {})

    def test_non_numeric_channel_id_is_rejected_before_any_http(self) -> None:
        def _post(url, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError(f"no HTTP call expected, got {url}")

        code, payload, _ = _invoke(
            CLEAR_CONTEXT,
            {"channel_id": "1] = []\nimport os; os.system('id') #"},
            self._env(),
            post=_post,
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("numeric Discord channel id", payload["error"])

    def test_partial_http_failure_never_reports_success(self) -> None:
        def _post(url, **kwargs):
            if "/discord" in url:
                return _Resp(200, {"ok": True})
            return _Resp(500, {"detail": "boom"})

        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, self._env(), post=_post)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("Context reset incomplete", payload["error"])
        self.assertNotIn("Context cleared", payload["data"]["result"])
        self.assertTrue(payload["data"]["session_reset"].startswith("failed: 500"))
        self.assertEqual(payload["data"]["bot_history"], "cleared")

    def test_bot_bridge_failure_still_reports_incomplete(self) -> None:
        def _post(url, **kwargs):
            if "/discord" in url:
                raise httpx.ConnectError("bridge down")
            return _Resp(200, {"ok": True, "old_session_id": "sess_old"})

        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, self._env(), post=_post)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data"]["session_reset"], "ok")
        self.assertEqual(payload["data"]["old_session_id"], "sess_old")
        self.assertIn("bot_history", payload["error"])

    def test_reset_404_means_already_clean(self) -> None:
        def _post(url, **kwargs):
            if "/discord" in url:
                return _Resp(200, {"ok": True})
            return _Resp(404, {"detail": "conversation not found"})

        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, self._env(), post=_post)

        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["session_reset"], "already clean (no active session)")

    def test_reset_401_is_reported_as_unauthorized(self) -> None:
        def _post(url, **kwargs):
            if "/discord" in url:
                return _Resp(200, {"ok": True})
            return _Resp(401, {"detail": "Unauthorized"})

        code, payload, _ = _invoke(CLEAR_CONTEXT, {"channel_id": CHANNEL_ID}, self._env(), post=_post)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("401 unauthorized", payload["data"]["session_reset"])

    def test_script_keeps_production_credential_names_only(self) -> None:
        source = CLEAR_CONTEXT.read_text(encoding="utf-8")
        self.assertIn("CLONOTH_ADMIN_TOKEN", source)
        self.assertIn("CLONOTH_SUPERVISOR_URL", source)
        self.assertIn("CLONOTH_TASK_ID", source)
        self.assertNotIn("CLONOTH_API_TOKEN", source, "no parallel token alias")
        self.assertIn("data", source)


# ── clonoth_debug ────────────────────────────────────────────────────────────


class DebugToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        (self.ws / "data").mkdir(parents=True, exist_ok=True)
        events = [
            {"type": "task_created", "ts": "2026-10-31T10:00:00.123456+00:00",
             "payload": {"task_id": "53cf8e69aaaa", "node_id": "child_coder", "message": "made"}},
            {"type": "task_started", "ts": "2026-10-31T10:00:01.000000+00:00",
             "payload": {"task_id": "53cf8e69aaaa", "node_id": "child_coder"}},
            {"type": "task_completed", "ts": "2026-10-31T10:00:02.000000+00:00",
             "payload": {"task_id": "53cf8e69aaaa", "node_id": "child_coder", "status": "completed"}},
            {"type": "approval_requested", "ts": "2026-10-31T10:00:03.000000+00:00",
             "payload": {"approval_id": "appr-1", "details": {"tool_name": "execute_command"}}},
        ]
        (self.ws / "data" / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _env(self, **overrides) -> dict[str, str]:
        base = {
            "CLONOTH_WORKSPACE": str(self.ws),
            "CLONOTH_SUPERVISOR_URL": "http://supervisor.test:9999/",
            "CLONOTH_ADMIN_TOKEN": ADMIN_TOKEN,
        }
        base.update(overrides)
        return _clean_env(**base)

    def test_health_uses_env_url_and_bearer(self) -> None:
        calls: list[dict] = []

        def _request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _Resp(200, {"status": "ok", "workspace_root": str(self.ws)})

        code, payload, stdout = _invoke(DEBUG_TOOL, {"action": "health"}, self._env(), request=_request)

        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["payload"]["status"], "ok")
        self.assertEqual(json.loads(payload["data"]["result"]), payload["data"]["payload"],
                         "data.result must be the full JSON payload (engine only forwards data.result)")
        self.assertGreater(len(payload["data"]["result"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["url"], "http://supervisor.test:9999/v1/health")
        self.assertEqual(calls[0]["headers"]["Authorization"], f"Bearer {ADMIN_TOKEN}")
        self.assertNotIn(ADMIN_TOKEN, stdout.getvalue())
    def test_api_action_passes_method_body_params_through(self) -> None:
        calls: list[dict] = []

        def _request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _Resp(200, {"ok": True})

        code, payload, _ = _invoke(
            DEBUG_TOOL,
            {
                "action": "api",
                "path": "/v1/sessions/sess_1/running_tasks",
                "method": "POST",
                "body": {"a": 1},
                "params": {"q": "x y"},
            },
            self._env(),
            request=_request,
        )

        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://supervisor.test:9999/v1/sessions/sess_1/running_tasks")
        self.assertEqual(call["json"], {"a": 1})
        self.assertEqual(call["params"], {"q": "x y"})

    def test_api_action_defaults_to_read_only_get(self) -> None:
        calls: list[dict] = []

        def _request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _Resp(200, {"tasks": {"running": 1}})

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "api", "path": "/v1/admin/state"}, self._env(), request=_request)

        self.assertEqual(code, 0, payload)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertIsNone(calls[0]["json"])

    def test_http_transport_errors_are_reported_as_not_ok(self) -> None:
        def _request_401(method, url, **kwargs):
            return _Resp(401, {"detail": "Unauthorized"})

        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "health"}, self._env(), request=_request_401)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"], "HTTP failure must not be reported as success")
        self.assertIn("HTTP 401", payload["error"])
        self.assertEqual(payload["data"]["payload"]["error"], "HTTP 401")
        self.assertEqual(payload["data"]["payload"]["detail"], "Unauthorized")

        def _request_down(method, url, **kwargs):
            raise httpx.ConnectError("supervisor down")

        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "api", "path": "/v1/admin/state"},
                                   self._env(), request=_request_down)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("Connection failed", payload["error"])

    def test_successful_payload_with_error_field_is_not_a_transport_error(self) -> None:
        # /v1/admin/state 的合法 payload 可能包含 error 字段，不能被当成传输失败。
        def _request(method, url, **kwargs):
            return _Resp(200, {"tasks": {"running": 0}, "error": "partial snapshot"})

        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "api", "path": "/v1/admin/state"},
                                   self._env(), request=_request)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["payload"]["error"], "partial snapshot")

    def test_large_payload_is_not_truncated(self) -> None:
        big = {f"key_{i}": "x" * 64 for i in range(600)}

        def _request(method, url, **kwargs):
            return _Resp(200, big)

        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "api", "path": "/v1/admin/state"},
                                   self._env(), request=_request)
        self.assertEqual(code, 0, payload)
        self.assertGreater(len(payload["data"]["result"]), 16000, "16K truncation boundary must be crossed")
        self.assertEqual(json.loads(payload["data"]["result"]), big)

    def test_active_tasks_marks_degraded_ok_false(self) -> None:
        def _request(method, url, **kwargs):
            raise httpx.ConnectError("supervisor down")

        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "active_tasks"}, self._env(), request=_request)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        # local event-derived data is still returned, but honestly labelled
        self.assertIn("tasks", payload["data"]["payload"])
        self.assertIn("api_error", payload["data"]["payload"])

    def test_event_tail_is_bounded_and_returns_last_lines(self) -> None:
        source = DEBUG_TOOL.read_text(encoding="utf-8")
        self.assertNotIn("readlines()", source, "must not load a GB event log into memory")
        self.assertIn("\"tail\"", source, "production tail -n argv call must be preserved")
        self.assertIn("timeout=10", source)

        big_log = self.ws / "data" / "events.jsonl"
        with big_log.open("w", encoding="utf-8") as fh:
            for i in range(5000):
                fh.write(json.dumps({"type": "noise", "ts": "2026-10-31T00:00:00",
                                     "payload": {"task_id": f"noise{i}"}}) + "\n")
            fh.write(json.dumps({"type": "task_started", "ts": "2026-10-31T11:00:00",
                                 "payload": {"task_id": "target123456", "node_id": "child_coder"}}) + "\n")

        def _request(method, url, **kwargs):  # pragma: no cover
            raise AssertionError("local event queries must not call the API")

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "task_events", "task_id": "target123456"},
            self._env(), request=_request)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["data"]["payload"]["count"], 1)
        self.assertEqual(payload["data"]["payload"]["events"][0]["type"], "task_started")

    def test_task_events_reads_local_log_from_injected_workspace(self) -> None:
        def _request(method, url, **kwargs):  # pragma: no cover
            raise AssertionError("local event queries must not call the API")

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "task_events", "task_id": "53cf8e69"}, self._env(), request=_request)

        self.assertEqual(code, 0, payload)
        body = payload["data"]["payload"]
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["events"][-1]["status"], "completed")
        self.assertEqual(body["events"][0]["ts"], "2026-10-31T10:00:00")

    def test_recent_events_filters_by_node_without_api_call(self) -> None:
        def _request(method, url, **kwargs):  # pragma: no cover
            raise AssertionError("read-only event queries must not call the API")

        code, payload, _ = _invoke(
            DEBUG_TOOL,
            {"action": "recent_events", "node_id": "child_coder", "limit": 2},
            self._env(),
            request=_request,
        )

        self.assertEqual(code, 0, payload)
        body = payload["data"]["payload"]
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["events"]), 2)

    def test_node_status_and_active_tasks(self) -> None:
        def _request(method, url, **kwargs):
            return _Resp(200, {"tasks": {"running": 0}})

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "node_status", "node_id": "child_coder"}, self._env(), request=_request)
        self.assertEqual(code, 0, payload)
        body = payload["data"]["payload"]
        self.assertEqual(body["node_id"], "child_coder")
        self.assertEqual(body["active_tasks"], [], "completed node tasks are not active")

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "active_tasks"}, self._env(), request=_request)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["data"]["payload"]["active_count"], 0)

    def test_path_validation_blocks_url_override(self) -> None:
        def _request(method, url, **kwargs):  # pragma: no cover
            raise AssertionError("invalid paths must not reach the network")

        for bad in ("https://evil.example/v1/health", "//evil/v1", "/v1/health\nX: y", "v1/health"):
            code, payload, _ = _invoke(
                DEBUG_TOOL, {"action": "api", "path": bad}, self._env(), request=_request)
            self.assertEqual(code, 1, f"{bad!r} must be rejected")
            self.assertFalse(payload["ok"])

    def test_method_allowlist(self) -> None:
        def _request(method, url, **kwargs):  # pragma: no cover
            raise AssertionError("invalid methods must not reach the network")

        code, payload, _ = _invoke(
            DEBUG_TOOL, {"action": "api", "path": "/v1/health", "method": "PATCH"},
            self._env(), request=_request)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("method must be one of", payload["error"])

    def test_unknown_action_fails(self) -> None:
        code, payload, _ = _invoke(DEBUG_TOOL, {"action": "drop_database"}, self._env())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_no_hardcoded_production_event_path(self) -> None:
        source = DEBUG_TOOL.read_text(encoding="utf-8")
        self.assertNotIn("EVENTS_FILE = \"data/events.jsonl\"", source)
        self.assertNotIn("open(\"data/events.jsonl\"", source)
        self.assertIn("CLONOTH_WORKSPACE", source)
        self.assertNotIn("CLONOTH_API_TOKEN", source)

# ── supervisor reset backend (fake state, real methods) ──────────────────────

_BOUND_METHODS = (
    "reset_conversation",
    "_cleanup_branch_locked",
    "_cancel_task_for_reset_locked",
    "_task_terminal",
    "_purge_context_usage_for_session",
    "_remove_child_mapping_for_session_locked",
    "_delete_conversation_session_locked",
    "_ensure_entry_branch_indexes_locked",
    "_entry_branch_ids_for_parent_locked",
    "_is_entry_branch_session_locked",
    "_event_task_snapshot",
    "_event_payload_for_task",
    "_cleanup_tool_result_artifacts_for_task",
)


class _RegistryStore:
    def __init__(self) -> None:
        self._registry: dict[str, dict] = {}
        self.resets: list[str] = []

    def _flush(self) -> None:  # pragma: no cover - flush bookkeeping only
        pass

    def remove_session(self, session_id: str) -> None:
        self._registry.pop(session_id, None)

    def remove_sessions(self, session_ids) -> int:
        # [2026-11-02] 适配真实 SessionStore 接口：_cleanup_branch_locked 会调用
        # remove_sessions(批量)，fake 只做等价的批量 pop（不引入生产兼容分支）。
        removed = 0
        for sid in list(session_ids or ()):
            if self._registry.pop(str(sid), None) is not None:
                removed += 1
        return removed

    def on_session_reset(self, session_id: str) -> None:
        self.resets.append(session_id)


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, **kwargs) -> dict:
        self.events.append(kwargs)
        return {"seq": len(self.events)}


def _now():
    return datetime.now(timezone.utc)


def _task(task_id: str, session_id: str, status: TaskStatus, **input_data) -> Task:
    ts = _now()
    return Task(
        task_id=task_id,
        session_id=session_id,
        kind=TaskKind.node,
        status=status,
        input=dict(input_data),
        created_at=ts,
        updated_at=ts,
        lease_expires_at=ts + timedelta(seconds=30),
    )


def _make_state(tmp: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        workspace_root=tmp,
        sessions={},
        session_generations={},
        tasks={},
        conversation_map={},
        child_session_map={},
        parent_children={},
        parent_entry_branches={},
        entry_branch_parents={},
        _cancelled_sessions=set(),
        _session_context_usage={},
        _session_store=_RegistryStore(),
        eventlog=_EventLog(),
        approvals={},
        _async_tools={},
        lock=__import__("threading").RLock(),
    )
    state._lock = state.lock
    for name in _BOUND_METHODS:
        setattr(state, name, types.MethodType(getattr(SupervisorState, name), state))
    return state


def _conv_file(ws: Path, sid: str) -> Path:
    return ws / "data" / "conversations" / f"{sid}.jsonl"


def _write_conv(ws: Path, sid: str) -> Path:
    path = _conv_file(ws, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "message", "role": "assistant", "content": "ok"}\n', encoding="utf-8")
    return path


def _add_main(state: SimpleNamespace, ws: Path, sid: str = "sess_main", key: str = "discord:1") -> str:
    state.conversation_map[key] = sid
    state.sessions[sid] = SimpleNamespace(session_id=sid)
    state.session_generations[sid] = 1
    state._session_store._registry[sid] = {
        "session_id": sid, "channel": "discord", "conversation_key": key, "reset": False,
    }
    _write_conv(ws, sid)
    return sid


def _add_branch(state: SimpleNamespace, ws: Path, branch_sid: str, parent_sid: str = "sess_main") -> str:
    state.sessions[branch_sid] = SimpleNamespace(session_id=branch_sid)
    state.session_generations[branch_sid] = 1
    state.parent_entry_branches.setdefault(parent_sid, set()).add(branch_sid)
    state.entry_branch_parents[branch_sid] = parent_sid
    state._session_store._registry[branch_sid] = {
        "session_id": branch_sid,
        "channel": "discord",
        "conversation_key": "discord:1",
        "node_id": "__entry_branch__",
        "parent_session_id": parent_sid,
        "reset": False,
    }
    _write_conv(ws, branch_sid)
    return branch_sid


def _set_tasks(state: SimpleNamespace, tasks: list[Task]) -> None:
    state.tasks = {t.task_id: t for t in tasks}


class ResetConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_excluded_task_and_its_branch_survive_while_others_are_cancelled(self) -> None:
        main = _add_main(self.state, self.ws)
        caller_branch = _add_branch(self.state, self.ws, "branch_caller")
        other_branch = _add_branch(self.state, self.ws, "branch_other")
        _set_tasks(self.state, [
            # the task that is executing clear_context, inside an entry branch
            _task("t_clear", caller_branch, TaskStatus.running),
            # a sibling branch task and an old task on the main session
            _task("t_other", other_branch, TaskStatus.running),
            _task("t_old", main, TaskStatus.pending),
        ])

        result = self.state.reset_conversation(
            conversation_key="discord:1",
            exclude_session_id=caller_branch,
            exclude_task_id="t_clear",
        )

        self.assertTrue(result["ok"])
        # existing UI/consumer fields are preserved
        for key in ("old_session_id", "conversation_key", "cleared_children", "cancelled_tasks"):
            self.assertIn(key, result)
        self.assertEqual(result["old_session_id"], main)
        self.assertGreaterEqual(result["cancelled_tasks"], 1)

        # conversation mapping + history of the reset target are gone
        self.assertNotIn("discord:1", self.state.conversation_map)
        self.assertFalse(_conv_file(self.ws, main).exists())

        # the excluded task keeps running, and so does the branch it lives in
        self.assertEqual(self.state.tasks["t_clear"].status, TaskStatus.running)
        self.assertFalse(self.state.tasks["t_clear"].cancel_requested)
        self.assertIn(caller_branch, self.state.sessions)
        self.assertTrue(_conv_file(self.ws, caller_branch).exists())
        self.assertIn(caller_branch, self.state._session_store._registry)
        self.assertIn(caller_branch, self.state._skip_merge_sessions)

        # everything else is cancelled / cleaned. Running tasks get a cancel request
        # (the worker observes it); pending tasks land terminal immediately.
        self.assertEqual(self.state.tasks["t_old"].status, TaskStatus.cancelled)
        self.assertTrue(self.state.tasks["t_other"].cancel_requested)
        self.assertTrue(self.state.tasks["t_other"].input.get("_branch_skipped_merge"))
        self.assertNotIn(other_branch, self.state.sessions)
        self.assertFalse(_conv_file(self.ws, other_branch).exists())
        self.assertNotIn(other_branch, self.state._session_store._registry)
        self.assertEqual(self.state._session_store.resets, [main])

    def test_exclude_session_id_alone_protects_the_branch(self) -> None:
        main = _add_main(self.state, self.ws)
        caller_branch = _add_branch(self.state, self.ws, "branch_caller")
        other_branch = _add_branch(self.state, self.ws, "branch_other")
        _set_tasks(self.state, [_task("t_other", other_branch, TaskStatus.running)])

        result = self.state.reset_conversation(
            conversation_key="discord:1", exclude_session_id=caller_branch)

        self.assertTrue(result["ok"])
        self.assertIn(caller_branch, self.state.sessions)
        self.assertTrue(_conv_file(self.ws, caller_branch).exists())
        self.assertNotIn(other_branch, self.state.sessions)
        self.assertFalse(_conv_file(self.ws, other_branch).exists())
        self.assertFalse(_conv_file(self.ws, main).exists())

    def test_caller_chain_upstream_task_is_kept(self) -> None:
        main = _add_main(self.state, self.ws)
        caller_branch = _add_branch(self.state, self.ws, "branch_caller")
        parent_task = _task("t_parent", main, TaskStatus.running)
        child_task = _task("t_clear", caller_branch, TaskStatus.running)
        child_task.caller_task_id = "t_parent"
        _set_tasks(self.state, [parent_task, child_task])

        result = self.state.reset_conversation(
            conversation_key="discord:1",
            exclude_session_id=caller_branch,
            exclude_task_id="t_clear",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(self.state.tasks["t_parent"].status, TaskStatus.running)
        self.assertEqual(self.state.tasks["t_clear"].status, TaskStatus.running)
        self.assertEqual(result["cancelled_tasks"], 0)

    def test_without_exclusions_other_tasks_still_get_cancelled(self) -> None:
        main = _add_main(self.state, self.ws)
        other_branch = _add_branch(self.state, self.ws, "branch_other")
        _set_tasks(self.state, [
            _task("t_running", main, TaskStatus.running),
            _task("t_branch", other_branch, TaskStatus.running),
            _task("t_done", main, TaskStatus.completed),
        ])

        result = self.state.reset_conversation(conversation_key="discord:1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["cancelled_tasks"], 1, "completed tasks are not re-counted")
        self.assertTrue(self.state.tasks["t_running"].cancel_requested)
        self.assertTrue(self.state.tasks["t_running"].input.get("_branch_skipped_merge"))
        self.assertTrue(self.state.tasks["t_branch"].cancel_requested)
        self.assertEqual(self.state.tasks["t_done"].status, TaskStatus.completed)
        self.assertNotIn(other_branch, self.state.sessions)

    def test_dispatch_sessions_are_cascaded_and_exempted(self) -> None:
        main = _add_main(self.state, self.ws)
        # dispatch session A is plain, B belongs to the excluded caller task
        self.state.conversation_map["agent:child:discord:1"] = "sess_dispatch"
        self.state._session_store._registry["sess_dispatch"] = {
            "session_id": "sess_dispatch", "channel": "internal",
            "parent_session_id": main, "node_id": "child",
        }
        self.state.sessions["sess_dispatch"] = SimpleNamespace(session_id="sess_dispatch")
        _write_conv(self.ws, "sess_dispatch")

        self.state.conversation_map["agent:helper:discord:1:uuid"] = "sess_kept"
        self.state._session_store._registry["sess_kept"] = {
            "session_id": "sess_kept", "channel": "internal",
            "parent_session_id": main, "node_id": "helper",
        }
        self.state.sessions["sess_kept"] = SimpleNamespace(session_id="sess_kept")
        _write_conv(self.ws, "sess_kept")
        _set_tasks(self.state, [_task("t_clear", "sess_kept", TaskStatus.running)])

        result = self.state.reset_conversation(
            conversation_key="discord:1", exclude_task_id="t_clear")

        self.assertTrue(result["ok"])
        self.assertNotIn("agent:child:discord:1", self.state.conversation_map)
        self.assertFalse(_conv_file(self.ws, "sess_dispatch").exists())
        self.assertNotIn("sess_dispatch", self.state.sessions)
        # the excluded task's dispatch session keeps its history
        self.assertNotIn("agent:helper:discord:1:uuid", self.state.conversation_map)
        self.assertTrue(_conv_file(self.ws, "sess_kept").exists())
        self.assertIn("sess_kept", self.state.sessions)
        self.assertEqual(self.state.tasks["t_clear"].status, TaskStatus.running)

    def test_unknown_conversation_returns_not_ok(self) -> None:
        result = self.state.reset_conversation(conversation_key="discord:none")
        self.assertFalse(result["ok"])
        self.assertIn("conversation not found", result["error"])


class _CallerCompletionHooks:
    def __init__(self) -> None:
        self.fired: list[tuple] = []

    def fire(self, name, ctx=None):
        self.fired.append((name, ctx))
        raise AssertionError("post-hook must not fire on a reset-skipped branch")


_FINALIZE_METHODS = (
    "_finalize_branch_task_locked",
    "_route_completed_task_inner_locked",
    "_is_compact_dispatch_result",
    "_is_turn_summary_result",
    "_cleanup_branch_locked",
    "_merge_branch_locked",
    "_merge_branch_transcript_locked",
)


class CallerCompletionAfterResetTests(unittest.TestCase):
    """reset 之后调用链自己走到终态时不得把旧历史 merge 回来。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        self.state = _make_state(self.ws)
        for name in _FINALIZE_METHODS:
            setattr(self.state, name, types.MethodType(getattr(SupervisorState, name), self.state))
        self.state.hook_registry = _CallerCompletionHooks()
        self.state._maybe_trigger_turn_summary_locked = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("turn summary must not be triggered on a reset-skipped branch"))

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed_branch_history(self, branch: str) -> None:
        path = _conv_file(self.ws, branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for i in range(2):
                fh.write(json.dumps({
                    "id": f"m{i}", "role": "assistant", "content": f"old-{i}",
                    "created_at": f"2026-10-31T10:00:0{i}",
                }) + "\n")

    def test_caller_completion_does_not_resurrect_reset_history(self) -> None:
        main = _add_main(self.state, self.ws)
        branch = _add_branch(self.state, self.ws, "branch_caller")
        self._seed_branch_history(branch)
        task = _task(
            "t_clear", branch, TaskStatus.running,
            parent_session_id=main, branch_session_id=branch,
        )
        _set_tasks(self.state, [task])

        result = self.state.reset_conversation(
            conversation_key="discord:1",
            exclude_session_id=branch,
            exclude_task_id="t_clear",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(_conv_file(self.ws, main).exists(), "reset cleared the parent history")
        self.assertIn(branch, self.state._skip_merge_sessions, "reset registered the kept branch")

        # the caller chain now reaches a terminal state by itself
        task.status = TaskStatus.completed
        task.result = {"action": "cancelled"}
        self.state._route_completed_task_inner_locked(task)

        self.assertTrue(task.input.get("_branch_finalized"))
        self.assertTrue(task.input.get("_branch_skipped_merge"), "skip flag must be consumed onto the task")
        self.assertNotIn(branch, self.state._skip_merge_sessions, "consumed flag must not accumulate")
        self.assertEqual(self.state.hook_registry.fired, [], "memory extraction must not run on the reset conversation")
        self.assertFalse(_conv_file(self.ws, main).exists(),
                         "old branch history must not be merged back into the reset conversation")
        self.assertFalse(_conv_file(self.ws, branch).exists(), "finalize still cleans the finished branch")

    def test_finalize_consumes_cancelled_task_flag(self) -> None:
        main = _add_main(self.state, self.ws)
        branch = _add_branch(self.state, self.ws, "branch_other")
        self._seed_branch_history(branch)
        task = _task(
            "t_other", branch, TaskStatus.running,
            parent_session_id=main, branch_session_id=branch,
        )
        _set_tasks(self.state, [task])
        self.state.reset_conversation(conversation_key="discord:1")
        # the cancelled task carries the flag written by _cancel_task_for_reset_locked
        self.assertTrue(task.input.get("_branch_skipped_merge"))

        self.state._finalize_branch_task_locked(task, merge=True)

        self.assertFalse(_conv_file(self.ws, main).exists(),
                         "a reset-cancelled branch must not merge back")

    def test_plain_branch_finish_still_merges(self) -> None:
        main = _add_main(self.state, self.ws)
        branch = _add_branch(self.state, self.ws, "branch_plain")
        self._seed_branch_history(branch)
        task = _task(
            "t_plain", branch, TaskStatus.running,
            parent_session_id=main, branch_session_id=branch,
        )
        _set_tasks(self.state, [task])
        task.status = TaskStatus.completed

        self.state._finalize_branch_task_locked(task, merge=True)

        self.assertFalse(task.input.get("_branch_skipped_merge"))
        self.assertEqual(task.input.get("_branch_merged_count"), 2)
        self.assertTrue(_conv_file(self.ws, main).exists(), "normal branch finish still merges")


# ── reset endpoint over ASGI (fake app.state.state, real route + auth) ───────


class ResetEndpointAsgiTests(unittest.TestCase):
    """Real create_app + real reset route; only app.state.state is a fake."""

    @classmethod
    def setUpClass(cls) -> None:
        import supervisor.admin_api as admin_api
        from supervisor.api import create_app

        cls._saved_token = admin_api._admin_token
        cls._saved_web_auth = admin_api._web_auth
        admin_api._admin_token = ADMIN_TOKEN
        admin_api._web_auth = None
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls._tmp.name)
        state = _make_state(cls.ws)
        # attributes create_app touches at build time
        state.bump_tools_reload = lambda: 0
        with contextlib.redirect_stdout(io.StringIO()):
            cls.app = create_app(state=state, process_manager=None, config_store=None)
        cls.state = state

    @classmethod
    def tearDownClass(cls) -> None:
        import supervisor.admin_api as admin_api

        admin_api._admin_token = cls._saved_token
        admin_api._web_auth = cls._saved_web_auth
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.state.conversation_map.clear()
        self.state.sessions.clear()
        self.state.session_generations.clear()
        self.state.parent_children.clear()
        self.state.parent_entry_branches.clear()
        self.state.entry_branch_parents.clear()
        self.state._session_store._registry.clear()
        self.state._session_store.resets.clear()
        self.state.tasks.clear()
        self.state.__dict__.pop("_skip_merge_sessions", None)

    async def _post(self, body: dict, *, token: str | None = ADMIN_TOKEN) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://supervisor.test") as client:
            return await client.post("/v1/conversations/reset", json=body, headers=headers)

    def test_endpoint_forwards_exclusions_and_keeps_caller_branch(self) -> None:
        import asyncio

        main = _add_main(self.state, self.ws)
        caller_branch = _add_branch(self.state, self.ws, "branch_caller")
        other_branch = _add_branch(self.state, self.ws, "branch_other")
        _set_tasks(self.state, [
            _task("t_clear", caller_branch, TaskStatus.running),
            _task("t_other", other_branch, TaskStatus.pending),
        ])

        resp = asyncio.run(self._post({
            "conversation_key": "discord:1",
            "exclude_session_id": caller_branch,
            "exclude_task_id": "t_clear",
        }))

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["old_session_id"], main)
        self.assertEqual(body["conversation_key"], "discord:1")
        self.assertIn("cleared_children", body)
        self.assertIn("cancelled_tasks", body)
        self.assertIn("context_files_cleaned", body, "endpoint-level cleanup field must stay")
        self.assertEqual(self.state.tasks["t_clear"].status, TaskStatus.running)
        self.assertTrue(_conv_file(self.ws, caller_branch).exists())
        self.assertFalse(_conv_file(self.ws, other_branch).exists())

    def test_endpoint_requires_auth_and_conversation_key(self) -> None:
        import asyncio

        _add_main(self.state, self.ws)
        resp = asyncio.run(self._post({"conversation_key": "discord:1"}, token=None))
        self.assertEqual(resp.status_code, 401)
        resp = asyncio.run(self._post({"conversation_key": ""}))
        self.assertEqual(resp.status_code, 400)
        resp = asyncio.run(self._post({"conversation_key": "discord:missing"}))
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
