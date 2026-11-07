"""Toolbox file-IO migration contract (read_file / search_in_files).

Covers the production baseline that was migrated onto this tree:

- read_file: numbered ranges (incl. legacy single-path ``start_line``/``end_line``),
  500-line default cap, 64 K char cap, 10 MB head preview + streamed
  startLine/endLine, bounded memory on a single huge line, binary/image shapes,
  batch + partial failure, approval-denied reads.
- search_in_files: content / files_only / count modes, glob semantics (``*``,
  ``**``, ``*/**``), per-file read approval before any content is read
  (``.env`` deny, external symlink approval/deny), per-file write approval in
  replace mode, literal vs regex replacement, ``$1`` capture groups and escaped
  backslashes, workspace-root ``data/`` skip vs an explicitly named data sub-path,
  binary / oversized file skipping.

No real tool request, no network, no supervisor: every tool call uses a tempdir
workspace and a duck-typed context whose async hooks are ``AsyncMock``.

Run:
    python3.11 -B -m unittest tests.test_toolbox_io_migration -v
"""
from __future__ import annotations

import asyncio
import importlib
import json
import struct
import sys
import tempfile
import tracemalloc
import unittest
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ``toolbox/builtins/__init__.py`` re-exports the *functions* under the same names
# as their modules, so ``import toolbox.builtins.read_file as m`` binds the function
# and ``patch.object`` on it fails. importlib is the only way to reach the module.
rf_mod = importlib.import_module("toolbox.builtins.read_file")
sf_mod = importlib.import_module("toolbox.builtins.search_in_files")
from toolbox.builtins.read_file import _is_binary_prefix, _read_single_file_sync, read_file
from toolbox.builtins.search_in_files import _match_pattern, search_in_files


# ---------------------------------------------------------------------------
#  Fake tool context (no supervisor, no HTTP)
# ---------------------------------------------------------------------------

class FakeCtx:
    """Duck-typed ``ToolContext``: only the attributes these tools touch."""

    def __init__(self, workspace_root, *, workspace=None, op_response=None, approval=None,
                 op_handler=None):
        self.workspace_root = Path(workspace_root)
        self.workspace = Path(workspace) if workspace is not None else None
        self.session_id = "session"
        self.parent_session_id = ""
        self.run_id = "run"
        self.worker_id = "worker"
        self.task_id = "task"
        self.supervisor_url = "http://127.0.0.1:1"
        self.http = AsyncMock()
        self.registry = SimpleNamespace()
        if op_handler is not None:
            # Per-path policy: the handler sees (op, params) exactly like the
            # supervisor's /v1/ops/request does.
            self.request_op = AsyncMock(side_effect=op_handler)
        else:
            self.request_op = AsyncMock(
                return_value={"safety_level": "allow"} if op_response is None else op_response
            )
        self.check_cancelled = AsyncMock(return_value=False)
        self.wait_for_approval = AsyncMock(
            return_value={"status": "allowed"} if approval is None else approval
        )

    @property
    def effective_workspace(self) -> Path:
        return self.workspace if self.workspace is not None else self.workspace_root

    def requested_ops(self) -> list[str]:
        return [c.args[0] for c in self.request_op.await_args_list]

    def requested_paths(self) -> list[str]:
        return [str(c.args[1].get("path", "")) for c in self.request_op.await_args_list]

    def requests(self) -> list[tuple[str, str]]:
        return list(zip(self.requested_ops(), self.requested_paths()))


def policy_handler(read_rules=(), write_rules=(), default=None):
    """Build an ``op_handler`` that answers per (op, path) like the policy engine.

    ``*_rules`` are ``(fnmatch_pattern, response)`` pairs matched against the
    absolute path handed to the guard.
    """
    fallback = dict(default or {"safety_level": "allow"})

    def _handler(op, params):
        path = str((params or {}).get("path", ""))
        rules = read_rules if op == "read_file" else write_rules
        for pattern, response in rules:
            if fnmatch(path, pattern):
                return dict(response)
        return dict(fallback)

    return _handler


DENY = lambda reason: {"safety_level": "deny", "reason": reason}  # noqa: E731
NEEDS_APPROVAL = {"safety_level": "approval_required", "approval_id": "ap-1"}


PNG_HEADER = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 64, 32)
    + b"\x08\x02\x00\x00\x00"
    + b"\x00\x00\x00\x00"
)


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve()

    def write(self, rel: str, data, *, encoding: str = "utf-8") -> Path:
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data, encoding=encoding)
        return p

    def outside_file(self, name: str, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name).resolve() / name
        target.write_text(text, encoding="utf-8")
        return target


# ---------------------------------------------------------------------------
#  read_file — text ranges and caps
# ---------------------------------------------------------------------------

