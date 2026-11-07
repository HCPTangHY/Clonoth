"""Offline checks for the unified knowledge (memory/skill) entry migration.

Covers the contract this migration is responsible for:
  * manage_memory / manage_skill unified names with an explicit action contract
    (default ``list``, mutations only on explicit ``upsert`` / ``delete``),
  * policy approval on the real absolute path for every mutation,
  * atomic memory book writes (a denied or failing write leaves no half file),
  * workspace isolation of stored entries,
  * empty allowlist rejecting everything, ``memories: constant`` filtering,
  * background extraction gates, batching, tail re-arm, and the mutation mutex
    based on ``result['_tool_actions']``,
  * dream routing of manage_memory with the real caller identity preserved.

No live Supervisor, no real workspace writes outside a temp directory.
"""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin import memory_extract as mem_extract
from engine.builtin.knowledge_inject import (
    PLUGIN_META,
    _classify_and_match_entries,
    _filter_entries,
    _resolve_action,
    build_knowledge_messages,
    delete_memory,
    list_memories,
    manage_memory,
    manage_skill,
    normalize_memory_entries,
    build_knowledge_context,
)
from engine.builtin.knowledge_inject.dream_router import DreamMemoryRouter
from engine.node import _node_from_dict
from engine.inference.pseudo_tools import build_node_tool_specs
from toolbox.context import ToolContext


# ---------------------------------------------------------------------------
#  Harness
# ---------------------------------------------------------------------------


NODES_DIR = Path(__file__).resolve().parents[1] / "engine" / "builtin" / "nodes"


def make_ctx(
    root: Path,
    *,
    node_id: str = "child_a",
    workspace_name: str = "",
    node_extra: dict | None = None,
    decision: dict | None = None,
) -> tuple[ToolContext, list[httpx.Request]]:
    """Build an offline ToolContext whose supervisor answers one policy decision."""
    seen: list[httpx.Request] = []
    reply = decision or {"safety_level": "auto", "reason": "test"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False}, request=request)
        if request.url.path.endswith("/v1/ops/request"):
            return httpx.Response(200, json=reply, request=request)
        return httpx.Response(200, json={}, request=request)

    ctx = ToolContext(
        supervisor_url="http://supervisor.test",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=root,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        registry=None,
        task_id="task-1",
        node_id=node_id,
        workspace_name=workspace_name,
    )
    ctx._node_extra = dict(node_extra or {})
    ctx._node_id = node_id
    return ctx, seen


def book_path(root: Path, ns: str, book: str) -> Path:
    return root / "data" / "memory" / ns / f"{book}.yaml"


class TempRootCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config" / "nodes").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "runtime.yaml").write_text("version: 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
#  Unified entry contract
# ---------------------------------------------------------------------------


class UnifiedEntryContractTests(unittest.TestCase):
    def test_only_unified_names_are_registered(self) -> None:
        names = [tool["name"] for tool in PLUGIN_META["tools"]]
        self.assertEqual(sorted(names), ["manage_memory", "manage_skill"])
        for retired in (
            "save_memory", "list_memories", "delete_memory",
            "create_or_update_skill", "list_skills", "delete_skill",
        ):
            self.assertNotIn(retired, names)

    def test_schema_default_action_matches_runtime_default(self) -> None:
        for tool in PLUGIN_META["tools"]:
            schema = tool["input_schema"]
            self.assertEqual(schema["required"], ["action"])
            self.assertEqual(schema["properties"]["action"]["enum"], ["list", "upsert", "delete"])
            self.assertIn("'list' (default)", schema["properties"]["action"]["description"])

    def test_action_defaults_to_list_and_mutations_are_explicit(self) -> None:
        self.assertEqual(_resolve_action({}), "list")
        # Presence of upsert-only fields must not silently turn a call into a write.
        self.assertEqual(_resolve_action({"id": "a", "content": "b"}), "list")
        self.assertEqual(_resolve_action({"name": "x", "description": "d"}), "list")
        self.assertEqual(_resolve_action({"action": "UPSERT"}), "upsert")
        self.assertEqual(_resolve_action({"action": "create"}), "create")
        self.assertEqual(_resolve_action({"action": "save"}), "save")
        self.assertEqual(_resolve_action({"action": "delete"}), "delete")
        self.assertEqual(_resolve_action({"action": "remove"}), "remove")


