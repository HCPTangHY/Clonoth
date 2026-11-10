"""Deterministic registry changes while approvals wait; no real tools/services.

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest tests/test_registry_approval_race.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolbox.registry import ToolRegistry


NAME = "mcp_demo_call"
ARGS = {"command": "echo allowed", "nested": {"value": 1}}


def setup_registry(*, inner_guard=False, wait_on="tool_call", outcome="allowed"):
    registry = ToolRegistry.__new__(ToolRegistry)
    original = AsyncMock(return_value={"ok": True, "data": {"result": "original"}})
    replacement = AsyncMock(return_value={"ok": True, "data": {"result": "replacement"}})
    registry._tool_funcs = {NAME: original}
    registry._tool_specs = {NAME: {"name": NAME, "input_schema": {"type": "object"}}}
    if inner_guard:
        registry._tool_specs[NAME].update(guard_op="execute_command", guard_params=["command"])
    entered, release = asyncio.Event(), asyncio.Event()
    requested = []

    async def request_op(op, parameters):
        requested.append(op)
        return {"safety_level": "approval_required", "approval_id": op} if op == wait_on else {"safety_level": "auto"}

    async def wait_for_approval(approval_id):
        entered.set()
        await release.wait()
        return {"status": outcome}

    ctx = SimpleNamespace(request_op=request_op, wait_for_approval=wait_for_approval,
                          check_cancelled=AsyncMock(return_value=False))
    return registry, original, replacement, ctx, entered, release, requested


@pytest.mark.parametrize("change", ["remove", "replace", "mutate_guard_spec"])
@pytest.mark.parametrize("wait_on", ["tool_call", "execute_command"])
def test_no_execution_when_registry_changes_during_approval(change, wait_on):
    async def run():
        registry, old, new, ctx, entered, release, requests = setup_registry(inner_guard=True, wait_on=wait_on)
        task = asyncio.create_task(registry.execute(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        if change == "remove":
            registry._tool_funcs.pop(NAME)
        elif change == "replace":
            registry._tool_funcs[NAME] = new
        else:
            registry._tool_specs[NAME]["guard_params"].append("nested")
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["ok"] is False and result["denied"] is False
        assert result["rejection_code"] == ("tool_unavailable" if change == "remove" else "tool_changed")
        assert result["data"]["result"].startswith("ERROR:")
        old.assert_not_awaited()
        new.assert_not_awaited()
        # Do not ask for a second guard after discovering that the first is stale.
        assert requests == (["tool_call"] if wait_on == "tool_call" else ["tool_call", "execute_command"])
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("change", ["remove", "replace", "guard", "arguments"])
def test_preflight_grant_cannot_cross_replacement_before_execution(authorized, change):
    async def run():
        registry, old, new, ctx, entered, release, requests = setup_registry()
        release.set()
        assert await registry.authorize(name=NAME, arguments=ARGS, ctx=ctx) is None
        arguments = ARGS
        if change == "remove":
            registry._tool_funcs.pop(NAME)
        elif change == "replace":
            registry._tool_funcs[NAME] = new
        elif change == "guard":
            registry._tool_specs[NAME]["guard_op"] = "write_file"
        else:
            arguments = {**ARGS, "nested": {"value": 2}}
        result = await registry.execute(name=NAME, arguments=arguments, ctx=ctx, _authorized=authorized)
        assert result["ok"] is False
        old.assert_not_awaited()
        new.assert_not_awaited()
        assert requests == ["tool_call"], "Stale grants must not cause automatic retries"
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
def test_preflight_unchanged_executes_once_without_second_approval(authorized):
    async def run():
        registry, old, new, ctx, entered, release, requests = setup_registry(inner_guard=True)
        release.set()
        assert await registry.authorize(name=NAME, arguments=ARGS, ctx=ctx) is None
        result = await registry.execute(name=NAME, arguments=ARGS, ctx=ctx, _authorized=authorized)
        assert result["ok"] is True
        old.assert_awaited_once()
        new.assert_not_awaited()
        assert requests == ["tool_call", "execute_command"]
    asyncio.run(run())


def test_denial_takes_precedence_over_registry_change():
    async def run():
        registry, old, new, ctx, entered, release, _ = setup_registry(outcome="denied")
        task = asyncio.create_task(registry.execute(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        registry._tool_funcs[NAME] = new
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["error"] == "user denied approval"
        assert result["denied"] is True
        assert "rejection_code" not in result
        old.assert_not_awaited()
        new.assert_not_awaited()
    asyncio.run(run())


def test_direct_execution_without_registry_change_is_unchanged():
    async def run():
        registry, old, new, ctx, _, release, _ = setup_registry()
        release.set()
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=ctx))["ok"]
        old.assert_awaited_once()
        new.assert_not_awaited()
    asyncio.run(run())


def test_incomplete_preflight_cannot_execute_with_authorized_flag():
    async def run():
        registry, old, new, ctx, entered, release, _ = setup_registry(outcome="denied")
        task = asyncio.create_task(registry.authorize(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        result = await registry.execute(name=NAME, arguments=ARGS, ctx=ctx, _authorized=True)
        assert result["rejection_code"] == "tool_not_authorized"
        release.set()
        assert (await task)["denied"]
        old.assert_not_awaited()
    asyncio.run(run())


def assert_no_grant(ctx):
    assert not hasattr(ctx, "_registry_approval")
    assert not hasattr(ctx, "_approved_guard_fps")


@pytest.mark.parametrize("change", ["name", "arguments", "same"])
def test_context_reuse_requires_fresh_approval(change):
    async def run():
        registry, old, new, ctx, _, release, requests = setup_registry(inner_guard=True)
        release.set()
        ctx.tool_call_id = "first"
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=ctx))["ok"]
        assert_no_grant(ctx)
        name, arguments = NAME, ARGS
        if change == "name":
            name = "another"
            registry._tool_funcs[name] = new
            registry._tool_specs[name] = registry._tool_specs[NAME].copy()
        elif change == "arguments":
            arguments = {**ARGS, "command": "echo another"}
        # Even identical params (or reused provider IDs) are not cached across execute.
        ctx.tool_call_id = "second" if change != "same" else "first"
        assert (await registry.execute(name=name, arguments=arguments, ctx=ctx))["ok"]
        assert_no_grant(ctx)
        assert requests == ["tool_call", "execute_command"] * 2
        assert old.await_count + new.await_count == 2
    asyncio.run(run())


@pytest.mark.parametrize("preflight", [False, True])
def test_denial_cleans_grant_and_retry_asks_again(preflight):
    async def run():
        registry, old, new, ctx, _, release, requests = setup_registry(inner_guard=True, wait_on="execute_command", outcome="denied")
        release.set()
        ctx.tool_call_id = "first"
        invoke = registry.authorize if preflight else registry.execute
        assert (await invoke(name=NAME, arguments=ARGS, ctx=ctx))["denied"]
        assert_no_grant(ctx)
        ctx.wait_for_approval = AsyncMock(return_value={"status": "allowed"})
        # Retry can use the same context, ID and params, without stale fingerprints.
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=ctx))["ok"]
        assert_no_grant(ctx)
        assert requests == ["tool_call", "execute_command"] * 2
        old.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "raise"])
def test_pending_approval_cleanup_on_cancellation_or_exception(preflight, failure):
    async def run():
        registry, old, _, ctx, entered, release, _ = setup_registry(inner_guard=True, wait_on="execute_command")
        if failure == "raise":
            async def broken_wait(_):
                entered.set()
                await release.wait()
                raise RuntimeError("approval transport broken")
            ctx.wait_for_approval = broken_wait
        invoke = registry.authorize if preflight else registry.execute
        task = asyncio.create_task(invoke(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        assert hasattr(ctx, "_approved_guard_fps")  # outer gate already passed
        if failure == "cancel":
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await task
        assert_no_grant(ctx)
        old.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancel", "raise"])
def test_business_execution_cleanup_does_not_retry(failure):
    async def run():
        registry, old, _, ctx, _, release, requests = setup_registry()
        release.set()
        entered, business_release = asyncio.Event(), asyncio.Event()
        async def business(*args):
            entered.set()
            await business_release.wait()
            raise RuntimeError("business failed")
        old.side_effect = business
        task = asyncio.create_task(registry.execute(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        if failure == "cancel":
            task.cancel()
        business_release.set()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await task
        assert_no_grant(ctx)
        old.assert_awaited_once()
        assert requests == ["tool_call"]
    asyncio.run(run())


@pytest.mark.parametrize("authorized", [False, True])
def test_preflight_cannot_be_reused_for_new_call_id(authorized):
    async def run():
        registry, old, _, ctx, _, release, requests = setup_registry()
        release.set()
        ctx.tool_call_id = "first"
        assert await registry.authorize(name=NAME, arguments=ARGS, ctx=ctx) is None
        ctx.tool_call_id = "second"
        result = await registry.execute(name=NAME, arguments=ARGS, ctx=ctx, _authorized=authorized)
        assert_no_grant(ctx)
        if authorized:
            assert result["rejection_code"] == "tool_changed"
            old.assert_not_awaited()
            assert requests == ["tool_call"]
        else:
            assert result["ok"]
            old.assert_awaited_once()
            assert requests == ["tool_call", "tool_call"]
    asyncio.run(run())


def test_snapshot_before_preflight_keeps_grant_on_execution_context_only():
    from toolbox.context import ToolContext
    from engine.inference.async_tools import _snapshot_tool_context

    async def run():
        registry, old, _, ctx, _, release, requests = setup_registry()
        release.set()
        base = ToolContext(supervisor_url="http://test.invalid", session_id="s", run_id="r",
                           worker_id="w", workspace_root=Path.cwd(), http=None, registry=registry,
                           tool_call_id="first")
        snapshot = _snapshot_tool_context(base)
        snapshot.request_op = ctx.request_op
        snapshot.check_cancelled = ctx.check_cancelled
        snapshot.wait_for_approval = ctx.wait_for_approval
        assert await registry.authorize(name=NAME, arguments=ARGS, ctx=snapshot) is None
        assert_no_grant(base)
        base.tool_call_id = "second"
        # Async schedulers pass this exact snapshot, not another dataclass copy.
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=snapshot, _authorized=True))["ok"]
        assert_no_grant(snapshot)
        assert snapshot.tool_call_id == "first"
        assert _snapshot_tool_context(base).tool_call_id == "second"
        old.assert_awaited_once()
        assert requests == ["tool_call"]
    asyncio.run(run())


@pytest.mark.parametrize("preflight", [False, True])
def test_returned_cancellation_cleans_approval_state(preflight):
    async def run():
        registry, old, _, ctx, _, release, _ = setup_registry(inner_guard=True, wait_on="execute_command", outcome="cancelled")
        release.set()
        invoke = registry.authorize if preflight else registry.execute
        assert (await invoke(name=NAME, arguments=ARGS, ctx=ctx))["cancelled"]
        assert_no_grant(ctx)
        old.assert_not_awaited()
    asyncio.run(run())


def test_changed_registry_error_cleans_grant_and_manual_retry_can_succeed():
    async def run():
        registry, old, new, ctx, entered, release, requests = setup_registry()
        task = asyncio.create_task(registry.authorize(name=NAME, arguments=ARGS, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        registry._tool_funcs[NAME] = new
        release.set()
        assert (await task)["rejection_code"] == "tool_changed"
        assert_no_grant(ctx)
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=ctx))["ok"]
        assert_no_grant(ctx)
        old.assert_not_awaited()
        new.assert_awaited_once()
        assert requests == ["tool_call", "tool_call"]
    asyncio.run(run())


@pytest.mark.parametrize("target", ["arguments", "spec"])
@pytest.mark.parametrize("wait_on", ["tool_call", "execute_command"])
@pytest.mark.parametrize("before,after", [
    (1, True), (True, 1), (0, False), (False, 0),
    (1, 1.0), (1.0, 1), (None, "null"), ("null", None),
])
def test_json_type_changes_in_nested_lists_invalidate_approval(target, wait_on, before, after):
    async def run():
        registry, old, new, ctx, entered, release, _ = setup_registry(inner_guard=True, wait_on=wait_on)
        arguments = {"command": "echo allowed", "nested": [{"value": before}]}
        registry._tool_specs[NAME]["input_schema"]["const"] = [{"value": before}]
        value = arguments["nested"][0] if target == "arguments" else registry._tool_specs[NAME]["input_schema"]["const"][0]
        task = asyncio.create_task(registry.execute(name=NAME, arguments=arguments, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        value["value"] = after
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["ok"] is False and result["rejection_code"] == "tool_changed"
        assert result["denied"] is False
        old.assert_not_awaited()
        new.assert_not_awaited()
        assert_no_grant(ctx)
    asyncio.run(run())


@pytest.mark.parametrize("target", ["arguments", "spec"])
def test_nested_dictionary_reordering_preserves_approval(target):
    async def run():
        registry, old, _, ctx, entered, release, _ = setup_registry()
        arguments = {"nested": [{"one": 1, "truth": True, "float": 1.0, "nothing": None}]}
        registry._tool_specs[NAME]["input_schema"]["const"] = [{"one": 1, "truth": True, "float": 1.0, "nothing": None}]
        mapping = arguments if target == "arguments" else registry._tool_specs[NAME]
        nested = arguments["nested"][0] if target == "arguments" else mapping["input_schema"]["const"][0]
        task = asyncio.create_task(registry.execute(name=NAME, arguments=arguments, ctx=ctx))
        await asyncio.wait_for(entered.wait(), 1)
        for item in (nested, mapping):
            reversed_items = list(reversed(list(item.items())))
            item.clear()
            item.update(reversed_items)
        release.set()
        assert (await asyncio.wait_for(task, 1))["ok"]
        old.assert_awaited_once()
        assert_no_grant(ctx)
    asyncio.run(run())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
@pytest.mark.parametrize("target", ["arguments", "spec"])
@pytest.mark.parametrize("during_approval", [False, True])
def test_non_json_identity_returns_structured_error_and_cleans_state(value, target, during_approval):
    async def run():
        registry, old, _, ctx, entered, release, requests = setup_registry()
        arguments = {"value": 1}
        mapping = arguments if target == "arguments" else registry._tool_specs[NAME]["input_schema"]
        if not during_approval:
            mapping["value"] = value
        task = asyncio.create_task(registry.execute(name=NAME, arguments=arguments, ctx=ctx))
        if during_approval:
            await asyncio.wait_for(entered.wait(), 1)
            mapping["value"] = value
            release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["ok"] is False and result["denied"] is False
        assert result["rejection_code"] == ("tool_changed" if during_approval else "tool_invalid_json")
        assert requests == (["tool_call"] if during_approval else [])
        old.assert_not_awaited()
        assert_no_grant(ctx)
    asyncio.run(run())


def test_trusted_authorized_flag_without_preflight_preserves_legacy_contract():
    async def run():
        registry, old, _, ctx, _, _, requests = setup_registry()
        assert (await registry.execute(name=NAME, arguments=ARGS, ctx=ctx, _authorized=True))["ok"]
        assert_no_grant(ctx)
        old.assert_awaited_once()
        assert requests == []
    asyncio.run(run())