class ReadFileTextTest(_TmpCase):
    def _read(self, args, ctx=None):
        return asyncio.run(read_file(args, ctx or FakeCtx(self.ws)))

    def test_default_read_numbers_lines_and_has_no_truncation(self) -> None:
        self.write("a.txt", "alpha\nbeta\ngamma\n")
        r, mm = _read_single_file_sync(self.ws / "a.txt", "a.txt", None, None, self.ws)
        self.assertIsNone(mm)
        self.assertTrue(r["success"])
        self.assertEqual(r["type"], "text")
        self.assertEqual(r["lineCount"], 3)
        self.assertNotIn("truncated", r)
        self.assertEqual(r["content"].splitlines()[0], "   1 | alpha")
        self.assertEqual(r["content"].splitlines()[2], "   3 | gamma")

    def test_explicit_range_reports_total_and_bounds(self) -> None:
        self.write("a.txt", "\n".join(f"L{i}" for i in range(1, 21)))
        r, _ = _read_single_file_sync(self.ws / "a.txt", "a.txt", 5, 8, self.ws)
        self.assertTrue(r["success"])
        self.assertEqual((r["startLine"], r["endLine"]), (5, 8))
        self.assertEqual(r["lineCount"], 4)
        self.assertEqual(r["totalLines"], 20)
        self.assertIn("L5", r["content"])
        self.assertIn("L8", r["content"])
        self.assertNotIn("L9", r["content"])

    def test_start_line_beyond_eof_is_an_error(self) -> None:
        self.write("a.txt", "one\ntwo\n")
        r, _ = _read_single_file_sync(self.ws / "a.txt", "a.txt", 9, None, self.ws)
        self.assertFalse(r["success"])
        self.assertIn("exceeds total lines", r["error"])
        self.assertEqual(r["totalLines"], 2)

    def test_string_line_numbers_are_coerced_not_ignored(self) -> None:
        self.write("a.txt", "\n".join(f"L{i}" for i in range(1, 21)))
        r, _ = _read_single_file_sync(self.ws / "a.txt", "a.txt", "10", "12", self.ws)
        self.assertTrue(r["success"])
        self.assertEqual((r["startLine"], r["endLine"]), (10, 12))

    def test_default_line_cap_is_500(self) -> None:
        self.write("big.txt", "\n".join(f"L{i:03d}" for i in range(600)))
        res = self._read({"path": "big.txt"})
        entry = res["data"]["results"][0]
        self.assertEqual(entry["lineCount"], 500)
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["totalLines"], 600)
        self.assertEqual(entry["shownLines"], 500)
        self.assertIn("startLine/endLine", entry["hint"])
        self.assertIn("   1 | L000", entry["content"])
        self.assertIn(" 500 | L499", entry["content"])
        self.assertNotIn("L500", entry["content"])

    def test_output_char_cap_stops_at_64k(self) -> None:
        line = "x" * 1000
        self.write("wide.txt", "\n".join(line for _ in range(200)))
        r, _ = _read_single_file_sync(self.ws / "wide.txt", "wide.txt", None, None, self.ws)
        self.assertTrue(r["success"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["lineCount"], 65)          # 65 * 1001 chars < 64 K
        self.assertEqual(r["totalLines"], 200)
        self.assertIn("character limit", r["hint"])
        self.assertLessEqual(len(r["content"]), 66_000)

    def test_legacy_single_path_passes_start_and_end_line(self) -> None:
        self.write("a.txt", "one\ntwo\nthree\nfour\n")
        res = self._read({"path": "a.txt", "start_line": 2, "end_line": 3})
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], "a.txt")
        self.assertEqual(res["content"].splitlines(), ["   2 | two", "   3 | three"])
        entry = res["data"]["results"][0]
        self.assertEqual((entry["startLine"], entry["endLine"]), (2, 3))
        self.assertEqual(entry["totalLines"], 4)


# ---------------------------------------------------------------------------
#  read_file — oversized files, bounded memory
# ---------------------------------------------------------------------------