class ArgumentValidationTests(TempRootCase):
    async def test_upsert_without_required_arguments_fails_loudly(self) -> None:
        ctx, seen = make_ctx(self.root)
        res = await manage_memory({"action": "upsert"}, ctx)
        self.assertFalse(res["ok"])
        self.assertIn("requires id", res["error"])
        res = await manage_memory({"action": "upsert", "id": "a"}, ctx)
        self.assertFalse(res["ok"])
        self.assertIn("requires content", res["error"])
        res = await manage_skill({"action": "upsert", "content": "body"}, ctx)
        self.assertFalse(res["ok"])
        self.assertIn("requires name", res["error"])
        # No mutation was attempted, so no policy request left the process.
        self.assertEqual(seen, [])

    async def test_delete_without_id_and_unknown_action(self) -> None:
        ctx, _seen = make_ctx(self.root)
        self.assertIn("requires id", (await manage_memory({"action": "delete"}, ctx))["error"])
        self.assertIn("requires name", (await manage_skill({"action": "delete"}, ctx))["error"])
        bad = await manage_memory({"action": "rename", "id": "a", "content": "b"}, ctx)
        self.assertFalse(bad["ok"])
        self.assertIn("invalid action", bad["error"])
        self.assertIn("upsert", bad["error"])

    async def test_aliases_and_invalid_argument_shapes_do_not_mutate(self) -> None:
        ctx, seen = make_ctx(self.root)
        for tool in (manage_memory, manage_skill):
            for action in ('save', 'create', 'update', 'remove'):
                result = await tool({'action': action, 'id': 'entry', 'name': 'skill', 'content': 'body'}, ctx)
                self.assertFalse(result['ok'], (tool.__name__, action, result))
            self.assertFalse((await tool([], ctx))['ok'])
        self.assertEqual(seen, [])
        await ctx.http.aclose()

    async def test_manage_memory_list_default_does_not_write(self) -> None:
        ctx, seen = make_ctx(self.root)
        res = await manage_memory({"id": "nope", "content": "ignored"}, ctx)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["entries"], [])
        self.assertFalse((self.root / "data" / "memory").exists())
        self.assertEqual(seen, [])


# ---------------------------------------------------------------------------
#  Mutations: approval, atomicity, isolation
# ---------------------------------------------------------------------------


