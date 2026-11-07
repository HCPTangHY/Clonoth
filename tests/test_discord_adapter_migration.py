"""Discord adapter / SDK migration regression tests; no real network access.

Covers the production-branch behaviours migrated into this repo:
  * channel context watermark only advances after a successful send
    (shared watermark can never regress under concurrency)
  * sent message id / attachment marker recorded into channel history
  * key_roles identity trimming + safe defaults for empty allowlist
  * BotConfig.workspace_root / extra_roots (+ positional compatibility)
  * SDK HealthInfo.workspace_root old/new supervisor compatibility
  * response_info notices go through a standalone callback, never into body/history
  * existing dual-Gemini `!model provider` help stays intact

Run: python3.11 -B tests/test_discord_adapter_migration.py
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState, TriggerInfo
from clonoth_sdk.types import Event, HealthInfo

from adapters.discord import app as app_mod
from adapters.discord import callbacks as dc_callbacks
from adapters.discord.callbacks import DiscordCallbacks
from adapters.discord.context import (
    _format_identity,
    _format_member_entry,
    _get_role_names,
)
from adapters.discord.messaging import _record_bot_reply


REACT_PATTERN = __import__("re").compile(r"\[REACT:(.*?)\]")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
#  Fake Discord / runtime objects (no gateway, no HTTP)
# ---------------------------------------------------------------------------


class _Role:
    def __init__(self, name: str) -> None:
        self.name = name


class _Member:
    def __init__(self, name="alice", display_name="Alice", roles=(), uid=1):
        self.name = name
        self.display_name = display_name
        self.roles = [_Role(r) if isinstance(r, str) else r for r in roles]
        self.id = uid


class _FakeChannel:
    def __init__(self, cid: int = 1) -> None:
        self.id = cid
        self.send = AsyncMock(return_value=_FakeMessage(cid * 1000 + 1, self))

    def typing(self):
        return _AsyncNull()


class _AsyncNull:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeMessage:
    def __init__(self, mid: int = 1, channel=None) -> None:
        self.id = mid
        self.channel = channel if channel is not None else _FakeChannel()
        # 延迟创建：reply 结果用 SimpleNamespace，避免 message ↔ reply 递归构造。
        self.reply = AsyncMock(
            return_value=SimpleNamespace(id=mid + 1, channel=self.channel),
        )


class _FakeRuntime:
    """Minimal stand-in for DiscordRuntime (only what callbacks touch)."""

    def __init__(self, session_state: SessionState | None = None) -> None:
        self.workspace = Path("/tmp/clonoth-test-workspace")
        self.channel_history: dict[int, list[dict]] = {}
        self.history_seq_counter = 0
        self.history_max_len = 15
        self.session_state = session_state if session_state is not None else SessionState()
        self.react_pattern = REACT_PATTERN
        self.restart_signal = "[BOT_RESTART]"
        self.split_signal = "[SPLIT]"
        self.entry_node_id = "main"
        self.child_node_display_names: dict[str, str] = {}
        self.agent_log_channel_id = 0
        self.bot_restart_view_factory = None
        self.superusers: set[int] = set()
        self.key_roles: set[str] = set()
        self.bridge_port = 8768
        self.dc_client = MagicMock()
        self.dc_client.get_channel = MagicMock(return_value=None)
        self.dc_client.fetch_channel = AsyncMock(return_value=None)


def _trigger(seq: int, channel_id: int, watermark, *, sid="sess-1") -> TriggerInfo:
    platform_data = {
        "message": _FakeMessage(mid=100 + seq),
        "channel_id": channel_id,
        "status_msg": None,
    }
    if watermark is not None:
        platform_data["context_watermark"] = watermark
    return TriggerInfo(
        inbound_seq=seq,
        conversation_key=f"discord:{channel_id}",
        session_id=sid,
        is_dm=False,
        platform_data=platform_data,
    )


# ---------------------------------------------------------------------------
#  Watermark: only a successful send may advance channel context
# ---------------------------------------------------------------------------


class SendWatermarkTests(unittest.TestCase):
    def test_failed_send_does_not_advance_watermark(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        trigger = _trigger(7, 1, 42)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text",
                AsyncMock(side_effect=RuntimeError("discord down")),
            ):
                await cb.send_reply(trigger, "hello", [])

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(1), -1)
        self.assertFalse(rt.channel_history.get(1))

    def test_successful_send_advances_watermark_and_records_msg_id(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        trigger = _trigger(7, 1, 42)
        sent = _FakeMessage(mid=555)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text", AsyncMock(return_value=[sent]),
            ):
                await cb.send_reply(trigger, "hello", [])

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(1), 42)
        entry = rt.channel_history[1][-1]
        self.assertEqual(entry["msg_id"], 555)
        self.assertTrue(entry["is_bot"])
        self.assertIn("hello", entry["text"])

    def test_intermediate_reply_advances_watermark_and_records_msg_id(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        trigger = _trigger(8, 3, 17)
        sent = _FakeMessage(mid=777)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text", AsyncMock(return_value=[sent]),
            ):
                await cb.send_intermediate_reply(trigger, "partial", [])

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(3), 17)
        self.assertEqual(rt.channel_history[3][-1]["msg_id"], 777)

    def test_attachment_only_send_records_attachment_marker(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        trigger = _trigger(9, 4, 7)
        sent = _FakeMessage(mid=888)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text", AsyncMock(return_value=[sent]),
            ):
                # Attachment path does not exist on disk -> no discord.File, send still happens.
                await cb.send_reply(trigger, "", [{"path": "missing.png", "name": "missing.png"}])

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(4), 7)
        entry = rt.channel_history[4][-1]
        self.assertIn("[附件]", entry["text"])
        self.assertEqual(entry["msg_id"], 888)

    def test_trigger_without_context_watermark_keeps_watermark(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        trigger = _trigger(10, 5, None)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text",
                AsyncMock(return_value=[_FakeMessage(mid=999)]),
            ):
                await cb.send_reply(trigger, "legacy trigger", [])

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(5), -1)
        self.assertEqual(rt.channel_history[5][-1]["msg_id"], 999)

    def test_concurrent_older_watermark_never_regresses(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)

        async def scenario():
            with patch.object(
                dc_callbacks, "_send_split_text",
                AsyncMock(return_value=[_FakeMessage(mid=1)]),
            ):
                newer = cb.send_reply(_trigger(20, 6, 50), "newer", [])
                older = cb.send_reply(_trigger(21, 6, 30), "older", [])
                await asyncio.gather(newer, older)

        _run(scenario())
        self.assertEqual(rt.session_state.get_high_watermark(6), 50)

        # Same guarantee at the helper level, including a late (stale) update.
        _record_bot_reply(rt, 6, "late", msg_id=3, context_watermark=10)
        self.assertEqual(rt.session_state.get_high_watermark(6), 50)


# ---------------------------------------------------------------------------
#  key_roles identity trimming
# ---------------------------------------------------------------------------


class KeyRolesTests(unittest.TestCase):
    def test_empty_allowlist_keeps_no_role_tag(self):
        member = _Member(roles=["Admin", "Guild"])
        self.assertEqual(_get_role_names(member), [])
        self.assertEqual(_get_role_names(member, set()), [])
        self.assertEqual(_format_identity(member, set()), "Alice")
        self.assertNotIn("Admin", _format_member_entry(member, "hi"))

    def test_allowlist_keeps_only_listed_roles(self):
        member = _Member(roles=["Admin", "Guild", "@everyone"])
        self.assertEqual(_get_role_names(member, {"Guild"}), ["Guild"])
        self.assertEqual(_format_identity(member, {"Guild"}), "Alice(Guild)")
        entry = _format_member_entry(member, "hi", key_roles={"Admin"})
        self.assertEqual(entry, "Alice(Admin): hi")

    def test_identity_dedupes_username_and_marks_admin(self):
        same = _Member(name="alice", display_name="Alice")
        self.assertEqual(_format_identity(same), "Alice")
        self.assertEqual(_format_identity(same, is_admin=True), "Alice ✓ADMIN")
        other = _Member(name="a1", display_name="Alice")
        self.assertEqual(_format_identity(other), "Alice[a1]")


# ---------------------------------------------------------------------------
#  response_info notices: standalone callback only
# ---------------------------------------------------------------------------


class ResponseNoticeTests(unittest.TestCase):
    def _router(self, callbacks):
        state = SessionState()
        state.register_session("discord:1", "sess-1")
        state.register_trigger(_trigger(5, 1, 11))
        return EventRouter(
            AsyncMock(), state, callbacks,
            BotConfig(base_url="http://supervisor.test", entry_node_id="main"),
        )

    @staticmethod
    def _event():
        return Event(
            seq=1, event_id="e1", ts="", run_id="r1",
            session_id="sess-1", component="engine", type="outbound_message",
            payload={
                "source_inbound_seq": 5,
                "text": "正文内容",
                "response_info": {"notices": [{"code": "notice", "text": "说明文字"}]},
            },
        )

    def test_notice_is_sent_separately_and_never_joins_body(self):
        cb = MagicMock()
        cb.send_reply = AsyncMock()
        cb.send_response_notice = AsyncMock()
        router = self._router(cb)

        _run(router._handle_outbound_message(self._event()))

        reply_text = cb.send_reply.await_args.args[1]
        self.assertEqual(reply_text, "正文内容")
        self.assertNotIn("说明文字", reply_text)

        cb.send_response_notice.assert_awaited_once()
        trigger, conv_key, info = cb.send_response_notice.await_args.args
        self.assertEqual(conv_key, "discord:1")
        self.assertEqual(info["notices"][0]["text"], "说明文字")
        self.assertEqual(trigger.inbound_seq, 5)

    def test_legacy_adapter_without_notice_callback_is_tolerated(self):
        class _LegacyCallbacks:
            def __init__(self):
                self.send_reply = AsyncMock()
                self.send_to_channel = AsyncMock()

        cb = _LegacyCallbacks()
        router = self._router(cb)
        _run(router._handle_outbound_message(self._event()))
        cb.send_reply.assert_awaited_once()

    def test_discord_callback_renders_embed_outside_history(self):
        rt = _FakeRuntime()
        cb = DiscordCallbacks(rt)
        channel = _FakeChannel(1)
        trigger = _trigger(6, 1, 3)
        trigger.platform_data["message"] = _FakeMessage(mid=9, channel=channel)

        _run(cb.send_response_notice(trigger, "discord:1", {"notices": [{"text": "**note**"}]}))

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("embed", kwargs)
        self.assertIn("note", kwargs["embed"].description)
        # Notices must never be recorded as model-visible channel history.
        self.assertFalse(rt.channel_history)


# ---------------------------------------------------------------------------
#  SDK health: old/new supervisor compatibility
# ---------------------------------------------------------------------------

OLD_HEALTH = {"status": "ok", "run_id": "r1", "started_at": "2026-01-01T00:00:00", "uptime_seconds": 1.5}


def _client_with(payload: dict) -> ClonothClient:
    client = ClonothClient("http://supervisor.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    return client


class HealthCompatTests(unittest.TestCase):
    def test_healthinfo_workspace_root_defaults_to_empty(self):
        info = HealthInfo("ok", "r1", "t0", 1.0)
        self.assertEqual(info.workspace_root, "")

    def test_old_supervisor_without_workspace_root(self):
        client = _client_with(dict(OLD_HEALTH))

        async def scenario():
            try:
                return await client.get_health()
            finally:
                await client._client.aclose()

        info = _run(scenario())
        self.assertEqual(info.workspace_root, "")
        self.assertEqual(info.status, "ok")
        self.assertEqual(info.run_id, "r1")

    def test_new_supervisor_with_workspace_root(self):
        client = _client_with({**OLD_HEALTH, "workspace_root": "/srv/clonoth"})

        async def scenario():
            try:
                return await client.get_health()
            finally:
                await client._client.aclose()

        self.assertEqual(_run(scenario()).workspace_root, "/srv/clonoth")

    def test_non_string_workspace_root_is_ignored(self):
        client = _client_with({**OLD_HEALTH, "workspace_root": None})

        async def scenario():
            try:
                return await client.get_health()
            finally:
                await client._client.aclose()

        self.assertEqual(_run(scenario()).workspace_root, "")

    def test_set_admin_token_rebinds_shared_client(self):
        client = ClonothClient("http://supervisor.test")
        client.set_admin_token("token-a", "/tmp/does-not-exist/.admin_token")
        self.assertEqual(client._resolve_admin_token(), "token-a")
        self.assertEqual(client._admin_token_path, "/tmp/does-not-exist/.admin_token")


# ---------------------------------------------------------------------------
#  BotConfig / adapter defaults
# ---------------------------------------------------------------------------


class BotConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = BotConfig(base_url="http://x")
        self.assertIsNone(cfg.workspace_root)
        self.assertEqual(cfg.extra_roots, [])
        self.assertFalse(cfg.auto_approve_internal)

    def test_positional_seventh_argument_stays_auto_approve(self):
        # 旧代码按位置传第 7 个参数（审批布尔），新字段必须不抢占该位置。
        cfg = BotConfig("http://x", None, "main", "discord", 0.8, 50, True)
        self.assertIs(cfg.auto_approve_internal, True)
        self.assertIsNone(cfg.workspace_root)
        self.assertEqual(cfg.extra_roots, [])

    def test_explicit_workspace_and_extra_roots(self):
        cfg = BotConfig(
            base_url="http://x",
            workspace_root=Path("/srv/clonoth"),
            extra_roots=[Path("/srv/shared")],
            auto_approve_internal=True,
        )
        self.assertEqual(cfg.workspace_root, Path("/srv/clonoth"))
        self.assertEqual(cfg.extra_roots, [Path("/srv/shared")])
        self.assertTrue(cfg.auto_approve_internal)

    def test_adapter_defaults_stay_empty_and_local(self):
        env_keys = (
            "DISCORD_TOKEN", "DISCORD_SUPERUSERS", "DISCORD_LOG_CHANNEL",
            "DISCORD_ENTRY_NODE", "DISCORD_KEY_ROLES",
        )
        backup = {k: os.environ.pop(k, None) for k in env_keys}
        try:
            reloaded = importlib.reload(app_mod)
            self.assertEqual(reloaded._DEFAULT_DISCORD_TOKEN, "")
            self.assertEqual(reloaded.SUPERUSERS, set())
            self.assertEqual(reloaded.AGENT_LOG_CHANNEL_ID, 0)
            self.assertEqual(reloaded._ENTRY_NODE_ID, "main")
            self.assertEqual(reloaded._parse_key_roles(""), set())
            self.assertEqual(reloaded._parse_key_roles(" a, ,b "), {"a", "b"})
        finally:
            for key, value in backup.items():
                if value is not None:
                    os.environ[key] = value
            importlib.reload(app_mod)


# ---------------------------------------------------------------------------
#  Workspace resolution ordering: workspace -> key_roles / policy / token
# ---------------------------------------------------------------------------


class WorkspaceResolutionTests(unittest.TestCase):
    def test_resolve_workspace_uses_health_when_env_unset(self):
        rt = SimpleNamespace(
            workspace=Path("/local/default"),
            clonoth_client=MagicMock(
                get_health=AsyncMock(return_value=HealthInfo("ok", "r", "t", 1.0, "/srv/remote")),
            ),
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLONOTH_WORKSPACE", None)
            _run(app_mod._resolve_workspace(rt))
        self.assertEqual(rt.workspace, Path("/srv/remote"))

    def test_resolve_workspace_keeps_default_for_old_supervisor(self):
        rt = SimpleNamespace(
            workspace=Path("/local/default"),
            clonoth_client=MagicMock(
                get_health=AsyncMock(return_value=HealthInfo("ok", "r", "t", 1.0, "")),
            ),
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLONOTH_WORKSPACE", None)
            _run(app_mod._resolve_workspace(rt))
        self.assertEqual(rt.workspace, Path("/local/default"))

    def test_resolve_workspace_respects_explicit_env(self):
        client = MagicMock(get_health=AsyncMock())
        rt = SimpleNamespace(workspace=Path("/local/default"), clonoth_client=client)
        with patch.dict(os.environ, {"CLONOTH_WORKSPACE": "/from/env"}, clear=False):
            _run(app_mod._resolve_workspace(rt))
        self.assertEqual(rt.workspace, Path("/local/default"))
        client.get_health.assert_not_awaited()

    def test_key_roles_and_extra_roots_read_from_effective_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "config").mkdir()
            (ws / "config" / "runtime.yaml").write_text(
                "supervisor:\n  process_manager:\n    discord_key_roles: \"Core Team, Observer\"\n",
                encoding="utf-8",
            )
            (ws / "data").mkdir()
            (ws / "data" / "policy.yaml").write_text(
                "extra_roots:\n  - /srv/shared\n  - data\n", encoding="utf-8",
            )
            rt = SimpleNamespace(workspace=ws, key_roles=set())
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DISCORD_KEY_ROLES", None)
                _run(app_mod._resolve_key_roles(rt))
            self.assertEqual(rt.key_roles, {"Core Team", "Observer"})
            self.assertEqual(
                app_mod._load_extra_roots(rt),
                [Path("/srv/shared"), (ws / "data").resolve()],
            )

    def test_env_key_roles_override_runtime_config(self):
        rt = SimpleNamespace(workspace=Path("/nonexistent"), key_roles={"env-role"})
        with patch.dict(os.environ, {"DISCORD_KEY_ROLES": "env-role"}, clear=False):
            _run(app_mod._resolve_key_roles(rt))
        self.assertEqual(rt.key_roles, {"env-role"})

    def test_initialize_sdk_reads_token_from_resolved_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "data").mkdir()
            (ws / "data" / ".admin_token").write_text("TOKEN-FROM-EFFECTIVE-WS", encoding="utf-8")

            client = MagicMock()
            client.get_health = AsyncMock(return_value=HealthInfo("ok", "r", "t", 1.0, str(ws)))
            client.set_admin_token = MagicMock()

            class _FakeRouter:
                def __init__(self, *args, **kwargs):
                    pass

                def set_raw_event_hook(self, hook):
                    pass

                async def run(self):
                    await asyncio.sleep(0)

            class _FakeCallbacks:
                def __init__(self, runtime):
                    self.raw_event_hook = lambda *a, **k: None

            rt = SimpleNamespace(
                workspace=Path("/wrong/instance"),
                base_url="http://supervisor.test",
                superusers=set(),
                entry_node_id="main",
                key_roles=set(),
                clonoth_client=None,
                bot_config=None,
                session_state=None,
                callbacks=None,
                event_router=None,
                router_task=None,
            )

            async def scenario():
                with patch.dict(os.environ, {}, clear=False), \
                        patch.object(app_mod, "ClonothClient", return_value=client), \
                        patch.object(app_mod, "_start_bridge", AsyncMock()), \
                        patch.object(app_mod, "DiscordCallbacks", _FakeCallbacks), \
                        patch.object(app_mod, "EventRouter", _FakeRouter):
                    os.environ.pop("CLONOTH_WORKSPACE", None)
                    await app_mod._initialize_sdk(rt)

            _run(scenario())
            self.assertEqual(rt.workspace, ws)
            token_arg, token_path = client.set_admin_token.call_args.args
            self.assertEqual(token_arg, "TOKEN-FROM-EFFECTIVE-WS")
            self.assertEqual(Path(token_path), ws / "data" / ".admin_token")
            self.assertEqual(rt.bot_config.workspace_root, ws)
            self.assertTrue(rt.bot_config.auto_approve_internal)


# ---------------------------------------------------------------------------
#  Existing dual-Gemini help must survive the migration
# ---------------------------------------------------------------------------


class ModelCommandHelpTests(unittest.TestCase):
    def test_model_provider_help_lists_both_gemini_channels(self):
        from adapters.discord.agent import handle_model_command

        message = SimpleNamespace(
            author=_Member(uid=42),
            channel=SimpleNamespace(id=1),
            reply=AsyncMock(),
        )
        state = MagicMock()
        state.get_session_id = MagicMock(return_value="sess-1")
        rt = SimpleNamespace(
            superusers={42},
            clonoth_client=MagicMock(),
            session_state=state,
            entry_node_id="main",
        )

        consumed = _run(handle_model_command(rt, message, "!model provider"))
        self.assertTrue(consumed)
        message.reply.assert_awaited_once()
        text = message.reply.await_args.args[0]
        self.assertIn("gemini-aistudio", text)
        self.assertIn("gemini-vertex", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
