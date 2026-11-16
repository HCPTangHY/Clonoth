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


if __name__ == "__main__":
    unittest.main()
