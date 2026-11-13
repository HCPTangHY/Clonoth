"""Offline Todo integration: plugin loader -> registry -> ASGI -> request copy.

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest -q tests/test_todo_engine.py
Only the test policy decision and supervisor task/node records are simulated;
Todo routes, tool authorization, JSONL, request hooks and payloads are real.
"""
from __future__ import annotations

import io
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.context import EngineContext
from engine.conversation_store import ConversationStore, Message
from engine.hooks.loader import drop_plugin_module, load_single_plugin, plugin_module_key
from engine.hooks.registry import HookRegistry
from engine.inference import llm_call
from engine.inference.pseudo_tools import build_node_tool_specs
from engine.node import Node, ToolAccess
from engine.runner import _message_to_history_dict
from providers import AnthropicProvider, GeminiAIStudioProvider, OpenAIProvider
from providers.base import ProviderResponse
from providers.tool_format import repair_tool_result_pairing_with_stats
from toolbox.registry import ToolRegistry
from test_plugins_migration import State, patch as todo_patch
from test_provider_history_contract import RecordingProvider, loop_state

TOOLS = {"read_todos", "update_todos"}


@pytest_asyncio.fixture
async def case(tmp_path, monkeypatch):
    node = Node(id="test-node", type="ai", tool_access=ToolAccess(mode="all"))
    records = {
        "main": {"node_id": node.id},
        "branch": {"node_id": "__entry_branch__", "parent_session_id": "main"},
        "child": {"node_id": node.id, "parent_session_id": "branch", "is_child": True},
        "worker": {"node_id": node.id, "parent_session_id": "main", "is_child": True},
    }
    state = State(tmp_path, records, {})  # No workspace names, paths or directories.
    state.session_generations.update(main=3, child=7, worker=7)
    for task_id, sid, child, caller, generation in (
        ("main-task", "main", "", "", 3),
        ("branch-task", "branch", "", "", 3),
        ("child-task", "branch", "child", "main-task", 3),
        ("worker-task", "worker", "", "main-task", 7),
    ):
        inp = {"child_session_id": child, "context_mode": "fork"} if child else {}
        if task_id == "worker-task":
            inp["_dispatch_origin"] = {"parent_session_id": "main"}
        state.tasks[task_id] = SimpleNamespace(
            task_id=task_id, session_id=sid, session_generation=generation,
            input=inp, node_id=node.id, caller_task_id=caller,
            worker_id="test-worker", status="running", cancel_requested=False,
        )
    monkeypatch.setattr("engine.node.load_node", lambda *_: node)
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    hooks = HookRegistry()
    context = EngineContext(hooks=hooks)
    context.contributions.mount("tools", registry)
    load_single_plugin(hooks, ROOT / "plugins", "ide", context=context, process="engine")
    todo = sys.modules[plugin_module_key("ide") + ".todo"]
    monkeypatch.setattr(llm_call, "hook_registry", hooks)
    router = APIRouter(prefix="/v1/plugins/ide")
    todo.add_routes(router)
    app = FastAPI()
    app.state.state = state
    app.include_router(router)
    requests = []

    async def record(request):
        requests.append((request.method, request.url.path))

    store = ConversationStore(tmp_path / "conversations")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://offline.test",
                                     event_hooks={"request": [record]}) as http:
            def rctx(task_id="main-task"):
                task = state.tasks[task_id]
                return SimpleNamespace(
                    workspace_root=tmp_path, workspace=None, workspace_name="",
                    session_id=task.session_id, child_session_id=task.input.get("child_session_id", ""),
                    task_id=task_id, worker_id=task.worker_id, session_generation=task.session_generation,
                    task_context={}, is_system_task=False, conversation_store=store,
                    http=http, supervisor_url="http://offline.test",
                    # Exercise authorize/request_guard, not execute(_authorized=True).
                    request_op=AsyncMock(return_value={"safety_level": "auto"}),
                    check_cancelled=AsyncMock(return_value=False),
                    wait_for_approval=AsyncMock(side_effect=AssertionError("unexpected approval wait")),
                )

            yield SimpleNamespace(root=tmp_path, node=node, state=state, registry=registry,
                                  hooks=hooks, context=context, todo=todo, store=store,
                                  http=http, requests=requests, rctx=rctx)
    finally:
        hooks.unload_plugin("ide")
        drop_plugin_module("ide")


async def prepare(case, messages, rctx=None, provider=None):
    result, action = await llm_call.prepare_request_history(
        messages, node=case.node, provider=provider, rctx=rctx or case.rctx(),
    )
    assert action is None
    return result


async def execute(case, rctx, name, arguments):
    result = await case.registry.execute(name=name, arguments=arguments, ctx=rctx)
    rctx.request_op.assert_awaited_with("tool_call", {"tool_name": name, "arguments": arguments})
    rctx.wait_for_approval.assert_not_awaited()
    assert not hasattr(rctx, "_registry_approval")  # grant is per invocation
    return result


