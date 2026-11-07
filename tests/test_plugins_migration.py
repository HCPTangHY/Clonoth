"""Root plugins/ migration (original ``plugins.ide`` + ``plugins.ide.todo``).

Run:
    TMPDIR=/www/wwwroot/clonoth_original/data/temp/plugins-migration-v8zeqbin \
    PYTHONDONTWRITEBYTECODE=1 python3.11 -B tests/test_plugins_migration.py

Offline only: only the two originals above are imported, no production snapshot
is touched, no supervisor is booted, no service or network is contacted and no
git command runs. Route/hook faces are fakes or an unmounted face, supervisor
state is a handful of attributes, and every temp dir lives under data/temp.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / "data" / "temp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from engine.faces.routes import PluginRoutesFace, _iter_http_routes  # noqa: E402
from engine.registry_core import DisposalLedger  # noqa: E402
import plugins.ide as ide  # noqa: E402
from plugins.ide import todo as ide_todo  # noqa: E402

P = "/v1/plugins/ide"
ACTOR = {"type": "user"}
apply_update, operate = ide_todo.apply_update, ide_todo.operate


def tmpdir(case: unittest.TestCase) -> Path:
    """TemporaryDirectory, always under data/temp (never the host /tmp)."""
    holder = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
    case.addCleanup(holder.cleanup)
    return Path(holder.name)


def expect(case: unittest.TestCase, code: int, fn, *args):
    with case.assertRaises(HTTPException) as ctx:
        fn(*args)
    case.assertEqual(ctx.exception.status_code, code)
    return ctx.exception


def item(**kw) -> dict:
    base = {"id": "i1", "title": "t", "status": "pending", "note": "",
            "created_at": "2026-01-01T00:00:00+00:00"}
    base.update(kw)
    return base


def todo_list(items=(), revision=0, path="/ws") -> dict:
    return {"schema_version": 1, "session_id": "s1", "workspace": {"name": "w", "path": path},
            "revision": revision, "items": list(items), "updated_at": None, "updated_by": None}


def patch(ws="/ws", **kw) -> dict:
    base = {"expected_revision": 0, "workspace_path": str(ws), "changes": [],
            "remove_ids": [], "order": None}
    base.update(kw)
    return base


def contrib(**surfaces):
    """Fake contributions face: one namespace per surface, missing means absent."""
    return SimpleNamespace(get=lambda name: surfaces.get(name))


class Hooks:
    def __init__(self):
        self.calls = []

    def register(self, name, handler, priority=0):
        self.calls.append((name, handler, priority))
        return lambda: None


class Tools:
    def __init__(self):
        self.names = []

    def register_builtin_tool(self, name, *args):
        self.names.append(name)
        return lambda: None


class TruncatedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        ide._expand_cache.clear()
        self.tmp = tmpdir(self)

    def test_reads_exactly_131076_bytes_and_caps_at_32k_chars(self) -> None:
        target = self.tmp / "big.txt"
        target.write_text("a" * 200_000, encoding="utf-8")

        class Spy:  # counts what the plugin actually pulls off disk
            def __init__(self, fh): self._fh, self.total = fh, 0

            def read(self, n=-1):
                data = self._fh.read(n); self.total += len(data); return data

            def __enter__(self): return self
            def __exit__(self, *_): self._fh.close()

        real_open, spy = Path.open, {}

        def keep_open(path, *args, **kwargs):
            spy["fh"] = Spy(real_open(path, *args, **kwargs)); return spy["fh"]

        with mock.patch.object(Path, "open", keep_open):
            text = ide._read_truncated(target)
        self.assertEqual(ide._MAX_EXPAND_READ_BYTES, 131_076)
        self.assertEqual(spy["fh"].total, 131_076)  # bounded: the file is 200 KB
        self.assertEqual(text, "a" * 32_768 + "\n... (truncated)")

    def test_utf8_sequence_split_at_read_boundary_keeps_prefix(self) -> None:
        target = self.tmp / "cut.txt"
        target.write_bytes(b"a" * 131_075 + "✓".encode() + b"a" * 32)
        text = ide._read_truncated(target)  # None would mean the cut byte is fatal
        self.assertIsInstance(text, str)
        self.assertTrue(text.endswith("\n... (truncated)"))

    def test_binary_and_invalid_utf8_return_none(self) -> None:
        binary, broken = self.tmp / "blob.bin", self.tmp / "broken.txt"
        binary.write_bytes(b"\x00\x01\x02abc")
        broken.write_bytes(b"abc\xff\xfe")
        self.assertIsNone(ide._read_truncated(binary))
        self.assertIsNone(ide._read_truncated(broken))

    def test_missing_file_returns_none_and_drops_cache_entry(self) -> None:
        target = self.tmp / "gone.txt"
        target.write_text("x", encoding="utf-8")
        self.assertEqual(ide._read_truncated(target), "x")
        target.unlink()
        self.assertIsNone(ide._read_truncated(target))
        self.assertNotIn(target.as_posix(), ide._expand_cache)

    def test_mtime_bump_invalidates_cache(self) -> None:
        target = self.tmp / "edit.txt"
        target.write_text("one", encoding="utf-8")
        self.assertEqual(ide._read_truncated(target), "one")
        target.write_text("two!", encoding="utf-8")
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertEqual(ide._read_truncated(target), "two!")

    def test_cache_is_bounded_to_128_entries(self) -> None:
        for index in range(129):
            target = self.tmp / f"f{index}.txt"
            target.write_text(str(index), encoding="utf-8")
            self.assertEqual(ide._read_truncated(target), str(index))
        self.assertEqual(len(ide._expand_cache), ide._MAX_EXPAND_CACHE_ENTRIES)
        self.assertEqual(len(ide._expand_cache), 128)
        self.assertNotIn((self.tmp / "f0.txt").as_posix(), ide._expand_cache)


class MentionExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        ide._expand_cache.clear()
        self.tmp = tmpdir(self)
        self.ws = self.tmp / "ws"
        (self.ws / "pkg").mkdir(parents=True)
        (self.ws / "a.py").write_text("A=1", encoding="utf-8")
        (self.ws / "pkg" / "b.py").write_text("B=2", encoding="utf-8")
        (self.tmp / "outside.txt").write_text("SECRET", encoding="utf-8")

    def expand(self, messages, **extra):
        ide._expand_file_mentions(SimpleNamespace(extra=extra, rctx=None, messages=messages))
        return messages

    def test_duplicate_mentions_expand_once_per_request(self) -> None:
        messages = self.expand([{"role": "user", "content": "@a.py then @a.py again"},
                                {"role": "user", "content": "later @a.py and @pkg/b.py"}],
                               workspace=str(self.ws))
        first, second = messages[0]["content"], messages[1]["content"]
        self.assertEqual(first.count("[Referenced file: a.py]"), 1)
        self.assertIn("```\nA=1\n```", first)
        # Already expanded above: the second message only contributes its own file.
        self.assertNotIn("[Referenced file: a.py]", second)
        self.assertIn("[Referenced file: pkg/b.py]", second)
        self.assertTrue(second.startswith("later @a.py and @pkg/b.py"))

    def test_paths_outside_workspace_are_skipped(self) -> None:
        original = "@../outside.txt @/etc/passwd"
        messages = self.expand([{"role": "user", "content": original}], workspace=str(self.ws))
        self.assertEqual(messages[0]["content"], original)
        self.assertEqual(ide._expand_cache, {})

    def test_multimodal_text_part_expanded_others_preserved(self) -> None:
        parts = [{"type": "text", "text": "@a.py"}, {"type": "image_url", "image_url": {"url": "x"}}]
        messages = self.expand([{"role": "user", "content": parts}], workspace=str(self.ws))
        self.assertIs(messages[0]["content"], parts)
        self.assertIn("[Referenced file: a.py]", parts[0]["text"])
        self.assertEqual(parts[1], {"type": "image_url", "image_url": {"url": "x"}})

    def test_plain_words_dot_prefix_and_non_user_roles(self) -> None:
        messages = self.expand([{"role": "user", "content": "hi @foo and @./a.py"},
                                {"role": "assistant", "content": "@a.py"}], workspace=str(self.ws))
        content = messages[0]["content"]
        self.assertEqual(content.count("[Referenced file:"), 1)  # "@foo" is not a path
        self.assertIn("[Referenced file: a.py]", content)  # "./" stripped from the header
        self.assertEqual(messages[1]["content"], "@a.py")

    def test_without_workspace_is_a_noop(self) -> None:
        messages = self.expand([{"role": "user", "content": "@a.py"}])
        self.assertEqual(messages[0]["content"], "@a.py")
        # workspace_root is the documented engine fallback, tried after workspace.
        messages = self.expand([{"role": "user", "content": "@a.py"}], workspace_root=str(self.ws))
        self.assertIn("[Referenced file: a.py]", messages[0]["content"])


class EngineRegistrationTests(unittest.TestCase):
    def test_registers_only_before_llm_call(self) -> None:
        hooks, tools, called = Hooks(), Tools(), []
        ctx = SimpleNamespace(contributions=contrib(tools=tools), hooks=hooks)
        with mock.patch.object(ide_todo, "register_engine", lambda c: called.append(c)):
            disposers = ide.register(ctx)
        self.assertEqual([call[0] for call in hooks.calls], ["before_llm_call"])  # no before_step
        self.assertIs(hooks.calls[0][1], ide._expand_file_mentions)
        self.assertEqual(hooks.calls[0][2], 50)
        self.assertEqual((tools.names, called), ([], []))  # no todos tools, no todo.register_engine
        self.assertEqual(len(disposers), 1)
        self.assertTrue(callable(disposers[0]))


class SupervisorRoutesTests(unittest.TestCase):
    def face(self) -> PluginRoutesFace:
        ledger, face = DisposalLedger(), PluginRoutesFace()
        face.set_disposal_ledger(ledger)
        ctx = SimpleNamespace(contributions=contrib(routes=face), hooks=Hooks())
        with ledger.collecting("ide"):
            ide.register(ctx)
        return face

    def test_four_todo_endpoints_and_api_paths(self) -> None:
        records = self.face().list_routes("ide")
        pairs = {(r["method"], r["path"]) for rec in records for r in rec["routes"]}
        self.assertEqual({p for p in pairs if p[1].startswith(P + "/todos")}, {
            ("GET", P + "/todos"), ("PATCH", P + "/todos"),
            ("GET", P + "/todos/task/{task_id}"), ("PATCH", P + "/todos/task/{task_id}")})
        for pair in [("PUT", P + "/file"), ("GET", P + "/search"), ("POST", P + "/fs/mkdir"),
                     ("GET", P + "/git/status"), ("POST", P + "/git/commit")]:
            self.assertIn(pair, pairs)
        self.assertTrue(all(path.startswith(P) for _, path in pairs))
        self.assertTrue(all(rec["public"] is False for rec in records))  # global /v1/ auth
        self.assertTrue(all(rec["mounted"] is False for rec in records))  # face not attached

    def test_panel_static_resources(self) -> None:
        face = self.face()
        pairs = {(r["method"], r["path"]) for rec in face.list_routes("ide") for r in rec["routes"]}
        self.assertIn(("GET", P + "/web/{file_path:path}"), pairs)
        self.assertTrue((ROOT / "plugins/ide/web/index.html").is_file())
        route = next(r for decl in face._declarations for r in _iter_http_routes(decl.router)
                     if r.path.endswith("/web/{file_path:path}"))
        serve = route.endpoint
        self.assertTrue(getattr(serve, "_clonoth_panel_static", False))

        async def probe():  # handler called directly: no server, no socket
            index, script = await serve("index.html"), await serve("js/main.js")
            with self.assertRaises(HTTPException) as blocked:
                await serve("../todo.py")
            return index, script, blocked.exception.status_code

        index, script, code = asyncio.run(probe())
        self.assertEqual(Path(index.path).name, "index.html")
        self.assertTrue(Path(index.path).is_file())
        self.assertEqual(Path(script.path).parts[-2:], ("js", "main.js"))
        self.assertEqual(code, 404)  # traversal refused


class ApplyUpdateTests(unittest.TestCase):
    def test_add_appends_and_bumps_revision(self) -> None:
        current = todo_list()
        result = apply_update(current, patch(changes=[{"id": None, "title": "A"}]), ACTOR)
        self.assertEqual(result["revision"], 1)
        self.assertEqual([(i["title"], i["status"]) for i in result["items"]], [("A", "pending")])
        self.assertEqual(result["items"][0]["updated_by"], ACTOR)
        self.assertEqual(current["items"], [])  # input stays pristine

    def test_revision_conflict_and_workspace_mismatch(self) -> None:
        expect(self, 409, apply_update, todo_list(revision=3), patch(), ACTOR)
        expect(self, 409, apply_update, todo_list(), patch(ws="/elsewhere"), ACTOR)

    def test_failed_batch_leaves_input_untouched(self) -> None:
        current = todo_list([item()], revision=2)
        bad = patch(expected_revision=2,
                    changes=[{"id": "i1", "title": "renamed"}, {"id": "ghost", "title": "x"}])
        snapshot = (repr(current), repr(bad))
        expect(self, 404, apply_update, current, bad, ACTOR)  # second change is invalid
        self.assertEqual((repr(current), repr(bad)), snapshot)

    def test_blocked_and_cancelled_require_note(self) -> None:
        current = todo_list([item()])
        expect(self, 422, apply_update, current, patch(changes=[{"id": "i1", "status": "blocked"}]), ACTOR)
        ok = apply_update(todo_list([item()]),
                          patch(changes=[{"id": "i1", "status": "cancelled", "note": "dropped"}]), ACTOR)
        self.assertEqual((ok["items"][0]["status"], ok["items"][0]["note"]), ("cancelled", "dropped"))
        expect(self, 422, apply_update, current, patch(changes=[{"id": "i1", "status": "bogus"}]), ACTOR)

    def test_field_limits_and_unknown_fields(self) -> None:
        current = todo_list([item()])
        expect(self, 422, apply_update, current, patch(changes=[{"id": "i1", "title": "t" * 501}]), ACTOR)
        expect(self, 422, apply_update, current, patch(changes=[{"id": "i1", "note": "n" * 2001}]), ACTOR)
        expect(self, 422, apply_update, current, patch(changes=[{"id": "i1", "extra": 1}]), ACTOR)
        expect(self, 422, apply_update, current, patch(expected_revision=True), ACTOR)  # bool is not int
        expect(self, 422, apply_update, current, patch(unknown=1), ACTOR)

    def test_item_cap_is_200(self) -> None:
        full = todo_list([item(id=f"i{n}") for n in range(200)])
        self.assertEqual(apply_update(full, patch(), ACTOR)["revision"], 0)  # 200 is allowed
        expect(self, 422, apply_update, full, patch(changes=[{"id": None, "title": "over"}]), ACTOR)
        expect(self, 422, apply_update, todo_list(), patch(changes=[{"id": None, "title": "x"}] * 201), ACTOR)

    def test_remove_only_completed_or_cancelled(self) -> None:
        done = todo_list([item(status="completed")])
        expect(self, 422, apply_update, todo_list([item()]), patch(remove_ids=["i1"]), ACTOR)
        self.assertEqual(apply_update(done, patch(remove_ids=["i1"]), ACTOR)["items"], [])
        expect(self, 404, apply_update, done, patch(remove_ids=["nope"]), ACTOR)
        expect(self, 422, apply_update, done, patch(remove_ids=["i1", "i1"]), ACTOR)

    def test_noop_patch_and_order_validation(self) -> None:
        current = todo_list([item(), item(id="i2", title="b")], revision=4)
        noop = apply_update(current, patch(expected_revision=4), ACTOR)
        self.assertEqual((noop["revision"], noop["items"]), (4, current["items"]))
        expect(self, 422, apply_update, current, patch(expected_revision=4, order=[]), ACTOR)
        expect(self, 422, apply_update, current, patch(expected_revision=4, order=["i1"]), ACTOR)
        ordered = apply_update(current, patch(expected_revision=4, order=["i2", "i1"]), ACTOR)
        self.assertEqual([i["id"] for i in ordered["items"]], ["i2", "i1"])


class State:
    """Minimal SupervisorState stand-in: lock, session registry, workspaces, generations."""

    def __init__(self, root: Path, records: dict, workspaces: dict):
        self.workspace_root = Path(root)
        self._lock = threading.RLock()
        self._session_store = SimpleNamespace(_registry=records)
        self.sessions = {sid: SimpleNamespace(workspace=ws) for sid, ws in workspaces.items()}
        self.session_generations: dict[str, int] = {}
        self.tasks: dict[str, object] = {}

    def get_session_workspace(self, sid):
        info = self.sessions.get(sid)
        return dict(info.workspace) if info is not None else None


class OperateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tmpdir(self)
        self.ws, self.other = self.tmp / "ws", self.tmp / "other"
        self.ws.mkdir()
        self.other.mkdir()

    def state(self, workspaces=None) -> State:
        records = {"s1": {"node_id": "main"}, "s2": {"node_id": "main"},
                   "child": {"node_id": "c", "is_child": True},
                   "reset": {"node_id": "main", "reset": True}, "bare": {"node_id": "main"}}
        spaces = {sid: {"name": "w", "path": str(self.ws)}
                  for sid in ("s1", "s2", "child", "reset")}
        spaces["bare"] = {"name": "w"}
        spaces.update(workspaces or {})
        return State(self.tmp, records, spaces)

    def test_read_write_and_session_isolation(self) -> None:
        state = self.state()
        self.assertEqual(operate(state, "s1")["revision"], 0)
        self.assertEqual(operate(state, "s1", patch(self.ws, changes=[{"id": None, "title": "A"}]))["revision"], 1)
        self.assertEqual(operate(state, "s1")["items"][0]["title"], "A")
        self.assertEqual(operate(state, "s2")["items"], [])  # sibling session: own list
        self.assertEqual(len(list((self.tmp / "data" / "ide" / "todos").glob("*.json"))), 1)

    def test_workspace_switch_is_a_different_list(self) -> None:
        state = self.state({"s1": {"name": "o", "path": str(self.other)}})
        self.assertEqual(operate(state, "s1")["items"], [])  # does not read the ws/ list
        expect(self, 409, operate, state, "s1", patch(self.ws, changes=[{"id": None, "title": "stale"}]))
        self.assertEqual(operate(state, "s1", patch(self.other, changes=[{"id": None, "title": "B"}]))
                         ["workspace"]["path"], str(self.other.resolve()))
        expect(self, 409, operate, state, "s1", patch(self.ws))

    def test_access_denials_and_workspace_requirement(self) -> None:
        state = self.state()
        expect(self, 403, operate, state, "child")  # child sessions never inherit
        expect(self, 404, operate, state, "reset")
        expect(self, 404, operate, state, "missing")
        expect(self, 400, operate, state, "bare")  # no workspace bound
        missing = self.state({"s1": {"name": "w", "path": str(self.tmp / "nope")}})
        expect(self, 409, operate, missing, "s1")  # directory gone

    def test_entry_branch_redirects_to_parent(self) -> None:
        state = self.state()
        state._session_store._registry["branch"] = {"node_id": "__entry_branch__",
                                                    "parent_session_id": "s1"}
        state.sessions["branch"] = SimpleNamespace(workspace={"name": "w", "path": str(self.ws)})
        self.assertEqual(operate(state, "branch"), operate(state, "s1"))

    def test_save_read_roundtrip(self) -> None:
        space = {"name": "w", "path": str(self.ws.resolve())}
        self.assertEqual(ide_todo.read_list(self.tmp, "s1", space)["items"], [])
        data = {**todo_list([item(status="completed")], revision=3), "workspace": space}
        ide_todo.save_list(self.tmp, data)
        loaded = ide_todo.read_list(self.tmp, "s1", space)
        self.assertEqual((loaded["revision"], loaded["items"]), (3, data["items"]))
        self.assertEqual(loaded["workspace"], space)

    def test_corrupt_file_is_never_overwritten(self) -> None:
        space = {"name": "w", "path": str(self.ws.resolve())}
        path = ide_todo._path(self.tmp, "s1", space)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        expect(self, 500, ide_todo.read_list, self.tmp, "s1", space)
        expect(self, 500, operate, self.state(), "s1", patch(self.ws, changes=[{"id": None, "title": "A"}]))
        self.assertEqual(path.read_bytes(), b"{not json")  # write path never clobbered it
        path.write_bytes(b'{"schema_version": 1, "session_id": "other", "workspace": {"name": "w", '
                         b'"path": "' + str(self.ws.resolve()).encode() + b'"}, "revision": 0, "items": []}')
        expect(self, 500, ide_todo.read_list, self.tmp, "s1", space)  # foreign session id

    def test_oversized_data_is_rejected(self) -> None:
        space = {"name": "w", "path": str(self.ws.resolve())}
        data = {**todo_list(revision=1), "workspace": space,
                "items": [item(note="n" * 2000, title="t" * 500)] * 200}
        expect(self, 422, ide_todo.save_list, self.tmp, data)
        self.assertFalse(ide_todo._path(self.tmp, "s1", space).exists())
        path = ide_todo._path(self.tmp, "s1", space)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{" + b" " * ide_todo.MAX_BYTES)
        expect(self, 500, ide_todo.read_list, self.tmp, "s1", space)


if __name__ == "__main__":
    unittest.main(verbosity=2)
