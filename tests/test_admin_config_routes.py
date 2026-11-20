"""Offline regression checks for admin configuration, session, and WS routes."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class AdminConfigRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "data").mkdir(parents=True, exist_ok=True)
        (self.ws / "config" / "nodes").mkdir(parents=True, exist_ok=True)
        (self.ws / "tools").mkdir(parents=True, exist_ok=True)

        (self.ws / "data" / "config.yaml").write_text(
            "version: 1\n"
            "provider: gemini-vertex\n"
            "openai:\n"
            "  base_url: ''\n"
            "  api_key: ''\n"
            "  model: ''\n"
            "gemini-vertex:\n"
            "  base_url: https://zoaholic.example/v1\n"
            "  api_key: sk-secret-123456\n"
            "  model: gemini-4-argon\n",
            encoding="utf-8",
        )
        (self.ws / "data" / "schedules.yaml").write_text(
            "schedules:\n"
            "- id: legit_watch\n"
            "  cron: '8 * * * *'\n"
            "  type: script\n"
            "  command: python3 /opt/safe.py\n"
            "  enabled: true\n",
            encoding="utf-8",
        )

        self._env_patch = patch.dict(
            os.environ,
            {
                "CLONOTH_ADMIN_TOKEN": "test-machine-token-xyz",
                "CLONOTH_ALLOW_HTTP_SCRIPT_SCHEDULE": "",
                "CLONOTH_ALLOW_HTTP_TOOL_WRITE": "",
                "CLONOTH_ALLOW_HTTP_POLICY_WRITE": "",
                "CLONOTH_ALLOW_JWT_HIGH_RISK": "",
            },
            clear=False,
        )
        self._env_patch.start()

        import supervisor.admin_api as admin_api
        admin_api._admin_token = ""
        admin_api._web_auth = None

        from supervisor.config_store import ConfigStore
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState
        from supervisor.api import create_app

        self.st = SupervisorState(
            workspace_root=self.ws,
            eventlog=EventLog(self.ws / "data" / "events.jsonl", "cfg-test"),
            policy=PolicyEngine(workspace_root=self.ws),
        )
        self.cs = ConfigStore(path=self.ws / "data" / "config.yaml")
        self.app = create_app(state=self.st, process_manager=None, config_store=self.cs)
        self.client = TestClient(self.app)

        r = self.client.post(
            "/v1/admin/auth/setup",
            json={"username": "admin", "password": "StrongPassword!123"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.jwt = r.json()["token"]
        self.machine_hdr = {"Authorization": "Bearer test-machine-token-xyz"}
        self.jwt_hdr = {"Authorization": f"Bearer {self.jwt}"}

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_http_header_token_required(self) -> None:
        r = self.client.get("/v1/config?token=test-machine-token-xyz")
        self.assertEqual(r.status_code, 401)

    def test_config_public_reflects_active_non_openai_provider(self) -> None:
        r_pub = self.client.get("/v1/config", headers=self.jwt_hdr)
        self.assertEqual(r_pub.status_code, 200)
        body = r_pub.json()
        self.assertEqual(body["provider"], "gemini-vertex")
        self.assertEqual(body["openai"]["model"], "gemini-4-argon")
        self.assertEqual(body["openai"]["base_url"], "https://zoaholic.example/v1")
        self.assertTrue(body["openai"]["api_key_present"])
        self.assertEqual(body["openai"]["api_key"], "****3456")

        r_sec_jwt = self.client.get("/v1/config/openai/secret", headers=self.jwt_hdr)
        self.assertEqual(r_sec_jwt.status_code, 200)

        r_sec_mach = self.client.get("/v1/config/openai/secret", headers=self.machine_hdr)
        self.assertEqual(r_sec_mach.status_code, 200)

    def test_schedules_raw_update_validates_script_entries(self) -> None:
        invalid_direct = (
            "schedules:\n"
            "- id: bad_job\n"
            "  cron: '* * * * *'\n"
            "  type: script\n"
            "  command: rm -rf / --no-preserve-root\n"
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/schedules/raw", headers=self.jwt_hdr, json={"content": invalid_direct}).status_code,
            403,
        )

        invalid_piped = (
            "schedules:\n"
            "- id: media-index-check\n"
            "  cron: '* * * * *'\n"
            "  type: script\n"
            "  once: true\n"
            "  command: echo cm0gLXJmIC8= | base64 -d | sh\n"
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/schedules/raw", headers=self.jwt_hdr, json={"content": invalid_piped}).status_code,
            403,
        )

        good_yaml = (
            "schedules:\n"
            "- id: legit_watch\n"
            "  cron: '8 * * * *'\n"
            "  type: script\n"
            "  command: python3 /opt/safe.py\n"
            "  enabled: true\n"
            "- id: daily_ping\n"
            "  cron: '0 9 * * *'\n"
            "  type: message\n"
            "  text: 'hello'\n"
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/schedules/raw", headers=self.jwt_hdr, json={"content": good_yaml}).status_code,
            200,
        )

    def test_web_session_manages_config_runtime_policy_and_tools(self) -> None:
        self.assertEqual(
            self.client.put("/v1/admin/config/config/raw", headers=self.jwt_hdr, json={"content": "version: 1\n"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/runtime/raw", headers=self.jwt_hdr, json={"content": "timeout: 60\n"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/policy/raw", headers=self.jwt_hdr, json={"content": "version: 1\n"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/v1/admin/config/tools", headers=self.jwt_hdr, json={"id": "demo_tool", "content": "pass\n"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.put("/v1/admin/config/tools/demo_tool/raw", headers=self.jwt_hdr, json={"content": "x = 1\n"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete("/v1/admin/config/tools/demo_tool", headers=self.jwt_hdr).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/v1/admin/config/tools", json={"id": "x", "content": "pass"}).status_code,
            401,
        )

    def test_auth_session_lifecycle_and_password_update(self) -> None:
        r_no_old = self.client.post(
            "/v1/admin/auth/change-password",
            headers=self.jwt_hdr,
            json={"username": "admin", "new_password": "NewPassword!456"},
        )
        self.assertEqual(r_no_old.status_code, 400)

        r_ok_cp = self.client.post(
            "/v1/admin/auth/change-password",
            headers=self.jwt_hdr,
            json={
                "username": "admin",
                "old_password": "StrongPassword!123",
                "new_password": "NewPassword!456",
            },
        )
        self.assertEqual(r_ok_cp.status_code, 200)
        self.assertEqual(self.client.get("/v1/config", headers=self.jwt_hdr).status_code, 401)

        for _ in range(8):
            r_bad = self.client.post(
                "/v1/admin/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.55"},
            )
            self.assertEqual(r_bad.status_code, 401)
        r_429 = self.client.post(
            "/v1/admin/auth/login",
            json={"username": "admin", "password": "NewPassword!456"},
            headers={"X-Forwarded-For": "203.0.113.55"},
        )
        self.assertEqual(r_429.status_code, 429)

    def test_ws_initial_frame_handshake(self) -> None:
        with self.client.websocket_connect("/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": self.jwt})
            ws.send_json({"last_seq": 0})
            ws.close()

    def test_initial_setup_idempotency(self) -> None:
        import supervisor.admin_api as admin_api
        from supervisor.api import create_app

        (self.ws / "data" / "web_auth.json").unlink(missing_ok=True)
        admin_api._web_auth = None
        fresh_app = create_app(state=self.st, process_manager=None, config_store=self.cs)
        fresh_client = TestClient(fresh_app)

        r_first = fresh_client.post(
            "/v1/admin/auth/setup",
            headers={"X-Forwarded-For": "198.51.100.9"},
            json={"username": "owner", "password": "OwnerPassword1!"},
        )
        self.assertEqual(r_first.status_code, 200)

        r_second = fresh_client.post(
            "/v1/admin/auth/setup",
            headers={"X-Forwarded-For": "198.51.100.9"},
            json={"username": "second", "password": "SecondPassword1!"},
        )
        self.assertEqual(r_second.status_code, 409)

    def test_fallbacks_crud_model_override_and_secret_preservation(self) -> None:
        import yaml
        from engine.builtin.fallback_provider import _resolve_fallback_entry

        # Seed deepseek channel (flash) + initial fallback with inline secret & sticky_session
        r_up = self.client.put(
            "/v1/config/providers/deepseek",
            headers=self.jwt_hdr,
            json={
                "base_url": "https://api.deepseek.example/v1",
                "api_key": "sk-ds-channel-9999",
                "model": "deepseek-flash",
            },
        )
        self.assertEqual(r_up.status_code, 200)

        cfg_path = self.ws / "data" / "config.yaml"
        raw_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        raw_cfg["fallbacks"] = [
            {
                "provider": "openai",
                "base_url": "https://backup-oai.example/v1",
                "api_key": "sk-inline-secret-7788",
                "sticky_session": True,
            }
        ]
        cfg_path.write_text(yaml.safe_dump(raw_cfg, sort_keys=False), encoding="utf-8")
        self.cs.reload()

        # GET /v1/config/providers redacts inline fallback api_key
        r_get = self.client.get("/v1/config/providers", headers=self.jwt_hdr)
        self.assertEqual(r_get.status_code, 200)
        fb_pub = r_get.json()["fallbacks"]
        self.assertEqual(len(fb_pub), 1)
        self.assertNotIn("api_key", fb_pub[0])
        self.assertTrue(fb_pub[0]["api_key_present"])
        self.assertEqual(fb_pub[0]["api_key_redacted"], "****7788")

        # Unknown provider rejected with 400
        r_bad = self.client.put(
            "/v1/config/fallbacks",
            headers=self.jwt_hdr,
            json={"fallbacks": [{"provider": "not-registered"}]},
        )
        self.assertEqual(r_bad.status_code, 400)

        # Put deepseek (override model -> pro) ahead of reordered openai (_source_index=0)
        r_save = self.client.put(
            "/v1/config/fallbacks",
            headers=self.jwt_hdr,
            json={
                "fallbacks": [
                    {"provider": "deepseek", "model": "deepseek-pro"},
                    {"provider": "openai", "model": "gpt-4.1", "_source_index": 0},
                ]
            },
        )
        self.assertEqual(r_save.status_code, 200, r_save.text)
        saved_body = r_save.json()
        self.assertIn("registered", saved_body)
        self.assertIn("deepseek", saved_body["registered"])
        self.assertEqual(
            [(f["provider"], f.get("model")) for f in saved_body["fallbacks"]],
            [("deepseek", "deepseek-pro"), ("openai", "gpt-4.1")],
        )

        disk_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_cfg["fallbacks"][0], {"provider": "deepseek", "model": "deepseek-pro"})
        self.assertEqual(disk_cfg["fallbacks"][1]["api_key"], "sk-inline-secret-7788")
        self.assertEqual(disk_cfg["fallbacks"][1]["sticky_session"], True)
        self.assertNotIn("_source_index", disk_cfg["fallbacks"][1])

        # Verify engine fallback resolution uses deepseek-pro while inheriting base_url & api_key
        resolved = _resolve_fallback_entry(disk_cfg["fallbacks"][0], disk_cfg)
        self.assertEqual(resolved["provider"], "deepseek")
        self.assertEqual(resolved["model"], "deepseek-pro")
        self.assertEqual(resolved["base_url"], "https://api.deepseek.example/v1")
        self.assertEqual(resolved["api_key"], "sk-ds-channel-9999")

        # Clearing model override omits empty model key from YAML
        r_clear_model = self.client.put(
            "/v1/config/fallbacks",
            headers=self.jwt_hdr,
            json={"fallbacks": [{"provider": "deepseek", "model": "", "_source_index": 0}]},
        )
        self.assertEqual(r_clear_model.status_code, 200)
        disk_cfg_after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_cfg_after["fallbacks"], [{"provider": "deepseek"}])

    def test_policy_approval_rules_file_download_guard_and_ws_query_reject(self) -> None:
        import stat
        from starlette.websockets import WebSocketDisconnect
        from engine.attachments import attachments_to_content_parts
        from engine.inference.dynamic_context import _load_dynamic_context_vars
        from supervisor.policy import PolicyEngine

        # 1. File permissions on .admin_token and web_auth.json are 0600
        tok_mode = stat.S_IMODE((self.ws / "data" / ".admin_token").stat().st_mode)
        wa_mode = stat.S_IMODE((self.ws / "data" / "web_auth.json").stat().st_mode)
        self.assertEqual(tok_mode, 0o600)
        self.assertEqual(wa_mode, 0o600)

        # 2. Policy rules: credential/config reads require approval (not auto, not breaking .env deny)
        pol = PolicyEngine(workspace_root=self.ws)
        self.assertEqual(pol.evaluate_read_file(path=".env").safety_level.value, "deny")
        self.assertEqual(pol.evaluate_read_file(path=".env.example").safety_level.value, "auto")
        for p in (
            ".env.local",
            "sub/.env.production",
            "data/.admin_token",
            "data/web_auth.json",
            "data/config.yaml",
            "data/mcp_clients.yaml",
            "data/remote_workers.yaml",
            "data/keys_x.txt",
            "data/nai_key.txt",
        ):
            self.assertEqual(pol.evaluate_read_file(path=p).safety_level.value, "approval_required", p)
        self.assertEqual(pol.evaluate_write_file(path="config/dynamic_context.yaml").safety_level.value, "approval_required")
        self.assertEqual(pol.evaluate_write_file(path="plugins/demo/__init__.py").safety_level.value, "approval_required")

        # 3. Session workspace rejects restricted roots; session_file blocks data secrets, serves attachments
        r_sess = self.client.post(
            "/v1/sessions/get_or_create",
            headers=self.jwt_hdr,
            json={"channel": "web", "conversation_key": "web:file-check"},
        )
        self.assertEqual(r_sess.status_code, 200)
        sid = r_sess.json()["session_id"]

        for bad_ws in ("/", "/etc", "/root", str(self.ws / "data")):
            self.assertEqual(
                self.client.put(f"/v1/sessions/{sid}/workspace", headers=self.jwt_hdr, json={"name": "bad", "path": bad_ws}).status_code,
                400,
                bad_ws,
            )
        self.assertEqual(
            self.client.get("/v1/workspace/tree", headers=self.jwt_hdr, params={"path": "/etc"}).status_code,
            400,
        )

        # Bind workspace to self.ws: ordinary project file works, data secrets & .env variants are blocked (403)
        (self.ws / "readme.txt").write_text("hello", encoding="utf-8")
        (self.ws / ".env.local").write_text("SEC=1", encoding="utf-8")
        (self.ws / "data" / "attachments" / "s1").mkdir(parents=True, exist_ok=True)
        (self.ws / "data" / "attachments" / "s1" / "ok.txt").write_text("att-ok", encoding="utf-8")
        (self.ws / "data" / "temp").mkdir(parents=True, exist_ok=True)
        (self.ws / "data" / "temp" / "gemini_ais_keys_alive_28.txt").write_text("SECRET_KEY", encoding="utf-8")
        (self.ws / "data" / "temp" / "config.yaml.bak.2026").write_text("BAK_SECRET", encoding="utf-8")
        (self.ws / "data" / "memory").mkdir(parents=True, exist_ok=True)
        (self.ws / "data" / "memory" / "default.yaml").write_text("MEM_SECRET", encoding="utf-8")
        self.assertEqual(
            self.client.put(f"/v1/sessions/{sid}/workspace", headers=self.jwt_hdr, json={"name": "ws", "path": str(self.ws)}).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/v1/sessions/{sid}/file", headers=self.jwt_hdr, params={"path": "readme.txt"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/v1/sessions/{sid}/file", headers=self.jwt_hdr, params={"path": "data/attachments/s1/ok.txt"}).status_code,
            200,
        )
        for blocked_dl in (
            "data/.admin_token",
            "data/web_auth.json",
            "data/config.yaml",
            "data/memory/default.yaml",
            "data/temp/gemini_ais_keys_alive_28.txt",
            "data/temp/config.yaml.bak.2026",
            ".env.local",
        ):
            self.assertEqual(
                self.client.get(f"/v1/sessions/{sid}/file", headers=self.jwt_hdr, params={"path": blocked_dl}).status_code,
                403,
                blocked_dl,
            )

        # 4. Inline attachments block protected runtime/credential files under data/
        parts = attachments_to_content_parts(
            [
                {"path": "data/.admin_token", "type": "file"},
                {"path": "data/config.yaml", "type": "file"},
                {"path": "data/attachments/s1/ok.txt", "type": "file"},
            ],
            self.ws,
        )
        self.assertIn("protected runtime or credential file", parts[0]["text"])
        self.assertIn("protected runtime or credential file", parts[1]["text"])
        self.assertIn("att-ok", parts[2]["text"])

        # 5. dynamic_context allows safe expressions and blocks __import__ & format-string dunder
        (self.ws / "config" / "dynamic_context.yaml").write_text(
            "variables:\n"
            "  util: \"f'{round(prompt_tokens / compact_threshold * 100)}%' if compact_threshold else 'N/A'\"\n"
            "  blocked: \"__import__('os').popen('id').read()\"\n"
            "  blocked_fmt: \"'{0.__class__}'.format(now)\"\n",
            encoding="utf-8",
        )
        dc_vars = _load_dynamic_context_vars(self.ws, prompt_tokens=50, compact_threshold=200)
        self.assertEqual(dc_vars["util"], "25%")
        self.assertEqual(dc_vars["blocked"], "")
        self.assertEqual(dc_vars["blocked_fmt"], "")

        # 6. Login rate-limit uses rightmost X-Forwarded-For even when spoofed X-Real-IP is sent
        for idx in range(8):
            r_fail = self.client.post(
                "/v1/admin/auth/login",
                json={"username": "admin", "password": "bad"},
                headers={
                    "X-Forwarded-For": f"10.0.0.{idx}, 198.51.100.77",
                    "X-Real-IP": f"203.0.113.{idx}",
                },
            )
            self.assertEqual(r_fail.status_code, 401)
        r_blocked = self.client.post(
            "/v1/admin/auth/login",
            json={"username": "admin", "password": "StrongPassword!123"},
            headers={"X-Forwarded-For": "10.9.9.9, 198.51.100.77"},
        )
        self.assertEqual(r_blocked.status_code, 429)

        # 7. WS ?token= query param alone is not accepted without first-frame/header auth
        with self.client.websocket_connect(f"/v1/ws?token={self.jwt}") as ws:
            ws.send_json({"last_seq": 0})
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_text()


if __name__ == "__main__":
    unittest.main()