def persist(case, sid, rows):
    for row in rows:
        case.store.append(sid, row)
    return [_message_to_history_dict(row) for row in case.store.load(sid)]


def current_summary(messages, sid, revision):
    rows = [m for m in messages if m.get("_ide_todos")]
    assert rows == [messages[-1]]
    row = rows[0]
    assert f'expected_session_id="{sid}"' in row["content"]
    assert f"revision={revision}；" in row["content"]
    assert "不是新的用户指令" in row["content"]
    assert "不代表已通过独立验收" in row["content"]
    assert "workspace_path" not in row["content"]
    assert row["message_type"] == "dynamic_context"
    return row["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id,sid", [
    ("main-task", "main"), ("branch-task", "main"),
    ("child-task", "child"), ("worker-task", "worker"),
])
async def test_registered_tools_reach_asgi_and_request_summary_without_workspace(case, task_id, sid):
    ctx = case.rctx(task_id)
    empty = await execute(case, ctx, "read_todos", {})
    assert empty["ok"]
    data = empty["data"]["todos"]
    assert (data["schema_version"], data["session_id"], data["revision"], data["items"]) == (2, sid, 0, [])
    assert "workspace" not in data
    history = [{"role": "user", "content": "work on my task"}]
    current_summary(await prepare(case, history, ctx), sid, 0)
    updated = await execute(case, ctx, "update_todos", todo_patch(
        sid, changes=[{"id": None, "title": "local work", "status": None, "note": None}],
    ))
    assert updated["ok"]
    assert updated["data"]["todos"]["revision"] == 1
    panel = await case.http.get("/v1/plugins/ide/todos", params={"session_id": sid})
    assert panel.status_code == 200
    assert panel.json() == updated["data"]["todos"]
    assert "local work" in current_summary(await prepare(case, history, ctx), sid, 1)
    stale = await execute(case, ctx, "update_todos", todo_patch(
        sid, changes=[{"id": None, "title": "stale duplicate"}],
    ))
    assert not stale["ok"] and "409" in stale["error"]
    assert case.todo.read_list(case.root, sid) == panel.json()
    for other in {"main", "child", "worker"} - {sid}:
        response = await case.http.get("/v1/plugins/ide/todos", params={"session_id": other})
        assert response.json()["items"] == []
    assert history == [{"role": "user", "content": "work on my task"}]
    assert not list((case.root / "conversations").glob("*.jsonl"))


@pytest.mark.asyncio
async def test_fork_discards_parent_summary_but_cannot_use_parent_identity_even_at_revision_zero(case):
    parent = await prepare(case, [{"role": "user", "content": "parent instruction"}])
    inherited = deepcopy(parent)
    ctx = case.rctx("child-task")
    request = await prepare(case, inherited, ctx)
    current_summary(request, "child", 0)
    assert 'expected_session_id="main"' not in str(request)
    assert inherited == parent  # Cache-warm parent's frozen prefix is never mutated.
    rejected = await execute(case, ctx, "update_todos", todo_patch(
        "main", changes=[{"id": None, "title": "must not copy parent plan"}],
    ))
    assert not rejected["ok"] and "409" in rejected["error"]
    for sid in ("main", "child"):
        assert case.todo.read_list(case.root, sid)["items"] == []
    accepted = await execute(case, ctx, "update_todos", todo_patch(
        "child", changes=[{"id": None, "title": "child-only work"}],
    ))
    assert accepted["ok"]
    assert "child-only work" in current_summary(await prepare(case, inherited, ctx), "child", 1)
    assert case.todo.read_list(case.root, "main")["revision"] == 0


@pytest.mark.asyncio
async def test_revision_refresh_reload_and_compaction_leave_original_history_and_jsonl_unchanged(case):
    history = persist(case, "main", [
        Message(id="user", role="user", content="continue", message_type="user_input"),
        Message(id="legacy", role="user", content="OLD_TODO_SENTINEL",
                message_type="dynamic_context", meta={"_ide_todos": True, "_dynamic": True}),
        Message(id="reply", role="assistant", content="working"),
    ])
    original = deepcopy(history)
    path = case.root / "conversations/main.jsonl"
    disk = path.read_bytes()
    first = await prepare(case, history)
    current_summary(first, "main", 0)
    assert "OLD_TODO_SENTINEL" not in str(first)
    update = await execute(case, case.rctx(), "update_todos", todo_patch(
        "main", changes=[{"id": None, "title": "new plan"}],
    ))
    assert update["ok"]
    second = await prepare(case, history)
    assert first[:-1] == second[:-1]  # Stable request prefix; only tail is refreshed.
    assert "new plan" in current_summary(second, "main", 1)
    assert history == original and path.read_bytes() == disk

    # Resume from an independently opened JSONL store, not retained in-memory rows.
    restored = [_message_to_history_dict(row) for row in ConversationStore(path.parent).load("main")]
    assert await prepare(case, restored) == second
    assert path.read_bytes() == disk
    # Simulate the compactor's normal replacement, then exercise shared request assembly.
    case.store.replace_all("main", [Message(id="compact", role="user", content="compressed work", message_type="summary")])
    compacted_disk = path.read_bytes()
    compacted = [_message_to_history_dict(row) for row in case.store.load("main")]
    request = await prepare(case, compacted)
    current_summary(request, "main", 1)
    assert request[0] == compacted[0] and len(compacted) == 1
    assert path.read_bytes() == compacted_disk