class ReadFileOversizedTest(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        lines = [f"line_{i:04d}_" + ("x" * 30) for i in range(1, 201)]
        self.big = self.write("big.txt", "\n".join(lines))
        self.assertGreater(self.big.stat().st_size, 2048)

    def test_head_preview_without_range(self) -> None:
        with patch.object(rf_mod, "_MAX_TEXT_FILE_BYTES", 2048):
            r, mm = _read_single_file_sync(self.big, "big.txt", None, None, self.ws)
        self.assertIsNone(mm)
        self.assertTrue(r["success"])
        self.assertTrue(r["oversized"])
        self.assertTrue(r["truncated"])
        self.assertEqual((r["startLine"], r["endLine"]), (1, 50))
        self.assertIn("line_0001_", r["content"])
        self.assertIn("line_0050_", r["content"])
        self.assertNotIn("line_0051_", r["content"])
        self.assertIn("startLine=51", r["hint"])
        self.assertIn("execute_command", r["hint"])

    def test_explicit_range_streams_and_stops_early(self) -> None:
        with patch.object(rf_mod, "_MAX_TEXT_FILE_BYTES", 2048):
            r, _ = _read_single_file_sync(self.big, "big.txt", 120, 130, self.ws)
        self.assertTrue(r["success"])
        self.assertTrue(r["oversized"])
        self.assertEqual((r["startLine"], r["endLine"]), (120, 130))
        self.assertEqual(r["lineCount"], 11)
        self.assertIn("line_0120_", r["content"])
        self.assertIn("line_0130_", r["content"])
        self.assertNotIn("line_0131_", r["content"])
        self.assertIn("startLine=131", r["hint"])

    def test_oversized_scan_ceiling(self) -> None:
        with patch.object(rf_mod, "_MAX_TEXT_FILE_BYTES", 2048), \
                patch.object(rf_mod, "_MAX_OVERSIZED_SCAN_CHARS", 1000):
            r, _ = _read_single_file_sync(self.big, "big.txt", 150, 160, self.ws)
        self.assertFalse(r["success"])
        self.assertIn("streaming scan limit", r["error"])

    def test_single_huge_line_stays_memory_bounded(self) -> None:
        huge = self.write("huge.txt", b"x" * (8 * 1024 * 1024))
        tracemalloc.start()
        try:
            r, _ = _read_single_file_sync(huge, "huge.txt", None, None, self.ws)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(r["success"])
        self.assertEqual(r["lineCount"], 1)
        self.assertIn("[line truncated]", r["content"])
        self.assertLessEqual(len(r["content"]), rf_mod._MAX_LINE_KEEP_CHARS + 64)
        # Peak allocation is one refill chunk, never the 8 MB line.
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_huge_line_does_not_break_following_line_numbers(self) -> None:
        huge = self.write("huge2.txt", b"x" * (4 * 1024 * 1024) + b"\nafter\n")
        r, _ = _read_single_file_sync(huge, "huge2.txt", 2, 2, self.ws)
        self.assertTrue(r["success"])
        self.assertEqual(r["lineCount"], 1)
        self.assertEqual(r["content"], "   2 | after")

    def test_binary_prefix_sniff_is_accurate_at_the_tail(self) -> None:
        self.assertFalse(_is_binary_prefix(b""))
        self.assertFalse(_is_binary_prefix(b"hello world\n"))
        # truncated multi-byte sequence at the end is still text
        self.assertFalse(_is_binary_prefix(b"hello \xc3"))
        self.assertFalse(_is_binary_prefix(b"hello \xe4\xb8"))
        # 0xFF can never start a UTF-8 sequence, even as the last byte
        self.assertTrue(_is_binary_prefix(b"hello \xff"))
        self.assertTrue(_is_binary_prefix(b"caf\xe9 bar"))
        self.assertTrue(_is_binary_prefix(b"a\x00b"))


# ---------------------------------------------------------------------------
#  read_file — binary / image shapes, batch, approvals
# ---------------------------------------------------------------------------

class ReadFileShapesAndApprovalTest(_TmpCase):
    def _read(self, args, ctx=None):
        return asyncio.run(read_file(args, ctx or FakeCtx(self.ws)))

    def test_binary_file_reports_shape_without_content(self) -> None:
        payload = b"\x00\x01\x02BINARY-SECRET" + b"\x00" * 64
        self.write("blob.bin", payload)
        res = self._read({"path": "blob.bin"})
        entry = res["data"]["results"][0]
        self.assertTrue(entry["success"])
        self.assertEqual(entry["type"], "binary")
        self.assertEqual(entry["size"], len(payload))
        self.assertNotIn("content", entry)
        self.assertIn("[binary:", res["data"]["result"])
        self.assertNotIn("BINARY-SECRET", json.dumps(res))

    def test_png_reports_multimodal_entry_and_attachment(self) -> None:
        self.write("pic.png", PNG_HEADER)
        res = self._read({"path": "pic.png"})
        entry = res["data"]["results"][0]
        self.assertTrue(entry["success"])
        self.assertEqual(entry["type"], "multimodal")
        self.assertEqual(entry["mimeType"], "image/png")
        self.assertEqual(entry["dimensions"], {"width": 64, "height": 32, "aspectRatio": "2:1"})
        att = res["attachments"][0]
        self.assertEqual(att["type"], "image")
        self.assertEqual(att["mime_type"], "image/png")
        self.assertTrue(att["path"].startswith("data/attachments/read_file/"))
        self.assertTrue((self.ws / att["path"]).is_file())
        self.assertIs(res["data"]["attachments"], res["attachments"])

    def test_batch_partial_failure(self) -> None:
        self.write("ok1.txt", "first\n")
        self.write("ok2.txt", "second\n")
        res = self._read({"files": [
            {"path": "ok1.txt"}, {"path": "missing.txt"}, {"path": "ok2.txt"},
        ]})
        self.assertFalse(res["ok"])
        self.assertFalse(res["success"])
        self.assertEqual(res["data"]["successCount"], 2)
        self.assertEqual(res["data"]["failCount"], 1)
        self.assertEqual(res["data"]["totalCount"], 3)
        self.assertEqual(len(res["data"]["results"]), 3)
        self.assertEqual(res["error"], "1 file(s) failed to read")
        self.assertIn("── missing.txt ── ERROR: File not found", res["data"]["result"])

    def test_batch_all_success_is_ok(self) -> None:
        self.write("ok1.txt", "first\n")
        self.write("ok2.txt", "second\n")
        res = self._read({"files": [{"path": "ok1.txt"}, {"path": "ok2.txt"}]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["successCount"], 2)
        self.assertEqual(res["data"]["failCount"], 0)

    def test_batch_with_two_images_aggregates_attachments(self) -> None:
        self.write("a.png", PNG_HEADER)
        self.write("b.png", PNG_HEADER)
        res = self._read({"files": [{"path": "a.png"}, {"path": "b.png"}]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["successCount"], 2)
        self.assertEqual(len(res["attachments"]), 2)
        self.assertEqual({a["mime_type"] for a in res["attachments"]}, {"image/png"})
        self.assertEqual(len(res["data"]["attachments"]), 2)

    def test_empty_files_list_is_an_error(self) -> None:
        res = self._read({"files": []})
        self.assertFalse(res["ok"])
        self.assertIn("no path specified", res["error"])

    def test_denied_read_never_touches_the_file(self) -> None:
        self.write("secret.txt", "TOP-SECRET-PAYLOAD\n")
        ctx = FakeCtx(self.ws, op_response=DENY("blocked by policy"))
        res = self._read({"path": "secret.txt"}, ctx)
        self.assertFalse(res["ok"])
        self.assertEqual(ctx.requested_ops(), ["read_file"])
        self.assertEqual(ctx.requested_paths(), ["secret.txt"])
        self.assertNotIn("TOP-SECRET-PAYLOAD", json.dumps(res))
        self.assertIn("blocked by policy", res["error"])

    def test_denied_approval_does_not_read(self) -> None:
        self.write("secret.txt", "TOP-SECRET-PAYLOAD\n")
        ctx = FakeCtx(self.ws, op_response=NEEDS_APPROVAL, approval={"status": "denied"})
        res = self._read({"path": "secret.txt"}, ctx)
        self.assertFalse(res["ok"])
        self.assertIn("user denied approval", res["error"])
        self.assertNotIn("TOP-SECRET-PAYLOAD", json.dumps(res))
        ctx.wait_for_approval.assert_awaited_once_with("ap-1")

    def test_workspace_read_still_consults_the_guard(self) -> None:
        self.write("a.txt", "alpha\n")
        ctx = FakeCtx(self.ws)
        self._read({"path": "a.txt"}, ctx)
        self.assertEqual(ctx.requested_ops(), ["read_file"])


# ---------------------------------------------------------------------------
#  search_in_files — modes and globs
# ---------------------------------------------------------------------------

class _SearchCase(_TmpCase):
    def search(self, args, ctx=None):
        return asyncio.run(search_in_files(args, ctx or FakeCtx(self.ws)))

    def spy_load_text(self):
        """Record which paths the scanner actually reads."""
        seen: list[str] = []
        real = sf_mod._load_text

        def _spy(p, **kwargs):
            seen.append(str(p))
            return real(p, **kwargs)

        patcher = patch.object(sf_mod, "_load_text", side_effect=_spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen


class SearchModesTest(_SearchCase):
    def test_content_mode_transcript(self) -> None:
        self.write("a.py", "alpha\nbeta\ngamma\n")
        self.write("b.py", "nothing here\n")
        res = self.search({"query": "BETA"})          # case-insensitive search
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["count"], 1)
        text = res["data"]["result"]
        self.assertIn("1 results found:", text)
        self.assertIn("a.py:2 | beta", text)
        self.assertIn("1: alpha", text)
        self.assertIn("3: gamma", text)
        m = res["data"]["results"][0]
        self.assertEqual((m["line"], m["column"], m["match"]), (2, 1, "beta"))

    def test_content_mode_reports_each_column(self) -> None:
        self.write("a.txt", "needle needle\n")
        res = self.search({"query": "needle"})
        self.assertEqual(res["data"]["count"], 2)
        self.assertEqual([m["column"] for m in res["data"]["results"]], [1, 8])

    def test_files_only_mode(self) -> None:
        self.write("a.txt", "needle needle\n")
        self.write("b.txt", "needle\n")
        self.write("c.txt", "haystack\n")
        res = self.search({"query": "needle", "output_mode": "files_only"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["files"], ["a.txt", "b.txt"])
        self.assertEqual(res["data"]["count"], 2)
        self.assertIn("2 files found:", res["data"]["result"])

    def test_count_mode(self) -> None:
        self.write("a.txt", "needle needle\n")
        self.write("b.txt", "needle\n")
        res = self.search({"query": "needle", "output_mode": "count"})
        self.assertEqual(
            res["data"]["counts"], [{"file": "a.txt", "count": 2}, {"file": "b.txt", "count": 1}],
        )
        self.assertEqual(res["data"]["count"], 3)
        self.assertEqual(res["data"]["filesCount"], 2)
        self.assertIn("3 matches across 2 files", res["data"]["result"])

    def test_empty_query_and_bad_regex(self) -> None:
        self.assertFalse(self.search({"query": ""})["ok"])
        res = self.search({"query": "([", "isRegex": True})
        self.assertFalse(res["ok"])
        self.assertIn("invalid regex", res["error"])

    def test_garbage_limits_do_not_raise(self) -> None:
        self.write("a.txt", "needle\n")
        res = self.search({"query": "needle", "maxResults": "not-a-number"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["count"], 1)


class GlobTest(_SearchCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("top.py", "needle\n")
        self.write("readme.md", "needle\n")
        self.write("src/a.py", "needle\n")
        self.write("src/deep/b.py", "needle\n")
        self.write("pkg/deep/c.py", "needle\n")

    def test_match_pattern_semantics(self) -> None:
        # no "/" -> matched against the file name at any depth
        self.assertTrue(_match_pattern("src/deep/b.py", "*.py"))
        self.assertTrue(_match_pattern("top.py", "*.py"))
        self.assertFalse(_match_pattern("readme.md", "*.py"))
        # "**/" spans zero or more directories
        self.assertTrue(_match_pattern("top.py", "**/*.py"))
        self.assertTrue(_match_pattern("src/deep/b.py", "**/*.py"))
        # single "*" does not cross a separator
        self.assertTrue(_match_pattern("src/a.py", "src/*.py"))
        self.assertFalse(_match_pattern("src/deep/b.py", "src/*.py"))
        self.assertTrue(_match_pattern("src/deep/b.py", "src/**"))
        self.assertFalse(_match_pattern("top.py", "*/*.py"))
        self.assertTrue(_match_pattern("src/a.py", "*/*.py"))
        self.assertTrue(_match_pattern("src/deep/b.py", "**/deep/*.py"))
        self.assertTrue(_match_pattern("a/b.py", "./**/*.py"))
        self.assertTrue(_match_pattern("anything.txt", "**/*"))
        self.assertTrue(_match_pattern("anything.txt", ""))

    def test_pattern_filter_end_to_end(self) -> None:
        res = self.search({"query": "needle", "pattern": "src/*.py", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["src/a.py"])

        res = self.search({"query": "needle", "pattern": "**/*.py", "output_mode": "files_only"})
        # walk order: root files first, then directories alphabetically
        self.assertEqual(
            res["data"]["files"],
            ["top.py", "pkg/deep/c.py", "src/a.py", "src/deep/b.py"],
        )

        res = self.search({"query": "needle", "pattern": "**/deep/*.py", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["pkg/deep/c.py", "src/deep/b.py"])

        res = self.search({"query": "needle", "pattern": "*.md", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["readme.md"])


# ---------------------------------------------------------------------------
#  search_in_files — per-file read approval
# ---------------------------------------------------------------------------

class ReadApprovalTest(_SearchCase):
    def test_dotenv_is_denied_even_though_root_is_auto(self) -> None:
        env = self.write(".env", "NEEDLE=dotenv-secret\n")
        self.write("a.txt", "NEEDLE in a plain file\n")
        ctx = FakeCtx(
            self.ws,
            op_handler=policy_handler(read_rules=[("*/.env", DENY("dotenv is human-only"))]),
        )
        seen = self.spy_load_text()
        res = self.search({"query": "NEEDLE", "output_mode": "files_only"}, ctx=ctx)

        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["files"], ["a.txt"])
        # the denied file is reported, not read, and never leaks its content
        self.assertEqual(res["data"]["blocked"], [{"file": ".env", "error": "dotenv is human-only"}])
        self.assertNotIn(str(env.resolve()), seen)
        self.assertNotIn("dotenv-secret", json.dumps(res))
        self.assertIn("skipped by policy", res["data"]["result"])
        # one read decision per candidate, keyed by the resolved absolute path
        self.assertIn(("read_file", str(env.resolve())), ctx.requests())

    def test_external_symlink_needs_approval_and_denied_is_not_read(self) -> None:
        target = self.outside_file("ext.txt", "NEEDLE-EXTERNAL\n")
        (self.ws / "link.txt").symlink_to(target)
        ctx = FakeCtx(
            self.ws,
            op_handler=policy_handler(read_rules=[(str(target), NEEDS_APPROVAL)]),
            approval={"status": "denied"},
        )
        seen = self.spy_load_text()
        res = self.search({"query": "NEEDLE", "output_mode": "files_only"}, ctx=ctx)

        self.assertFalse(res["data"]["files"])
        self.assertEqual(res["data"]["blocked"], [{"file": str(target), "error": "user denied approval"}])
        self.assertNotIn(str(target), seen)
        self.assertNotIn("NEEDLE-EXTERNAL", json.dumps(res))
        self.assertIn(("read_file", str(target)), ctx.requests())

    def test_external_symlink_allowed_is_read(self) -> None:
        target = self.outside_file("ext.txt", "NEEDLE-EXTERNAL\n")
        (self.ws / "link.txt").symlink_to(target)
        ctx = FakeCtx(self.ws, op_handler=policy_handler(read_rules=[(str(target), NEEDS_APPROVAL)]))
        res = self.search({"query": "NEEDLE", "output_mode": "files_only"}, ctx=ctx)
        self.assertTrue(res["ok"])
        # the resolved target is what was approved, read and reported
        self.assertEqual(res["data"]["files"], [str(target)])
        self.assertIn(("read_file", str(target)), ctx.requests())

    def test_guard_work_stops_once_results_are_full(self) -> None:
        for i in range(40):
            self.write(f"f{i:02d}.txt", "needle\n")
        ctx = FakeCtx(self.ws)
        res = self.search({"query": "needle", "maxResults": 1}, ctx=ctx)
        self.assertTrue(res["data"]["truncated"])
        self.assertEqual(res["data"]["count"], 1)
        # One batch of decisions, not one per file: the scan stopped at the cap.
        self.assertEqual(ctx.request_op.await_count, sf_mod._GUARD_BATCH)
        self.assertLess(ctx.request_op.await_count, 40)

    def test_denied_read_does_not_break_other_files(self) -> None:
        self.write("a.txt", "needle\n")
        b = self.write("b.txt", "needle\n")
        ctx = FakeCtx(
            self.ws,
            op_handler=policy_handler(read_rules=[(str(b.resolve()), DENY("nope"))]),
        )
        res = self.search({"query": "needle", "output_mode": "count"}, ctx=ctx)
        self.assertEqual(res["data"]["counts"], [{"file": "a.txt", "count": 1}])
        self.assertEqual(res["data"]["blocked"], [{"file": "b.txt", "error": "nope"}])


# ---------------------------------------------------------------------------
#  search_in_files — replace semantics and approvals
# ---------------------------------------------------------------------------

class ReplaceTest(_SearchCase):
    def test_literal_replace_keeps_regex_metachars_literal(self) -> None:
        target = self.write("a.txt", "a.b.c\n")
        untouched = self.write("b.txt", "axb\n")
        res = self.search({"query": "a.b", "mode": "replace", "replace": "X"})
        self.assertTrue(res["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "X.c\n")
        self.assertEqual(untouched.read_text(encoding="utf-8"), "axb\n")
        self.assertEqual(res["data"]["filesModified"], 1)
        self.assertEqual(res["data"]["totalReplacements"], 1)
        self.assertIn("1 files modified, 1 replacements", res["data"]["result"])

    def test_literal_replace_keeps_backslashes_and_dollar(self) -> None:
        target = self.write("a.txt", "hello world\n")
        res = self.search({"query": "world", "mode": "replace", "replace": r"\\1 $1"})
        self.assertTrue(res["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "hello " + r"\\1 $1" + "\n")

    def test_regex_replace_supports_dollar_capture_groups(self) -> None:
        target = self.write("a.txt", "foo_123 and bar_456\n")
        res = self.search({
            "query": r"([a-z]+)_(\d+)", "isRegex": True,
            "mode": "replace", "replace": "$2:$1",
        })
        self.assertTrue(res["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "123:foo and 456:bar\n")
        self.assertEqual(res["data"]["totalReplacements"], 2)

    def test_regex_replace_keeps_escaped_backslash_semantics(self) -> None:
        target = self.write("a.txt", "a b\n")
        self.search({"query": "b", "isRegex": True, "mode": "replace", "replace": r"\\"})
        self.assertEqual(target.read_text(encoding="utf-8"), "a " + "\\" + "\n")

    def test_denied_read_in_replace_mode_never_reads_nor_writes(self) -> None:
        target = self.write("a.txt", "alpha beta\n")
        ctx = FakeCtx(self.ws, op_handler=policy_handler(read_rules=[("*a.txt", DENY("no reads"))]))
        seen = self.spy_load_text()
        res = self.search({"query": "beta", "mode": "replace", "replace": "gamma"}, ctx=ctx)
        self.assertFalse(res["ok"])
        self.assertIn("blocked by policy", res["error"])
        self.assertEqual(res["data"]["blocked"], [{"file": "a.txt", "error": "no reads"}])
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha beta\n")
        self.assertNotIn(str(target.resolve()), seen)
        self.assertEqual(ctx.requested_ops(), ["read_file"])

    def test_denied_write_leaves_file_untouched(self) -> None:
        target = self.write("a.txt", "alpha beta\n")
        ctx = FakeCtx(self.ws, op_handler=policy_handler(write_rules=[("*a.txt", DENY("no writes"))]))
        res = self.search({"query": "beta", "mode": "replace", "replace": "gamma"}, ctx=ctx)
        self.assertFalse(res["ok"])
        self.assertIn("blocked by policy", res["error"])
        self.assertEqual(res["data"]["blocked"], [{"file": "a.txt", "error": "no writes"}])
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha beta\n")
        self.assertEqual(ctx.requested_ops(), ["read_file", "write_file"])

    def test_replace_guards_every_candidate_file(self) -> None:
        env = self.write(".env", "beta=1\n")
        allowed = self.write("a.txt", "beta\n")
        engine = self.write("engine/x.py", "beta\n")
        policy = self.write("data/policy.yaml", "beta\n")
        ctx = FakeCtx(
            self.ws,
            op_handler=policy_handler(
                read_rules=[("*/.env", DENY("dotenv is human-only"))],
                write_rules=[("*/engine/x.py", NEEDS_APPROVAL)],
            ),
            approval={"status": "denied"},
        )
        res = self.search({"query": "beta", "mode": "replace", "replace": "gamma"}, ctx=ctx)

        self.assertEqual(allowed.read_text(encoding="utf-8"), "gamma\n")
        self.assertEqual(env.read_text(encoding="utf-8"), "beta=1\n")
        self.assertEqual(engine.read_text(encoding="utf-8"), "beta\n")
        self.assertEqual(policy.read_text(encoding="utf-8"), "beta\n")
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["filesModified"], 1)
        self.assertEqual(
            res["data"]["blocked"],
            [
                {"file": ".env", "error": "dotenv is human-only"},
                {"file": "engine/x.py", "error": "user denied approval"},
            ],
        )
        self.assertIn("blocked: .env", res["data"]["result"])
        self.assertEqual(
            ctx.requests(),
            [
                ("read_file", str(env.resolve())),
                ("read_file", str(allowed.resolve())),
                ("read_file", str(engine.resolve())),
                ("write_file", str(allowed.resolve())),
                ("write_file", str(engine.resolve())),
            ],
        )

        # The same mechanism protects a candidate under an explicitly named path.
        ctx2 = FakeCtx(
            self.ws,
            op_handler=policy_handler(read_rules=[("*data/policy.yaml", DENY("policy is human-only"))]),
        )
        res2 = self.search(
            {"query": "beta", "path": "data", "mode": "replace", "replace": "gamma"}, ctx=ctx2,
        )
        self.assertFalse(res2["ok"])
        self.assertIn("blocked by policy", res2["error"])
        self.assertEqual(policy.read_text(encoding="utf-8"), "beta\n")
        self.assertEqual(ctx2.requested_paths(), [str(policy.resolve())])

    def test_replace_preserves_crlf_and_skips_undecodable(self) -> None:
        crlf = self.write("crlf.txt", b"line1\r\nbeta\r\n")
        broken = self.write("broken.txt", b"beta \xff\xfe tail")
        res = self.search({"query": "beta", "mode": "replace", "replace": "gamma"})
        self.assertTrue(res["ok"])
        self.assertEqual(crlf.read_bytes(), b"line1\r\ngamma\r\n")
        self.assertEqual(broken.read_bytes(), b"beta \xff\xfe tail")
        self.assertEqual(res["data"]["skippedUndecodable"], 1)
        self.assertEqual(res["data"]["filesModified"], 1)

    def test_replace_noop_does_not_rewrite(self) -> None:
        target = self.write("a.txt", "beta\n")
        before = target.stat().st_mtime_ns
        res = self.search({"query": "beta", "mode": "replace", "replace": "beta"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["filesModified"], 0)
        self.assertEqual(res["data"]["totalReplacements"], 0)
        self.assertEqual(target.stat().st_mtime_ns, before)


# ---------------------------------------------------------------------------
#  search_in_files — skip policy vs explicitly named paths
# ---------------------------------------------------------------------------

class SkipPolicyTest(_SearchCase):
    def test_root_scan_skips_data_dir_but_explicit_data_path_works(self) -> None:
        self.write("app.py", "NEEDLE\n")
        self.write("data/notes.txt", "NEEDLE\n")
        self.write("data/attachments/deep.txt", "NEEDLE\n")

        res = self.search({"query": "NEEDLE", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["app.py"])

        res = self.search({"query": "NEEDLE", "path": "data", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["data/notes.txt"])

        res = self.search({"query": "NEEDLE", "path": "data/notes.txt", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["data/notes.txt"])

        # read_file has no such skip: the same file is readable through it.
        read_res = asyncio.run(read_file({"path": "data/notes.txt"}, FakeCtx(self.ws)))
        self.assertIn("NEEDLE", read_res["content"])

    def test_skip_dir_and_suffix_do_not_hide_explicit_paths(self) -> None:
        self.write("build/gen.py", "NEEDLE\n")
        self.write("app.log", "NEEDLE\n")
        self.write("app.py", "NEEDLE\n")

        res = self.search({"query": "NEEDLE", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["app.py"])

        res = self.search({"query": "NEEDLE", "path": "build/gen.py", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["build/gen.py"])

        res = self.search({"query": "NEEDLE", "path": "app.log", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["app.log"])

    def test_binary_file_skipped_in_walk_but_searchable_when_named(self) -> None:
        self.write("blob.dat", b"NEEDLE\x00\x00tail")
        res = self.search({"query": "NEEDLE", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], [])

        res = self.search({"query": "NEEDLE", "path": "blob.dat", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["blob.dat"])

    def test_oversized_file_is_skipped_by_directory_scan(self) -> None:
        self.write("config/runtime.yaml", "meta:\n  search:\n    max_file_size_bytes: 100000\n")
        self.write("ok.txt", "needle\n")
        self.write("big.txt", "needle " * 25_000)      # 175 KB > 100 KB limit
        res = self.search({"query": "needle", "output_mode": "files_only"})
        self.assertEqual(res["data"]["files"], ["ok.txt"])

        # Same file is found when the configured limit allows it.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws2 = Path(tmp.name).resolve()
        (ws2 / "big.txt").write_text("needle " * 25_000, encoding="utf-8")
        res2 = asyncio.run(search_in_files(
            {"query": "needle", "output_mode": "files_only"}, FakeCtx(ws2),
        ))
        self.assertEqual(res2["data"]["files"], ["big.txt"])


# ---------------------------------------------------------------------------
#  search_in_files — external search root
# ---------------------------------------------------------------------------

class ExternalApprovalTest(_SearchCase):
    def test_external_root_denied_is_not_searched(self) -> None:
        target = self.outside_file("ext.txt", "NEEDLE-EXTERNAL\n")
        ctx = FakeCtx(
            self.ws,
            op_handler=policy_handler(read_rules=[("*", NEEDS_APPROVAL)]),
            approval={"status": "denied"},
        )
        res = self.search({"query": "NEEDLE", "path": str(target)}, ctx=ctx)
        self.assertFalse(res["ok"])
        self.assertIn("user denied approval", res["error"])
        self.assertEqual(ctx.requested_paths(), [str(target)])
        self.assertNotIn("NEEDLE-EXTERNAL", json.dumps(res))

    def test_external_root_allowed_is_searched_with_per_file_guard(self) -> None:
        target = self.outside_file("ext.txt", "NEEDLE-EXTERNAL\n")
        ctx = FakeCtx(self.ws, op_handler=policy_handler(read_rules=[("*", NEEDS_APPROVAL)]))
        res = self.search(
            {"query": "NEEDLE", "path": str(target), "output_mode": "files_only"}, ctx=ctx,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["files"], [str(target)])
        # search root decision first, then the per-file read decision
        self.assertEqual(ctx.requested_paths(), [str(target), str(target)])


# ---------------------------------------------------------------------------
#  Real policy engine — the per-file path is what the rules are matched against
# ---------------------------------------------------------------------------

class PolicyBoundaryTest(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        from supervisor.policy import PolicyEngine

        self.engine = PolicyEngine(workspace_root=self.ws)

    def test_fresh_policy_initializes_without_private_setup_calls(self) -> None:
        self.assertTrue((self.ws / "data" / "policy.yaml").is_file())
        self.assertEqual(self.engine.evaluate_read_file(path=".env").safety_level.value, "deny")
        self.assertEqual(self.engine.evaluate_tool(name="manage_schedule").safety_level.value, "auto")
        self.assertEqual(self.engine.extra_roots, [])

    def test_defaults_still_protect_paths_when_policy_file_cannot_be_created(self) -> None:
        from supervisor.policy import PolicyEngine
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(PolicyEngine, "_ensure_policy_file_exists"):
                engine = PolicyEngine(workspace_root=Path(directory))
            self.assertEqual(engine.evaluate_read_file(path=".env").safety_level.value, "deny")
            self.assertEqual(engine.evaluate_write_file(path="data/policy.yaml").safety_level.value, "deny")
            self.assertEqual(engine.evaluate_tool(name="unlisted_tool").safety_level.value, "approval_required")

    def test_write_rules_apply_to_the_file_path_not_to_the_search_root(self) -> None:
        self.assertEqual(self.engine.evaluate_write_file(path="data/policy.yaml").safety_level.value, "deny")
        self.assertEqual(self.engine.evaluate_write_file(path=".env").safety_level.value, "deny")
        self.assertEqual(self.engine.evaluate_write_file(path="engine/x.py").safety_level.value, "approval_required")
        self.assertEqual(self.engine.evaluate_write_file(path="a.txt").safety_level.value, "auto")
        # the synthetic root path the migrated tool used to send bypassed every rule
        self.assertEqual(self.engine.evaluate_write_file(path="(search_replace)").safety_level.value, "auto")

    def test_read_rules_apply_to_the_file_path(self) -> None:
        self.assertEqual(self.engine.evaluate_read_file(path=".env").safety_level.value, "deny")
        self.assertEqual(self.engine.evaluate_read_file(path="a.txt").safety_level.value, "auto")

    def test_symlink_target_is_classified_external(self) -> None:
        target = self.outside_file("ext.txt", "x")
        (self.ws / "link.txt").symlink_to(target)
        # The tool hands the *resolved* absolute path to the guard (see
        # _collect_files), which policy classifies as external -> approval.
        read_decision = self.engine.evaluate_read_file(path=str(target))
        self.assertEqual(read_decision.trust_level, "external")
        self.assertEqual(read_decision.safety_level.value, "approval_required")
        write_decision = self.engine.evaluate_write_file(path=str(target))
        self.assertEqual(write_decision.trust_level, "external")
        self.assertEqual(write_decision.safety_level.value, "approval_required")
        # A relative path that resolves out of the tree is refused outright, so a
        # link can never be approved under its in-tree name.
        self.assertEqual(self.engine.evaluate_read_file(path="link.txt").safety_level.value, "deny")

if __name__ == "__main__":
    unittest.main()