class MutationGuardTests(TempRootCase):
    async def test_upsert_is_approved_on_the_real_absolute_path(self) -> None:
        ctx, seen = make_ctx(self.root, workspace_name="ws1")
        res = await manage_memory(
            {"action": "upsert", "id": "user_a", "content": "likes tea", "book": "people"}, ctx,
        )
        self.assertTrue(res["ok"], res)
        ops = [r for r in seen if r.url.path.endswith("/v1/ops/request")]
        self.assertEqual(len(ops), 1)
        payload = ops[0].read().decode()
        expected = book_path(self.root, "child_a/@ws1", "people")
        self.assertIn(str(expected), payload)
        self.assertTrue(expected.is_absolute())
        self.assertTrue(expected.exists())

    async def test_denied_approval_leaves_no_file_and_no_partial_book(self) -> None:
        ctx, _seen = make_ctx(
            self.root, decision={"safety_level": "deny", "reason": "nope"},
        )
        res = await manage_memory(
            {"action": "upsert", "id": "user_a", "content": "likes tea", "book": "people"}, ctx,
        )
        self.assertFalse(res["ok"])
        self.assertIn("nope", res["error"])
        self.assertFalse((self.root / "data" / "memory").exists())

    async def test_failed_write_keeps_previous_book_and_no_tmp_file(self) -> None:
        ctx, _seen = make_ctx(self.root, workspace_name="ws1")
        ok = await manage_memory(
            {"action": "upsert", "id": "user_a", "content": "first", "book": "people"}, ctx,
        )
        self.assertTrue(ok["ok"], ok)
        target = book_path(self.root, "child_a/@ws1", "people")
        before = target.read_text(encoding="utf-8")

        # Simulate the rename step failing after the temp file was written.
        from engine.builtin.knowledge_inject import _save_book

        def boom(src, dst):  # noqa: ANN001 - test double
            raise PermissionError("read-only filesystem")

        with patch("engine.builtin.knowledge_inject.os.replace", boom):
            with self.assertRaises(PermissionError):
                _save_book(target, {"book": "people", "entries": []})

        self.assertEqual(target.read_text(encoding="utf-8"), before)
        leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    async def test_workspace_scope_isolates_entries(self) -> None:
        ctx_ws, _ = make_ctx(self.root, workspace_name="ws1")
        res = await manage_memory(
            {"action": "upsert", "id": "task_x", "content": "ws only", "book": "projects"}, ctx_ws,
        )
        self.assertEqual(res["data"]["scope"], "workspace")
        self.assertTrue(book_path(self.root, "child_a/@ws1", "projects").exists())
        self.assertFalse(book_path(self.root, "child_a", "projects").exists())

        # A different workspace must not see it.
        other, _ = make_ctx(self.root, workspace_name="ws2")
        listed = await list_memories({}, other)
        self.assertEqual(listed["data"]["entries"], [])

        # The owning workspace sees it, and delete finds it without an explicit scope.
        listed = await list_memories({}, ctx_ws)
        self.assertEqual([e["id"] for e in listed["data"]["entries"]], ["task_x"])
        self.assertEqual(listed["data"]["entries"][0]["workspace"], "ws1")
        deleted = await delete_memory({"id": "task_x", "book": "projects"}, ctx_ws)
        self.assertTrue(deleted["ok"], deleted)
        self.assertFalse(book_path(self.root, "child_a/@ws1", "projects").exists())

    async def test_delete_cannot_escape_into_another_memory_namespace(self) -> None:
        target = book_path(self.root, 'other_node', 'people')
        target.parent.mkdir(parents=True)
        target.write_text(yaml.safe_dump({'book': 'people', 'entries': [{'id': 'entry', 'content': 'keep'}]}))
        before = target.read_bytes()
        ctx, seen = make_ctx(self.root)
        result = await manage_memory({'action': 'delete', 'book': '../other_node/people', 'id': 'entry'}, ctx)
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(seen, [])
        await ctx.http.aclose()

    async def test_atomic_write_does_not_follow_a_preexisting_temp_symlink(self) -> None:
        from engine.builtin.knowledge_inject import _save_book
        target = book_path(self.root, 'child_a', 'people')
        target.parent.mkdir(parents=True)
        outside = self.root / 'unrelated.txt'
        outside.write_text('keep')
        trap = target.parent / '.people.yaml.tmp'
        try:
            trap.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable on this platform')
        _save_book(target, {'book': 'people', 'entries': []})
        self.assertEqual(outside.read_text(), 'keep')
        self.assertTrue(trap.is_symlink())
        self.assertEqual(yaml.safe_load(target.read_text())['entries'], [])

    async def test_node_scope_bypasses_workspace_directory(self) -> None:
        ctx, _ = make_ctx(self.root, workspace_name="ws1")
        res = await manage_memory(
            {
                "action": "upsert", "id": "shared", "content": "node level",
                "book": "rules", "scope": "node",
            },
            ctx,
        )
        self.assertTrue(res["ok"], res)
        self.assertTrue(book_path(self.root, "child_a", "rules").exists())
        self.assertFalse(book_path(self.root, "child_a/@ws1", "rules").exists())

    async def test_upsert_without_node_ids_keeps_existing_visibility(self) -> None:
        ctx, _ = make_ctx(self.root)
        await manage_memory(
            {
                "action": "upsert", "id": "private_one", "content": "v1",
                "book": "rules", "scope": "node", "node_ids": ["child_a"],
            },
            ctx,
        )
        await manage_memory(
            {"action": "upsert", "id": "private_one", "content": "v2", "book": "rules", "scope": "node"},
            ctx,
        )
        data = yaml.safe_load(book_path(self.root, "child_a", "rules").read_text(encoding="utf-8"))
        self.assertEqual(data["entries"][0]["node_ids"], ["child_a"])

    async def test_list_with_id_returns_full_untruncated_content(self) -> None:
        ctx, _ = make_ctx(self.root)
        long_body = "x" * 900
        await manage_memory(
            {"action": "upsert", "id": "long", "content": long_body, "book": "notes", "scope": "node"},
            ctx,
        )
        short = await list_memories({"book": "notes"}, ctx)
        self.assertLessEqual(len(short["data"]["entries"][0]["content"]), 200)
        full = await list_memories({"book": "notes", "id": "long"}, ctx)
        self.assertEqual(full["data"]["entries"][0]["content"], long_body)

    async def test_manage_skill_upsert_writes_through_the_guarded_path(self) -> None:
        ctx, seen = make_ctx(self.root)
        res = await manage_skill(
            {"action": "upsert", "name": "demo", "description": "demo skill", "content": "# Demo\n"},
            ctx,
        )
        self.assertTrue(res["ok"], res)
        target = self.root / "skills" / "demo" / "SKILL.md"
        self.assertTrue(target.exists())
        ops = [r for r in seen if r.url.path.endswith("/v1/ops/request")]
        self.assertEqual(len(ops), 1)
        self.assertIn(str(target), ops[0].read().decode())