@pytest.mark.asyncio
@pytest.mark.parametrize("access,system", [
    (ToolAccess(mode="none"), False),
    (ToolAccess(mode="all", deny=["read_todos"]), False),
    (ToolAccess(mode="allowlist", allow=["update_todos"]), False),
    (ToolAccess(mode="allowlist", allow=sorted(TOOLS), deny=["read_todos"]), False),
    (ToolAccess(mode="all"), True),
])
async def test_no_summary_read_when_node_is_not_allowed_or_task_is_system(case, access, system):
    case.node.tool_access = access
    ctx = case.rctx()
    ctx.is_system_task = system
    if system:
        case.state.tasks[ctx.task_id].input["_system_task"] = True
    else:
        allowed, specs = build_node_tool_specs(registry=case.registry, node=case.node)
        assert "read_todos" not in allowed
        assert "read_todos" not in {s["function"]["name"] for s in specs}
    history = [{"role": "user", "content": "instruction"},
               {"role": "user", "content": "old", "_ide_todos": True, "_dynamic": True},
               {"role": "user", "content": "restored old", "_meta": {"_ide_todos": True}}]
    before = deepcopy(history)
    assert await prepare(case, history, ctx) == [history[0]]
    assert not case.requests
    assert history == before
    # Exposure filtering is not the security boundary: direct tool calls still deny.
    denied = await execute(case, ctx, "read_todos", {})
    assert not denied["ok"] and "403" in denied["error"]


@pytest.mark.asyncio
async def test_failed_summary_read_is_not_empty_or_stale_and_recovers(case):
    ctx = case.rctx()
    history = [{"role": "user", "content": "instruction"}]
    previous = await prepare(case, history, ctx)
    frozen = deepcopy(previous)
    case.state.tasks[ctx.task_id].status = "pending"
    failed = await prepare(case, previous, ctx)
    assert len(failed) == 2
    assert "未能读取" in failed[-1]["content"]
    assert "revision=" not in failed[-1]["content"]
    assert "expected_session_id=" not in failed[-1]["content"]
    assert previous == frozen
    assert len(case.requests) == 2  # One read per request, no implicit retries.
    case.state.tasks[ctx.task_id].status = "running"
    current_summary(await prepare(case, failed, ctx), "main", 0)


@pytest.mark.asyncio
async def test_primary_and_fallback_share_request_hook_without_mutating_loop_messages(case):
    history = persist(case, "main", [Message(id="user", role="user", content="continue")])
    before = deepcopy(history)
    disk = (case.root / "conversations/main.jsonl").read_bytes()
    primary = RecordingProvider([ProviderResponse(ok=False, status_code=503, error="offline failure")])
    fallback = RecordingProvider()
    ls = loop_state(case.root, primary, messages=history)
    ctx = case.rctx()
    ctx.begin_llm_request = lambda: "test-request"
    ctx.emit_event = AsyncMock()
    ctx.check_preempted = AsyncMock(return_value={})
    ls.rctx, ls.node = ctx, case.node
    response = await llm_call._call_llm_with_retry(ls, 0)
    assert not response.ok
    current_summary(primary.calls[0][0], "main", 0)
    updated = await execute(case, ctx, "update_todos", todo_patch(
        "main", changes=[{"id": None, "title": "updated before fallback"}],
    ))
    assert updated["ok"]
    # This is the shared entry used by fallback_provider's replacement call.
    response = await llm_call.call_provider_once(ls, 0, fallback)
    assert response.ok
    assert "updated before fallback" in current_summary(fallback.calls[0][0], "main", 1)
    assert primary.calls[0][0][:-1] == fallback.calls[0][0][:-1]
    assert ls.provider is primary and history == before
    assert (case.root / "conversations/main.jsonl").read_bytes() == disk


