"""Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest tests/test_session_approval_rules.py"""
from pathlib import Path
import json
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor._helpers import SessionInfo, _now
from supervisor.policy import PolicyEngine
from supervisor.session_approval import validate_approval_rules, approval_action, ASK_RULES
from supervisor.session_store import SessionStore
from supervisor.state import SupervisorState
from supervisor.types import InboundMessageIn

ALLOW = {"version": 1, "default_action": "allow", "rules": []}


@pytest.fixture
def state(tmp_path):
    st = SupervisorState.__new__(SupervisorState)
    st._lock = RLock()
    st._session_store = SessionStore(tmp_path / "sessions.json")
    st.policy = PolicyEngine(workspace_root=tmp_path)
    st.sessions = {}
    st.tasks = {}
    st.approvals = {}
    st.entry_branch_parents = {}
    st.eventlog = Mock()
    for sid in ("a", "b"):
        info = SessionInfo(sid, "web", sid, _now(), _now())
        st.sessions[sid] = info
        st._session_store.on_session_created(info)
    return st


def request(st, sid="a", call="one", **kwargs):
    return st.request_operation(session_id=sid, op="tool_call", parameters={"tool_name": "unlisted_tool"}, tool_call_id=call, **kwargs)


def test_isolation_pending_and_live_change(state):
    pending = request(state)
    state.set_session_approval_rules("a", ALLOW)
    assert request(state).approval_id == pending.approval_id
    assert state.approvals[pending.approval_id].status == "pending"
    assert request(state, call="two").safety_level == "auto"
    assert request(state, "b").safety_level == "approval_required"
    state.set_session_approval_rules("a", ASK_RULES)
    assert request(state, call="three").safety_level == "approval_required"


def test_policy_hard_deny_and_auto_unchanged(state):
    state.set_session_approval_rules("a", ALLOW)
    assert state.request_operation(session_id="a", op="read_file", parameters={"path": ".env"}).safety_level == "deny"
    state.set_session_approval_rules("a", ASK_RULES)
    assert state.request_operation(session_id="a", op="tool_call", parameters={"tool_name": "list_dir"}).safety_level == "auto"


def test_branch_child_live_inheritance_and_override(state):
    # Real child registry shape: these sessions need not exist in st.sessions.
    state._session_store.on_child_session_created(child_session_id="branch", parent_session_id="a", node_id="__entry_branch__", context_key="")
    state._session_store.on_child_session_created(child_session_id="child", parent_session_id="branch", node_id="coder", context_key="")
    state.set_session_approval_rules("a", ALLOW)
    assert state.get_session_approval_rules("child")["source_session_id"] == "a"
    assert request(state, "child").safety_level == "auto"
    state.set_session_approval_rules("a", ASK_RULES)
    assert request(state, "child", call="two").safety_level == "approval_required"
    state.set_session_approval_rules("child", ALLOW)
    assert request(state, "child", call="three").safety_level == "auto"
    state.set_session_approval_rules("child", None)
    assert state.get_session_approval_rules("child")["source_session_id"] == "a"


def test_task_identity_resolves_child_override(state):
    state.tasks["task"] = SimpleNamespace(session_id="b", caller_task_id=None, input={})
    state.set_session_approval_rules("a", ALLOW)
    state.set_session_approval_rules("b", ASK_RULES)
    assert request(state, "a", task_id="task").safety_level == "approval_required"


def test_synchronous_child_task_uses_execution_session_rules(state):
    state._session_store.on_child_session_created(
        child_session_id="child", parent_session_id="a", node_id="coder", context_key="",
    )
    state.tasks["task"] = SimpleNamespace(
        session_id="a", caller_task_id=None, input={"child_session_id": "child"},
    )
    state.set_session_approval_rules("a", ALLOW)
    state.set_session_approval_rules("child", ASK_RULES)
    assert state.get_session_approval_rules("child")["effective_rules"] == ASK_RULES
    assert request(state, "a", task_id="task").safety_level == "approval_required"
    state.set_session_approval_rules("child", None)
    assert request(state, "a", call="two", task_id="task").safety_level == "auto"