class SystemNodeConstantBaselineTests(TempRootCase):
    async def test_system_node_constant_block_comes_from_routed_namespace(self) -> None:
        source = self.root / "data" / "memory" / "child_source"
        source.mkdir(parents=True)
        (source / "rules.yaml").write_text(
            yaml.safe_dump({"book": "rules", "entries": [
                {"id": "baseline", "content": "CONST-BODY", "constant": True,
                 "enabled": True, "keywords": [], "priority": 0},
                {"id": "loose", "content": "LOOSE-BODY", "constant": False,
                 "enabled": True, "keywords": [], "priority": 0},
            ]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        data = yaml.safe_load((NODES_DIR / "system.dream.yaml").read_text(encoding="utf-8"))
        node = _node_from_dict(self.root, "system.dream", data)
        _skill_static, _skill_dyn, mem_static, mem_dyn = build_knowledge_context(
            self.root, node, "", [], {}, task_context={"_memory_route_ns": "child_source"},
        )
        text = "\n".join(m["content"] for m in mem_static)
        self.assertIn("[MEMORY:CONSTANT]", text)
        self.assertIn("CONST-BODY", text)
        self.assertNotIn("LOOSE-BODY", text)
        self.assertEqual(mem_dyn, [])


class SystemNodeMemoryProtectionTests(TempRootCase):
    async def test_system_node_cannot_create_or_modify_constant_entries(self) -> None:
        # A constant baseline is seeded directly (only a human/admin path may do it).
        seeded_book = book_path(self.root, "system.memory_extractor", "rules")
        seeded_book.parent.mkdir(parents=True, exist_ok=True)
        seeded_book.write_text(
            yaml.safe_dump({"book": "rules", "entries": [
                {"id": "baseline", "content": "always", "constant": True,
                 "enabled": True, "keywords": [], "priority": 0, "scan_depth": 0},
            ]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        sys_ctx, _ = make_ctx(self.root, node_id="system.memory_extractor")
        rejected = await manage_memory(
            {"action": "upsert", "id": "new_rule", "content": "x", "book": "rules", "constant": True},
            sys_ctx,
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("constant", rejected["error"])

        overwrite = await manage_memory(
            {"action": "upsert", "id": "baseline", "content": "changed", "book": "rules"},
            sys_ctx,
        )
        self.assertFalse(overwrite["ok"])
        self.assertIn("cannot modify constant memory", overwrite["error"])

        deleted = await delete_memory({"id": "baseline", "book": "rules"}, sys_ctx)
        self.assertFalse(deleted["ok"])

    async def test_system_node_writes_are_tagged_auto(self) -> None:
        ctx, _ = make_ctx(self.root, node_id="system.memory_extractor")
        res = await manage_memory(
            {"action": "upsert", "id": "auto_one", "content": "auto", "book": "people", "scope": "node"},
            ctx,
        )
        self.assertTrue(res["ok"], res)
        data = yaml.safe_load(book_path(self.root, "system.memory_extractor", "people").read_text(encoding="utf-8"))
        self.assertEqual(data["entries"][0]["source"], "auto")
        self.assertFalse(data["entries"][0]["constant"])
        self.assertEqual(data["entries"][0]["scan_depth"], 0)


# ---------------------------------------------------------------------------
#  Injection filtering: allowlists and memories:constant
# ---------------------------------------------------------------------------


def memory_entry(book: str, entry_id: str, *, constant: bool = False, keywords=None) -> dict:
    return {
        "kind": "memory", "book": book, "id": entry_id, "content": "body",
        "constant": constant, "keywords": keywords or [], "compiled_keywords": [],
        "priority": 0, "order": 0, "scan_depth": 0, "node_ids": [],
        "strategy": "constant" if constant else ("keyword" if keywords else "index"),
    }


class InjectionFilterTests(unittest.TestCase):
    def test_empty_memory_allowlist_rejects_everything(self) -> None:
        entries = [memory_entry("people", "a"), memory_entry("rules", "b")]
        self.assertEqual(
            _filter_entries(entries, memory_mode="allowlist", memory_allow=[]), [],
        )
        self.assertEqual(_filter_entries(entries, memory_mode="allowlist", memory_allow=None), [])
        self.assertEqual(
            [e["id"] for e in _filter_entries(entries, memory_mode="allowlist", memory_allow=["rules"])],
            ["b"],
        )

    def test_empty_skill_allowlist_rejects_everything(self) -> None:
        entries = [{"kind": "skill", "id": "s1", "content": "x", "strategy": "constant"}]
        self.assertEqual(_filter_entries(entries, skill_mode="allowlist", skill_allow=[]), [])
        self.assertEqual(_filter_entries(entries, skill_mode="allowlist", skill_allow=None), [])

    def test_memory_mode_constant_keeps_only_constant_entries(self) -> None:
        entries = [
            memory_entry("rules", "const", constant=True),
            memory_entry("rules", "kw", keywords=["x"]),
            memory_entry("rules", "idx"),
        ]
        kept = _filter_entries(entries, memory_mode="constant")
        self.assertEqual([e["id"] for e in kept], ["const"])

    def test_keywordless_memory_becomes_index_entry(self) -> None:
        catalog = [{
            "book": "notes", "id": "idx", "content": "long text", "keywords": [],
            "compiled_keywords": [], "enabled": True, "constant": False,
        }]
        entries = normalize_memory_entries(catalog)
        self.assertEqual(entries[0]["strategy"], "index")
        buckets = _classify_and_match_entries(Path("."), entries)
        self.assertEqual([e["id"] for e in buckets["memory_index"]], ["idx"])

    def test_index_entries_render_with_read_back_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = normalize_memory_entries([{
                "book": "notes", "id": "idx", "content": "durable note body",
                "keywords": [], "compiled_keywords": [], "enabled": True, "constant": False,
            }])
            _skill_static, _skill_dyn, mem_static, _mem_dyn = build_knowledge_messages(
                root, entries, memory_mode="all",
            )
        text = "\n".join(m["content"] for m in mem_static)
        self.assertIn("[MEMORY:INDEX]", text)
        self.assertIn("notes/idx", text)
        self.assertIn('manage_memory(action="list"', text)


# ---------------------------------------------------------------------------
#  Dream router
# ---------------------------------------------------------------------------


def hook_ctx(node_id: str, tool_name: str, args: dict, tool_ctx, route_ns: str = "", route=None):
    rctx = SimpleNamespace(task_context={})
    if route_ns:
        rctx.task_context["_memory_route_ns"] = route_ns
    if route is not None:
        rctx.task_context["_memory_route"] = route
    return SimpleNamespace(
        node=SimpleNamespace(id=node_id),
        tool_call=SimpleNamespace(name=tool_name),
        extra={"tool_ctx": tool_ctx, "tool_args": args},
        rctx=rctx,
    )


class DreamRouterTests(TempRootCase):
    async def test_routes_manage_memory_and_preserves_caller_identity(self) -> None:
        ctx, _ = make_ctx(self.root, node_id="system.memory_extractor")
        router = DreamMemoryRouter()
        hook = hook_ctx(
            "system.memory_extractor", "manage_memory",
            {"action": "upsert", "id": "a", "content": "b"}, ctx,
            route_ns="child_source",
        )
        self.assertIsNone(await router.handle(hook))
        self.assertEqual(ctx.node_id, "system.memory_extractor")
        self.assertEqual(ctx._node_id, "system.memory_extractor")
        self.assertEqual(ctx._memory_target, ("child_source", ""))
        self.assertEqual(ctx._caller_node_id, "system.memory_extractor")

    async def test_retired_tool_names_are_not_routed(self) -> None:
        ctx, _ = make_ctx(self.root, node_id="system.dream")
        router = DreamMemoryRouter()
        for retired in ("save_memory", "delete_memory", "list_memories"):
            hook = hook_ctx("system.dream", retired, {"id": "a"}, ctx, route_ns="child_source")
            self.assertIsNone(await router.handle(hook))
            self.assertEqual(ctx.node_id, "system.dream", retired)

    async def test_dream_delete_uses_exact_book_id_route(self) -> None:
        ctx, _ = make_ctx(self.root, node_id="system.dream")
        router = DreamMemoryRouter()
        route = {
            "people": {"ns": "node_a", "ws": ""},
            "people/p1": {"ns": "node_b", "ws": "@ws1"},
        }
        hook = hook_ctx(
            "system.dream", "manage_memory",
            {"action": "delete", "book": "people", "id": "p1"}, ctx, route=route,
        )
        self.assertIsNone(await router.handle(hook))
        self.assertEqual(ctx.node_id, "system.dream")
        self.assertEqual(ctx.workspace_name, "")
        self.assertEqual(ctx._memory_target, ("node_b", "ws1"))

    async def test_dream_upsert_lands_node_level(self) -> None:
        ctx, _ = make_ctx(self.root, node_id="system.dream", workspace_name="stale")
        router = DreamMemoryRouter()
        route = {"people": {"ns": "node_a", "ws": "@ws1"}}
        hook = hook_ctx(
            "system.dream", "manage_memory",
            {"action": "upsert", "book": "people", "id": "p2", "content": "x"}, ctx, route=route,
        )
        self.assertIsNone(await router.handle(hook))
        self.assertEqual(ctx.node_id, "system.dream")
        self.assertEqual(ctx.workspace_name, "stale")
        self.assertEqual(ctx._memory_target, ("node_a", ""))
        result = await manage_memory(hook.extra['tool_args'], ctx)
        self.assertTrue(result['ok'], result)
        self.assertTrue(book_path(self.root, 'node_a', 'people').exists())
        self.assertFalse(book_path(self.root, 'node_a/@stale', 'people').exists())

    async def test_approval_identity_is_the_system_node(self) -> None:
        """A redirected write must ask policy as system.dream, not as the target ns."""
        ctx, seen = make_ctx(self.root, node_id="system.dream")
        router = DreamMemoryRouter()
        hook = hook_ctx("system.dream", "manage_memory",
                        {"action": "upsert", "book": "people", "id": "p3", "content": "x"},
                        ctx, route_ns="node_a")
        await router.handle(hook)
        res = await manage_memory(
            {"action": "upsert", "book": "people", "id": "p3", "content": "x"}, ctx,
        )
        self.assertTrue(res["ok"], res)
        ops = [r for r in seen if r.url.path.endswith("/v1/ops/request")]
        self.assertEqual(len(ops), 1)
        self.assertEqual(json.loads(ops[0].read())['node_id'], 'system.dream')
        self.assertEqual(ctx.node_id, 'system.dream')
        self.assertTrue(book_path(self.root, 'node_a', 'people').exists())


# ---------------------------------------------------------------------------
#  Background extraction
# ---------------------------------------------------------------------------


def fake_task(
    *,
    task_id: str = "t1",
    node_id: str = "entry",
    result: dict | None = None,
    kind: str = "node",
    input_data: dict | None = None,
    session_id: str = "sess-1",
    caller_task_id: str | None = None,
):
    return SimpleNamespace(
        task_id=task_id,
        node_id=node_id,
        kind=kind,
        session_id=session_id,
        session_generation=1,
        caller_task_id=caller_task_id,
        input=input_data or {},
        result=result if result is not None else {"action": "finish"},
    )


def message(role: str, content: str, *, task_id: str = "t1", message_type: str = "", meta=None):
    return SimpleNamespace(
        role=role, content=content, source_task_id=task_id,
        message_type=message_type, meta=meta or {},
    )


RUNTIME = {
    "entry_node_id": "entry",
    "memory": {"auto_extract": {
        "enabled": True, "min_increment": 3, "min_messages": 1,
        "idle_fallback_delay_sec": 60, "min_fallback_tasks": 1,
    }},
}


class BackgroundExtractHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.handler = mem_extract.MemoryExtractHandler()
        self.created: list[dict] = []
        self.timers: list[tuple[int, str]] = []
        self.messages: list[object] = []
        self.ctx = {
            "session_id": "sess-1",
            "workspace_root": self.root,
            "create_task": lambda **kw: self.created.append(kw),
            "acquire_lock": lambda: _NullLock(),
            "current_session_generation": lambda sid: 1,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patch(self):
        messages = self.messages

        class _FakeStore:
            def __init__(self, _path):
                self.path = _path

            def load(self, _sid):
                return list(messages)

        store_cls = _FakeStore
        return [
            patch.object(mem_extract, "load_runtime_config", lambda _root: RUNTIME),
            patch.object(mem_extract, "get_entry_node_id", lambda cfg: cfg.get("entry_node_id", "")),
            patch.object(mem_extract.threading, "Timer", self._fake_timer),
            patch("engine.conversation_store.ConversationStore", store_cls),
        ]

    def _fake_timer(self, delay, fn, args=None):
        timer = SimpleNamespace(delay=delay, fn=fn, args=args or [], daemon=False)
        timer.cancel = lambda: None
        timer.start = lambda: self.timers.append((delay, timer))
        return timer

    async def _complete(self, task, *, messages=None):
        if messages is not None:
            self.messages = messages
        patches = self._patch()
        for p in patches:
            p.start()
        try:
            self.handler.on_task_complete({"task": task, **self.ctx})
        finally:
            for p in patches:
                p.stop()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BackgroundExtractTests(BackgroundExtractHarness):
    async def test_inbound_type_blocks_dispatch_and_tool_results(self) -> None:
        for inbound_type in ("dispatch_result", "async_tool_result"):
            await self._complete(
                fake_task(input_data={"inbound_message_type": inbound_type}),
                messages=[message("user", "hello"), message("assistant", "hi")],
            )
        self.assertEqual(self.created, [])
        self.assertEqual(self.handler._memory_extract_pending_task_ids, {})

    async def test_tool_messages_are_not_dialogue_for_the_minimum(self) -> None:
        self.messages = [
            message("tool", "raw output", message_type="tool_result"),
            message("user", "hello", message_type="tool_result"),
            message("assistant", "hi", message_type="retry_hint"),
        ]
        # Exercise the live gate, not a private helper removed by the migration.
        await self._complete(fake_task())
        self.assertEqual(self.created, [])
        self.assertEqual(self.timers, [])
        self.assertEqual(self.handler._memory_extract_pending, {})

    async def test_batch_at_min_increment_dispatches_immediately(self) -> None:
        for i in range(3):
            tid = f"t{i}"
            await self._complete(
                fake_task(task_id=tid),
                messages=[message("user", "u", task_id=tid), message("assistant", "a", task_id=tid)],
            )
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["node_id"], "system.memory_extractor")
        self.assertTrue(self.created[0]["input_data"]["_system_task"])
        self.assertEqual(self.created[0]["input_data"]["_memory_route_ns"], "entry")
        self.assertIn("User: u", self.created[0]["input_data"]["instruction"])
        self.assertEqual(self.handler._memory_extract_pending_task_ids["sess-1"], [])

    async def test_tail_batch_waits_and_can_be_rearmed(self) -> None:
        good = [message("user", "u", task_id="t0"), message("assistant", "a", task_id="t0")]
        await self._complete(fake_task(task_id="t0"), messages=good)
        self.assertEqual(self.created, [])
        self.assertEqual(len(self.timers), 1)
        self.assertEqual(self.timers[0][0], 60)

        # A memory-writing turn must not extract itself, but earlier pending turns
        # must keep their tail timer alive.
        self.handler._memory_extract_pending.clear()
        self.handler._memory_extract_timers.clear()
        self.timers.clear()
        await self._complete(
            fake_task(
                task_id="t1",
                result={"action": "finish", "_tool_actions": [
                    {"name": "manage_memory", "action": "upsert"},
                ]},
            ),
            messages=good + [message("user", "write", task_id="t1")],
        )
        self.assertEqual(self.created, [])
        self.assertEqual(len(self.timers), 1, "tail timer must be re-armed after a memory write")
        self.assertEqual(self.timers[0][0], 60)

    async def test_mutex_reads_tool_actions_not_retired_names(self) -> None:
        good = [message("user", "u", task_id="t0"), message("assistant", "a", task_id="t0")]
        # Legacy names in _tool_names are no longer the exclusion signal.
        await self._complete(
            fake_task(task_id="t0", result={"action": "finish", "_tool_names": ["save_memory"]}),
            messages=good,
        )
        self.assertEqual(self.handler._memory_extract_pending_task_ids["sess-1"], ["t0"])

        # An explicit manage_memory mutation in _tool_actions blocks the turn.
        await self._complete(
            fake_task(
                task_id="t1",
                result={"action": "finish", "_tool_actions": [
                    {"name": "manage_memory", "action": "delete"},
                ]},
            ),
            messages=good,
        )
        self.assertEqual(self.handler._memory_extract_pending_task_ids["sess-1"], ["t0"])

    async def test_read_only_manage_memory_does_not_block(self) -> None:
        good = [message("user", "u", task_id="t0"), message("assistant", "a", task_id="t0")]
        await self._complete(
            fake_task(
                task_id="t0",
                result={"action": "finish", "_tool_actions": [
                    {"name": "manage_memory", "action": "list"},
                    {"name": "manage_skill", "action": "upsert"},
                ]},
            ),
            messages=good,
        )
        self.assertEqual(self.handler._memory_extract_pending_task_ids["sess-1"], ["t0"])

    async def test_mutation_helper_semantics(self) -> None:
        self.assertTrue(mem_extract._memory_mutation_happened(
            {"_tool_actions": [{"name": "manage_memory", "action": "upsert"}]}))
        self.assertTrue(mem_extract._memory_mutation_happened(
            {"_tool_actions": [{"name": "manage_memory", "action": "delete"}]}))
        self.assertFalse(mem_extract._memory_mutation_happened(
            {"_tool_actions": [{"name": "manage_memory", "action": "list"}]}))
        self.assertFalse(mem_extract._memory_mutation_happened(
            {"_tool_names": ["save_memory"]}))
        self.assertFalse(mem_extract._memory_mutation_happened(None))

    async def test_transcript_drops_tool_output_and_preserves_user_body(self) -> None:
        wrapped = (
            "【群聊上下文记录】\nnoise\n【当前消息引用】\nquoted line\n"
            "【当前用户指令】\nreal instruction"
        )
        text = mem_extract._format_transcript_for_extract([
            message("tool", "raw tool output", message_type="tool_result"),
            message("user", wrapped),
            message("assistant", "answer"),
            message("assistant", "rejected draft", meta={"rejected_draft": True}),
        ])
        self.assertNotIn("raw tool output", text)
        self.assertIn(wrapped, text)
        self.assertNotIn("rejected draft", text)
        self.assertIn("real instruction", text)
        self.assertIn("Assistant: answer", text)


# ---------------------------------------------------------------------------
#  System node declarations
# ---------------------------------------------------------------------------


class SystemNodeYamlTests(unittest.TestCase):
    def _node(self, name: str):
        data = yaml.safe_load((NODES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        node = _node_from_dict(Path(".").resolve(), name, data)
        self.assertIsNotNone(node, name)
        return data, node

    def test_system_memory_nodes_use_unified_tools_only(self) -> None:
        for name in ("system.dream", "system.memory_extractor"):
            data, node = self._node(name)
            allow = data["tool_access"]["allow"]
            for retired in ("save_memory", "delete_memory", "list_memories",
                            "create_or_update_skill", "list_skills"):
                self.assertNotIn(retired, allow, name)
            self.assertIn("manage_memory", allow, name)
            self.assertEqual(node.tool_access.mode, "allowlist", name)

        dream_data, dream = self._node("system.dream")
        self.assertIn("manage_skill", dream_data["tool_access"]["allow"])
        self.assertEqual(dream_data["tool_access"]["deny"], ["manage_skill:delete"])
        self.assertEqual(self._node("system.memory_extractor")[0]["tool_access"]["allow"],
                         ["manage_memory"])

    def test_system_memory_nodes_read_constants_and_inherit_global_model(self) -> None:
        for name in ("system.dream", "system.memory_extractor"):
            data, node = self._node(name)
            self.assertEqual(node.memory_access.mode, "constant", name)
            self.assertEqual(node.model, "", name)
            self.assertEqual(str(data.get("provider") or ""), "", name)
            self.assertEqual(node.output_mode, "hybrid", name)
            self.assertEqual(node.control_tools, "none", name)

    def test_prompts_are_generic_and_free_of_finish_requirements(self) -> None:
        for name in ("system.dream", "system.memory_extractor"):
            data, _node_obj = self._node(name)
            prompt = data["prompt"]
            for retired in ("save_memory", "delete_memory", "list_memories",
                            "create_or_update_skill", "list_skills"):
                self.assertNotIn(retired, prompt, name)
            self.assertNotIn("必须调用 finish", prompt, name)
            self.assertNotIn("finish(text", prompt, name)
            # Production prompts retain their content-selection rules. Migration
            # removes retired tool requirements, not those existing policies.
            self.assertNotIn("constant=true 必须", prompt, name)
            self.assertNotIn("standard 16", prompt, name)

    def test_memory_prompts_preserve_production_content_selection(self) -> None:
        # Confirmed against production on 2026-11-02; no live deployment is
        # needed to run these content-contract checks.
        required = {
            "system.dream": (
                "严禁保存任何“助手（AI）拒绝用户请求”相关的信号或规则",
                "严禁将 AI 拒绝或未能满足用户请求的实例抽象、归纳为任何行为限制规则",
            ),
            "system.memory_extractor": (
                "NEVER extract, summarize, or save any instance where the assistant refused or declined a user/member request",
                "NEVER create any refusal/restriction rule or policy entry from such responses",
            ),
        }
        for name, clauses in required.items():
            prompt = self._node(name)[0]["prompt"]
            for clause in clauses:
                self.assertIn(clause, prompt, name)

    def test_dream_tool_specs_prune_skill_delete_and_hide_control_tools(self) -> None:
        from engine.builtin.knowledge_inject import PLUGIN_META

        specs = [{"name": t["name"], "description": t["description"],
                  "input_schema": t["input_schema"]} for t in PLUGIN_META["tools"]]
        registry = SimpleNamespace(list_specs=lambda: specs)
        _data, dream = self._node("system.dream")
        _pseudo, real = build_node_tool_specs(registry=registry, node=dream)
        enums = {}
        for spec in real:
            fn = spec.get("function") or spec
            enums[fn["name"]] = fn["parameters"]["properties"]["action"]["enum"]
        self.assertEqual(enums["manage_memory"], ["list", "upsert", "delete"])
        self.assertEqual(enums["manage_skill"], ["list", "upsert"])
        # No control pseudo tools (finish/ask) are offered to a hybrid system node.
        self.assertNotIn("finish", enums)


class SystemTextNodeTemplateTests(unittest.TestCase):
    """compactor / turn_summarizer must never ask for a tool they do not have."""

    def _load(self, name: str):
        data = yaml.safe_load((NODES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        node = _node_from_dict(Path(".").resolve(), name, data)
        self.assertIsNotNone(node, name)
        return data, node

    def test_templates_load_as_hybrid_text_nodes_without_tools(self) -> None:
        for name in ("system.compactor", "system.turn_summarizer"):
            data, node = self._load(name)
            self.assertEqual(node.output_mode, "hybrid", name)
            self.assertEqual(node.control_tools, "none", name)
            self.assertEqual(node.tool_access.mode, "none", name)
            self.assertEqual(node.skill_access.mode, "none", name)
            self.assertEqual(node.memory_access.mode, "none", name)
            self.assertEqual(node.model, "", name)
            self.assertEqual(str(data.get("provider") or ""), "", name)
            self.assertEqual(node.prompt.strip() != "", True, name)

    def test_prompts_do_not_require_finish_or_tool_syntax(self) -> None:
        for name in ("system.compactor", "system.turn_summarizer"):
            data, _node_obj = self._load(name)
            prompt = data["prompt"]
            # Production mentions XML tags only to prohibit their output. Exempt
            # those exact negative clauses, not arbitrary occurrences of the tags.
            instructions = prompt.replace("不要包裹 <analysis>、<summary> 标签或代码块围栏", "")
            instructions = instructions.replace("严禁输出 `<analysis>` 或 `<summary>` 等标签", "")
            for banned in ("finish(text", "必须调用 finish", "MUST go into the finish",
                           "调用 finish(text", "<analysis>", "<summary>"):
                self.assertNotIn(banned, instructions, f"{name}: {banned}")
            self.assertIn("严禁调用任何工具", prompt, name)

    def test_text_prompts_preserve_production_output_contract(self) -> None:
        compactor = self._load("system.compactor")[0]["prompt"]
        for clause in (
            "不要包裹 <analysis>、<summary> 标签或代码块围栏",
            "严禁输出 `<analysis>` 或 `<summary>` 等标签",
            "严禁在摘要的任何段落中总结、提及或记录助手（AI）拒绝用户请求的相关内容",
        ):
            self.assertIn(clause, compactor)
        summarizer = self._load("system.turn_summarizer")[0]["prompt"]
        self.assertIn("严禁输出任何工具调用语法或内部标记", summarizer)
        self.assertIn("只直接输出纯文本摘要内容", summarizer)
        self.assertIn("严禁总结、提及或记录任何助手（AI）拒绝用户请求的内容", summarizer)

    def test_compactor_keeps_eight_sections(self) -> None:
        data, _node_obj = self._load("system.compactor")
        prompt = data["prompt"]
        for idx in range(1, 9):
            self.assertIn(f"{idx}. ", prompt)
        self.assertIn("8. 下一步", prompt)

    def test_no_tool_specs_are_composed_for_text_nodes(self) -> None:
        from engine.builtin.knowledge_inject import PLUGIN_META

        specs = [{"name": t["name"], "description": t["description"],
                  "input_schema": t["input_schema"]} for t in PLUGIN_META["tools"]]
        registry = SimpleNamespace(list_specs=lambda: specs)
        for name in ("system.compactor", "system.turn_summarizer"):
            _data, node = self._load(name)
            _pseudo, real = build_node_tool_specs(registry=registry, node=node)
            self.assertEqual(real, [], name)


if __name__ == "__main__":
    unittest.main()