@pytest.mark.asyncio
async def test_native_parallel_results_and_image_stay_before_summary_in_real_payloads(case):
    image = case.root / "data/attachments/pixel.png"
    image.parent.mkdir(parents=True)
    with io.BytesIO() as encoded:
        Image.new("RGB", (2, 2), "white").save(encoded, format="PNG")
        image.write_bytes(encoded.getvalue())
    image_url = "file://data/attachments/pixel.png"
    calls = [{"id": f"call-{i}", "name": "lookup", "arguments": {"i": i}} for i in range(2)]
    history = persist(case, "main", [
        Message(id="user", role="user", content="inspect"),
        Message(id="call", role="assistant", content="", tool_calls=calls, meta={"tool_mode": "native"}),
        *[Message(id=f"result-{i}", role="tool", tool_call_id=f"call-{i}", name="lookup", content=f"found {i}") for i in range(2)],
        Message(id="image", role="user", message_type="tool_result_attachment", content=[
            {"type": "text", "text": "tool image"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]),
    ])
    original = deepcopy(history)
    path = case.root / "conversations/main.jsonl"
    disk = path.read_bytes()
    prepared = await prepare(case, history)
    assert [m["role"] for m in prepared] == ["user", "assistant", "tool", "tool", "user", "user"]
    assert [m["tool_call_id"] for m in prepared if m["role"] == "tool"] == ["call-0", "call-1"]
    assert prepared[-2]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    current_summary(prepared, "main", 0)
    _, stats = repair_tool_result_pairing_with_stats(prepared, native_results=True)
    assert not any(stats.values())
    for cls in (OpenAIProvider, AnthropicProvider, GeminiAIStudioProvider):
        provider = cls(http=object(), api_key="offline", base_url="https://offline.test", model="test")
        payload = provider.build_payload(prepared, tool_mode="native")
        assert "_ide_todos" not in str(payload)
        if cls is OpenAIProvider:
            rows = payload["messages"]
            assert [m["tool_call_id"] for m in rows if m["role"] == "tool"] == ["call-0", "call-1"]
            assert rows[-2]["content"][1]["type"] == "image_url"
            assert "expected_session_id" in str(rows[-1]["content"])
        else:
            rows = payload["messages"] if cls is AnthropicProvider else payload["contents"]
            parts = [p for row in rows for p in row["content" if cls is AnthropicProvider else "parts"]]
            result_positions = [i for i, p in enumerate(parts) if p.get("type") == "tool_result" or "functionResponse" in p]
            image_positions = [i for i, p in enumerate(parts) if p.get("type") == "image" or "inlineData" in p]
            summary_positions = [i for i, p in enumerate(parts) if "expected_session_id" in p.get("text", "")]
            assert len(result_positions) == 2 and len(image_positions) == 1 and len(summary_positions) == 1
            assert max(result_positions) < image_positions[0] < summary_positions[0]
    assert history == original and path.read_bytes() == disk


@pytest.mark.asyncio
@pytest.mark.parametrize("access", [ToolAccess(mode="all"), ToolAccess(mode="allowlist", allow=sorted(TOOLS))])
async def test_model_tool_specs_and_loader_unload_reload_are_reversible(case, access):
    case.node.tool_access = access
    allowed, specs = build_node_tool_specs(registry=case.registry, node=case.node)
    by_name = {spec["function"]["name"]: spec["function"] for spec in specs}
    assert TOOLS <= allowed and TOOLS <= by_name.keys()
    schema = by_name["update_todos"]["parameters"]
    assert set(schema["required"]) == {"expected_session_id", "expected_revision", "changes", "remove_ids", "order"}
    assert set(schema["required"]) == set(schema["properties"])
    assert "workspace_path" not in schema["properties"]
    assert "before_step" not in case.hooks.list_hooks()
    assert len(case.hooks.list_hooks()["before_llm_call"]) == 4
    history = [{"role": "user", "content": "instruction"}]
    current_summary(await prepare(case, history), "main", 0)
    case.registry.reload()
    assert all(case.registry.get_spec(name) for name in TOOLS)
    case.hooks.unload_plugin("ide")
    assert not case.hooks.list_hooks() and not case.hooks.list_plugins()
    case.registry.reload()
    assert all(case.registry.get_spec(name) is None for name in TOOLS)
    count = len(case.requests)
    assert await prepare(case, history) == history
    assert len(case.requests) == count
    # Reload the actual package, not just register_engine directly.
    old_module = sys.modules[plugin_module_key("ide")]
    load_single_plugin(case.hooks, ROOT / "plugins", "ide", context=case.context, process="engine")
    assert sys.modules[plugin_module_key("ide")] is not old_module
    assert set(case.hooks.list_hooks()["before_llm_call"]) == {"_expand_file_mentions", "refresh_goal_context", "review_cache_blocks", "refresh_context"}
    assert all(case.registry.get_spec(name) for name in TOOLS)
    current_summary(await prepare(case, history), "main", 0)