def test_rule_order_and_inner_guard_tool_identity(state):
    rules = {"version": 1, "default_action": "allow", "rules": [
        {"trust_levels": ["trusted", "external"], "action": "ask"},
        {"tool": "apply_*", "action": "ask"},
        {"operation": "restart", "action": "ask"},
    ]}
    state.set_session_approval_rules("a", rules)
    assert state.request_operation(session_id="a", op="write_file", parameters={"path": "engine/x.py"}, tool_name="apply_diff").safety_level == "approval_required"
    assert approval_action(rules, tool="new_tool", operation="tool_call", trust_level="") == "allow"
    assert approval_action(rules, tool="new_tool", operation="read_file", trust_level="external") == "ask"


def test_persistence_delete_and_malformed_fail_closed(state):
    state.set_session_approval_rules("a", ALLOW)
    restored = SessionStore(state._session_store._path).load()[0]
    assert restored["a"].approval_rules == ALLOW
    data = json.loads(state._session_store._path.read_text())
    data["a"]["approval_rules"] = {"version": 1, "default_action": "allow", "rules": [{"action": "bad"}]}
    state._session_store._path.write_text(json.dumps(data))
    state.sessions = SessionStore(state._session_store._path).load()[0]
    assert request(state).safety_level == "approval_required"
    state.set_session_approval_rules("a", None)
    assert state.get_session_approval_rules("a") == {"rules": None, "effective_rules": None, "source_session_id": None}


def test_failed_persistence_does_not_apply_grant(state):
    with patch("supervisor.session_store.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError):
            state.set_session_approval_rules("a", ALLOW)
    assert state.sessions["a"].approval_rules is None
    assert state._session_store._registry["a"].get("approval_rules") is None


@pytest.mark.parametrize("bad", [{}, {**ALLOW, "version": True}, {**ALLOW, "rules": [{"action": "allow", "unknown": True}]}, {**ALLOW, "rules": [{"action": "allow", "trust_levels": []}]}])
def test_validation_rejects_whole_document(bad):
    with pytest.raises(ValueError):
        validate_approval_rules(bad)
    with pytest.raises(ValueError):
        InboundMessageIn(conversation_key="x", text="hello", approval_rules=bad)


def test_inbound_omission_distinct_from_explicit_clear():
    assert "approval_rules" not in InboundMessageIn(conversation_key="x", text="hello").model_fields_set
    assert "approval_rules" in InboundMessageIn(conversation_key="x", text="hello", approval_rules=None).model_fields_set


def test_real_api_auth_validation_and_inbound_order(tmp_path):
    import asyncio
    import httpx
    from supervisor import admin_api
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore
    from supervisor.eventlog import EventLog

    async def run():
        with patch.object(admin_api, "_admin_token", "test-approval-token"), patch.object(admin_api, "_web_auth", None):
            st = SupervisorState(workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", "approval-test"), policy=PolicyEngine(workspace_root=tmp_path))
            app = create_app(state=st, process_manager=None, config_store=ConfigStore(path=tmp_path / "config.yaml"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/v1/sessions/x/approval_rules")).status_code == 401
                client.headers["Authorization"] = "Bearer test-approval-token"
                assert (await client.get("/v1/sessions/x/approval_rules")).status_code == 404
                response = await client.post("/v1/inbound", json={"channel": "web", "conversation_key": "test:approval", "text": "hello", "approval_rules": ALLOW})
                assert response.status_code == 200, response.text
                sid = response.json()["session_id"]
                url = f"/v1/sessions/{sid}/approval_rules"
                assert (await client.get(url)).json()["effective_rules"] == ALLOW
                events = st.eventlog.events
                change = next(i for i, e in enumerate(events) if e["type"] == "session_approval_rules_changed")
                inbound = next(i for i, e in enumerate(events) if e["type"] == "inbound_message")
                assert change < inbound
                assert (await client.put(url, json={"default_action": "allow"})).status_code == 400
                assert (await client.put(url, json=ASK_RULES)).json()["rules"] == ASK_RULES
                assert (await client.delete(url)).json()["effective_rules"] is None
    asyncio.run(run())
