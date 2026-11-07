"""Toolbox mutation migration checks: apply_diff / write_file / execute_command / system.

Run: python3.11 -B tests/test_toolbox_mutation_migration.py

Every case runs against a throwaway temp dir (or a throwaway temp git repo) with a
fake ToolContext. No supervisor is contacted, no real restart is requested, no
production file is read or written.
"""
from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 包 __init__ 把同名工具函数导到了 toolbox.builtins 上，直接 from 会拿到函数而不是模块
apply_diff_mod = importlib.import_module("toolbox.builtins.apply_diff")
execute_command_mod = importlib.import_module("toolbox.builtins.execute_command")
system_mod = importlib.import_module("toolbox.builtins.system")
write_file_mod = importlib.import_module("toolbox.builtins.write_file")
sf_mod = importlib.import_module("toolbox.builtins.search_in_files")
from toolbox.builtins.search_in_files import search_in_files  # noqa: E402


# ── fake supervisor context ──────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return {}


class _FakeHttp:
    """Records supervisor calls instead of making them."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.gets: list[str] = []

    async def post(self, url: str, json: Any = None, **_kwargs: Any) -> _FakeResponse:
        self.posts.append((url, json))
        return _FakeResponse(self.status_code)

    async def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        self.gets.append(url)
        return _FakeResponse(self.status_code)


class FakeCtx:
    """Minimal ToolContext stand-in: policy answers come from arguments, HTTP is fake."""

    def __init__(
        self,
        root: str | Path,
        *,
        safety: str = "allow",
        approval: str = "allowed",
        cancelled: bool = False,
        http_status: int = 200,
    ):
        self.workspace_root = Path(root)
        self.workspace = None
        self.run_id = "test-run"
        self.session_id = "test-session"
        self.parent_session_id = ""
        self.task_id = ""
        self.worker_id = "test-worker"
        self.supervisor_url = "http://supervisor.invalid"
        self.http = _FakeHttp(http_status)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.safety = safety
        self.approval = approval
        self.cancelled = cancelled

    @property
    def effective_workspace(self) -> Path:
        return self.workspace or self.workspace_root

    def route_session_id(self) -> str:
        return self.parent_session_id or self.session_id

    async def emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        self.events.append((type_, payload))

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((op, parameters))
        if self.safety == "approval_required":
            return {"safety_level": "approval_required", "approval_id": "ap-1"}
        return {"safety_level": self.safety, "op": op}

    async def check_cancelled(self) -> bool:
        return self.cancelled

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        return {"approval_id": approval_id, "status": self.approval}


def _line_no(text: str, needle: str) -> int:
    for idx, line in enumerate(text.split("\n"), 1):
        if needle in line:
            return idx
    return -1


def _lines(count: int, prefix: str = "l") -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, count + 1)]


# ── apply_diff: start_line is always the original file's line number ─────────


class ApplyDiffOriginalLineTests(unittest.TestCase):
    NAME = "target.txt"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str) -> None:
        (self.root / self.NAME).write_text(text, encoding="utf-8")

    def _read(self) -> str:
        return (self.root / self.NAME).read_text(encoding="utf-8")

    def _run(self, diffs: list[dict[str, Any]], **ctx_kw: Any) -> dict[str, Any]:
        ctx = FakeCtx(self.root, **ctx_kw)
        return asyncio.run(apply_diff_mod.apply_diff({"path": self.NAME, "diffs": diffs}, ctx))

    # 生产误匹配反例 -----------------------------------------------------------

    def test_start_line_maps_past_lines_inserted_above(self) -> None:
        """原始第 20 行被 10 行插入推到第 30 行；当前第 20 行恰好也有同样文本。

        生产版会先命中 raw_start_line=20 的当前位置，改错成第 20 行。严格「原始行号」
        语义必须改到第 30 行。
        """
        rows = _lines(40)
        rows[0] = "# head"
        rows[9] = "TARGET = 1"    # 原始第 10 行
        rows[19] = "TARGET = 1"   # 原始第 20 行
        self._write("\n".join(rows) + "\n")

        inserted = "\n".join(f"inserted{i}" for i in range(10))
        res = self._run([
            {"search": "# head\n", "replace": f"# head\n{inserted}\n"},
            {"search": "TARGET = 1", "start_line": 20, "replace": "TARGET = 1  # patched"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 2, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "# patched"), 30)
        self.assertEqual(_line_no(text, "TARGET = 1"), 20)
        self.assertEqual(text.count("TARGET = 1"), 2)

    def test_many_prior_inserts_and_deletes_compose(self) -> None:
        """3+ 次插删（+3 / -2 / +5 / -1）叠在后，仍按原始行号命中。"""
        rows = _lines(40)
        for idx in (4, 24, 34):     # 原始第 5 / 25 / 35 行
            rows[idx] = "MARK = x"
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "l02\n", "replace": "l02\nins1\nins2\nins3\n"},   # +3
            {"search": "l12\nl13\n", "replace": ""},                    # -2
            {"search": "l18\n", "replace": "l18\na\nb\nc\nd\ne\n"},      # +5
            {"search": "l21\n", "replace": ""},                         # -1
            {"search": "MARK = x", "start_line": 25, "replace": "MARK = x  # patched"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 5, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "# patched"), 30)
        self.assertEqual([ln for ln in text.split("\n") if ln == "MARK = x"], ["MARK = x", "MARK = x"])

    def test_edit_below_first_then_above(self) -> None:
        """先改下方再改上方：下方插入不得扰动上方的原始行号。"""
        rows = _lines(30)
        rows[4] = "TARGET = 1"    # 原始第 5 行
        rows[27] = "TARGET = 1"   # 原始第 28 行
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "l20\n", "replace": "l20\nb1\nb2\nb3\nb4\n"},     # 第 20 行下方 +4
            {"search": "TARGET = 1", "start_line": 5, "replace": "TARGET = 1  # top"},
            {"search": "TARGET = 1", "start_line": 28, "replace": "TARGET = 1  # bottom"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 3, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "# top"), 5)
        self.assertEqual(_line_no(text, "# bottom"), 32)

    def test_reedited_inserted_text_keeps_later_line_mapping(self) -> None:
        """对前序插入的文本再替换/删除后，后续原始行号不能多算一次位移。

        回归：把 B 换成 xxx、再把 xxx 换成 Z 时，若把两次编辑都记成「原文件 [1,2]」，
        第二条 delta 会算成 0，而真实净变化是 B→Z（插入文本被吃掉）。
        """
        rows = _lines(12)
        rows[0] = "head"
        rows[1] = "ORIG_ONE"        # 原始第 2 行：稍后被插入文本再改掉
        rows[6] = "DUP_ME"          # 原始第 7 行
        rows[10] = "DUP_ME"         # 原始第 11 行
        rows[11] = "tail"
        self._write("\n".join(rows) + "\n")

        block = "INS1\nINS2\nINS3\nINS4\nINS5\n"
        res = self._run([
            {"search": "ORIG_ONE\n", "replace": block},        # 原文本 → 5 行插入
            {"search": block, "replace": "Z\n"},               # 再把插入文本换成 1 行
            {"search": "DUP_ME", "start_line": 11, "replace": "DUP_ME  # patched"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 3, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "DUP_ME  # patched"), 11)
        self.assertEqual(_line_no(text, "DUP_ME"), 7)
        self.assertEqual(_line_no(text, "Z"), 2)

    def test_deleted_inserted_text_keeps_later_line_mapping(self) -> None:
        """插入文本被后序 diff 整段删除后，下方重复文本的行号仍然正确。"""
        rows = _lines(12)
        rows[0] = "head"
        rows[1] = "ORIG_ONE"        # 原始第 2 行
        rows[6] = "DUP_ME"          # 原始第 7 行
        rows[10] = "DUP_ME"         # 原始第 11 行
        rows[11] = "tail"
        self._write("\n".join(rows) + "\n")

        block = "INS1\nINS2\nINS3\n"
        res = self._run([
            {"search": "ORIG_ONE\n", "replace": block},
            {"search": block, "replace": ""},                    # 插入文本被删尽
            {"search": "DUP_ME", "start_line": 11, "replace": "DUP_ME  # patched"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 3, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "DUP_ME  # patched"), 10)
        self.assertEqual(_line_no(text, "DUP_ME"), 6)

    def test_deleting_inserted_text_above_then_editing_top_copy(self) -> None:
        """被再编辑的插入块位于两处重复文本之间时，上方的目标也不能被拖偏。"""
        rows = _lines(12)
        rows[0] = "head"
        rows[2] = "DUP_ME"          # 原始第 3 行
        rows[7] = "ORIG_TWO"        # 原始第 8 行
        rows[10] = "DUP_ME"         # 原始第 11 行
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "ORIG_TWO\n", "replace": "NEW1\nNEW2\nNEW3\nNEW4\n"},
            {"search": "NEW1\nNEW2\n", "replace": ""},
            {"search": "DUP_ME", "start_line": 3, "replace": "DUP_ME  # top"},
            {"search": "DUP_ME", "start_line": 11, "replace": "DUP_ME  # bottom"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 4, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "# top"), 3)
        self.assertEqual(_line_no(text, "# bottom"), 12)  # +3 行插入 -2 行删除

    def test_locating_is_not_capped_by_candidate_display_limit(self) -> None:
        """第 201+ 处匹配也是真实目标：候选回显可以截断，定位不能。"""
        rows = ["DUP_ROW"] * 250 + [f"tail{i}" for i in range(10)]
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "DUP_ROW", "start_line": 240, "replace": "DUP_ROW  # patched"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 1, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "# patched"), 240)
        self.assertEqual(text.count("# patched"), 1)

        # 无行号时仍然报多匹配，但候选回显截断到展示上限
        res = self._run([{"search": "DUP_ROW", "replace": "x"}])
        self.assertFalse(res["ok"])
        self.assertIn("Multiple matches found (250)", res["error"])
        self.assertEqual(len(res["data"]["failedDiffs"][0]["candidateLines"]), 200)

    def test_deleted_target_does_not_hit_another_copy(self) -> None:
        """目标段落已被前序 diff 删除时，不得落到另一处同文本上。"""
        rows = _lines(30)
        rows[9] = "VALUE = 1"     # 原始第 10 行
        rows[19] = "VALUE = 1"    # 原始第 20 行
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "l19\nVALUE = 1\nl21\n", "replace": "l19\nl21\n"},   # 删掉原始第 20 行
            {"search": "VALUE = 1", "start_line": 20, "replace": "VALUE = 1  # wrong"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 1)
        self.assertEqual(res["data"]["failedCount"], 1)
        text = self._read()
        self.assertNotIn("# wrong", text)
        self.assertEqual(_line_no(text, "VALUE = 1"), 10)

    def test_replaced_target_does_not_hit_another_copy(self) -> None:
        """目标段落已被前序 diff 整体替换时同理。"""
        rows = _lines(30)
        rows[9] = "VALUE = 1"     # 原始第 10 行
        rows[19] = "VALUE = 1"    # 原始第 20 行
        self._write("\n".join(rows) + "\n")

        res = self._run([
            {"search": "l19\nVALUE = 1\nl21", "replace": "l19\nREPLACED\nl21"},
            {"search": "VALUE = 1", "start_line": 20, "replace": "VALUE = 1  # wrong"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 1)
        text = self._read()
        self.assertIn("REPLACED", text)
        self.assertNotIn("# wrong", text)
        self.assertEqual(_line_no(text, "VALUE = 1"), 10)

    def test_offset_transform_handles_reedited_insert(self) -> None:
        """直接固定坐标变换：B→xxx → xxx→Z 的净位移必须只计一次。"""
        # 原文本 ABCDE；先把 B 换成 xxx（+2），再把 xxx 换成 Z（-2）
        edits = [(1, 2, 3), (1, 4, 1)]
        mapped = [apply_diff_mod._current_offset(edits, off) for off in range(6)]
        #               A  B  C  D  E  EOF
        self.assertEqual(mapped, [0, 1, 2, 3, 4, 5])
        # 旧「原始区间 + 反推」写法会把第二条 delta 算成 0，C 错位成 4
        self.assertNotEqual(mapped[2], 4)

    # 定位语义 ---------------------------------------------------------------

    def test_unique_text_is_located_even_with_stale_start_line(self) -> None:
        self._write("a\nUNIQ = 1\nb\n")
        for stale in (99, 0, -3):
            res = self._run([{"search": "UNIQ = 1", "start_line": stale, "replace": "UNIQ = 2"}])
            self.assertTrue(res["ok"], res)
            self.assertIn("UNIQ = 2", self._read())
            self._write("a\nUNIQ = 1\nb\n")

    def test_multiple_matches_need_start_line_and_report_original_lines(self) -> None:
        rows = _lines(10)
        rows[2] = "DUP"
        rows[6] = "DUP"
        self._write("\n".join(rows) + "\n")

        res = self._run([{"search": "DUP", "replace": "X"}])

        self.assertFalse(res["ok"])
        self.assertIn("Multiple matches", res["error"])
        self.assertEqual(res["data"]["failedDiffs"][0]["candidateLines"], [3, 7])
        self.assertIn("DUP", self._read())

    def test_out_of_range_start_line_still_allows_unique_text(self) -> None:
        self._write("a\nUNIQ = 1\n")
        res = self._run([{"search": "UNIQ = 1", "start_line": 500, "replace": "UNIQ = 2"}])
        self.assertTrue(res["ok"], res)
        self.assertIn("UNIQ = 2", self._read())

    def test_out_of_range_start_line_rejected_for_ambiguous_text(self) -> None:
        self._write("DUP\nx\nDUP\n")
        res = self._run([{"search": "DUP", "start_line": 500, "replace": "X"}])
        self.assertFalse(res["ok"])
        self.assertIn("out of range", res["error"])
        self.assertEqual(self._read(), "DUP\nx\nDUP\n")

    def test_unicode_and_crlf_content(self) -> None:
        # 源文件是 CRLF；apply_diff 读入时按 Python 默认换行归一化（既有行为），
        # 因此这里校验的是 unicode 内容 + 行号映射（含 emoji、多字节字符）正确。
        rows = ["标题", "α😀β", "重复 = 值", "γ", "δ", "重复 = 值", "结尾"]
        (self.root / self.NAME).write_bytes(("\r\n".join(rows) + "\r\n").encode("utf-8"))

        res = self._run([
            {"search": "标题\n", "replace": "标题\n新1\n新2\n"},
            {"search": "重复 = 值", "start_line": 6, "replace": "重复 = 值 → 改"},
        ])

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 2, res["data"].get("failedDiffs"))
        text = self._read()
        self.assertEqual(_line_no(text, "→ 改"), 8)
        self.assertEqual(_line_no(text, "重复 = 值"), 5)  # 上方插入 2 行后，未修改的那处从第 3 行到第 5 行
        self.assertIn("α😀β", text)
        self.assertIn("新2", text)

    # 失败/审批路径 -----------------------------------------------------------

    def test_no_match_does_not_write(self) -> None:
        original = "abc\ndef\n"
        self._write(original)
        res = self._run([{"search": "zzz", "replace": "y"}])
        self.assertFalse(res["ok"])
        self.assertIn("No exact match", res["error"])
        self.assertEqual(self._read(), original)

    def test_approval_denied_does_not_write(self) -> None:
        original = "abc\n"
        self._write(original)
        res = self._run(
            [{"search": "abc", "replace": "xyz"}],
            safety="approval_required",
            approval="denied",
        )
        self.assertFalse(res["ok"])
        self.assertIn("denied", res["error"])
        self.assertEqual(self._read(), original)

    def test_cancelled_approval_does_not_write(self) -> None:
        original = "abc\n"
        self._write(original)
        res = self._run(
            [{"search": "abc", "replace": "xyz"}],
            safety="approval_required",
            approval="cancelled",
        )
        self.assertFalse(res["ok"])
        self.assertTrue(res["cancelled"])
        self.assertEqual(self._read(), original)

    def test_partial_apply_writes_and_reports(self) -> None:
        self._write("alpha\nbeta\n")
        res = self._run([
            {"search": "alpha", "replace": "ALPHA"},
            {"search": "missing", "replace": "x"},
        ])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["appliedCount"], 1)
        self.assertEqual(res["data"]["failedCount"], 1)
        self.assertEqual(res["data"]["diffCount"], 2)
        self.assertEqual(res["data"]["failedDiffs"][0]["index"], 1)
        self.assertEqual(self._read(), "ALPHA\nbeta\n")

    def test_success_result_fields(self) -> None:
        self._write("alpha\nbeta\n")
        res = self._run([{"search": "beta", "replace": "BETA"}])
        self.assertTrue(res["ok"])
        self.assertTrue(res["success"])
        data = res["data"]
        self.assertEqual(data["file"], self.NAME)
        self.assertEqual(data["status"], "accepted")
        self.assertEqual(data["diffCount"], 1)
        self.assertEqual(data["appliedCount"], 1)
        self.assertEqual(data["failedCount"], 0)
        self.assertEqual(data["result"], f"apply_diff on {self.NAME}: 1 applied, 0 rejected")
        self.assertEqual(self._read(), "alpha\nBETA\n")

    def test_escaping_path_valueerror_is_captured(self) -> None:
        res = self._run([{"search": "a", "replace": "b"}])
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"].lower())

        res = asyncio.run(apply_diff_mod.apply_diff(
            {"path": "bad\0name.txt", "diffs": [{"search": "a", "replace": "b"}]},
            FakeCtx(self.root),
        ))
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])
        self.assertEqual(list(self.root.iterdir()), [])


# ── write_file ───────────────────────────────────────────────────────────────


class WriteFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, args: dict[str, Any], **ctx_kw: Any) -> dict[str, Any]:
        return asyncio.run(write_file_mod.write_file(args, FakeCtx(self.root, **ctx_kw)))

    def test_invalid_path_valueerror_is_captured(self) -> None:
        res = self._run({"path": "bad\0name.txt", "content": "x"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])
        self.assertTrue(res["data"]["result"].startswith("ERROR:"))

    def test_success_result_fields(self) -> None:
        res = self._run({"path": "sub/note.txt", "content": "你好 🌍\n"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["path"], "sub/note.txt")
        self.assertEqual(res["data"]["bytes"], len("你好 🌍\n".encode("utf-8")))
        self.assertEqual(res["data"]["result"], "File written: sub/note.txt")
        self.assertEqual((self.root / "sub" / "note.txt").read_text(encoding="utf-8"), "你好 🌍\n")

    def test_empty_path_rejected(self) -> None:
        res = self._run({"path": "   ", "content": "x"})
        self.assertFalse(res["ok"])
        self.assertIn("empty path", res["error"])

    def test_directory_path_rejected(self) -> None:
        (self.root / "dir").mkdir()
        res = self._run({"path": "dir", "content": "x"})
        self.assertFalse(res["ok"])
        self.assertIn("directory", res["error"])

    def test_approval_denied_does_not_write(self) -> None:
        res = self._run(
            {"path": "note.txt", "content": "x"},
            safety="approval_required",
            approval="denied",
        )
        self.assertFalse(res["ok"])
        self.assertIn("denied", res["error"])
        self.assertFalse((self.root / "note.txt").exists())


# ── execute_command ──────────────────────────────────────────────────────────


class ExecuteCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, args: dict[str, Any], **ctx_kw: Any) -> dict[str, Any]:
        return asyncio.run(execute_command_mod.execute_command(args, FakeCtx(self.root, **ctx_kw)))

    def test_default_timeout_default_argument_is_90(self) -> None:
        seen: dict[str, Any] = {}
        real_get_float = execute_command_mod.get_float

        def spy(config: Any, key: str, default: float, **kwargs: Any) -> float:
            seen.setdefault("default", default)
            return real_get_float(config, key, default, **kwargs)

        with patch.object(execute_command_mod, "get_float", spy):
            res = self._run({"command": "echo hi"})

        self.assertTrue(res["ok"], res)
        self.assertEqual(seen["default"], 90.0)

    def test_configured_default_timeout_wins(self) -> None:
        (self.root / "config").mkdir()
        (self.root / "config" / "runtime.yaml").write_text(
            "meta:\n  execute_command:\n    default_timeout_sec: 1.0\n",
            encoding="utf-8",
        )
        res = self._run({"command": "sleep 5"})
        self.assertFalse(res["ok"])
        self.assertIn("timeout after 1.0s", res["error"])

    def test_explicit_timeout_argument_wins(self) -> None:
        res = self._run({"command": "sleep 5", "timeout_sec": 1})
        self.assertFalse(res["ok"])
        self.assertIn("timeout after 1.0s", res["error"])

    def test_success_result_fields(self) -> None:
        res = self._run({"command": "echo toolbox-migration"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["returncode"], 0)
        self.assertIn("toolbox-migration", res["data"]["output"])
        self.assertTrue(res["data"]["result"].startswith("returncode=0"))

    def test_nonzero_returncode_is_still_tool_success(self) -> None:
        res = self._run({"command": "exit 3"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["returncode"], 3)

    def test_approval_denied_does_not_execute(self) -> None:
        res = self._run(
            {"command": "touch marker.txt"},
            safety="approval_required",
            approval="denied",
        )
        self.assertFalse(res["ok"])
        self.assertFalse((self.root / "marker.txt").exists())


# ── system: git checkpoint before restart (temp repo only) ───────────────────


class SystemCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # A non-repository fixture must never discover the enclosing project.
        boundary = patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(self.root.parent)})
        boundary.start()
        self.addCleanup(boundary.stop)
        self._git("init")
        self._git("config", "user.name", "clonoth-test")
        self._git("config", "user.email", "clonoth-test@example.invalid")
        self._git("config", "commit.gpgsign", "false")
        (self.root / "a.py").write_text("a = 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("b = 1\n", encoding="utf-8")
        self._git("add", "a.py", "b.py")
        self._git("commit", "-m", "init")
        self.base_head = self._head()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, text=True, timeout=60,
        )

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _commit_count(self) -> int:
        return int(self._git("rev-list", "--count", "HEAD").stdout.strip())

    def _tracked(self) -> list[str]:
        return self._git("ls-files").stdout.split()

    def _tracked_status(self) -> str:
        return self._git("status", "--porcelain", "--untracked-files=no").stdout.strip()

    def _restart(self, **ctx_kw: Any) -> tuple[dict[str, Any], FakeCtx]:
        ctx = FakeCtx(self.root, **ctx_kw)
        res = asyncio.run(system_mod.request_restart({"target": "engine", "reason": "test"}, ctx))
        return res, ctx

    def test_untracked_only_does_not_block_restart_and_is_not_committed(self) -> None:
        (self.root / "scratch.py").write_text("scratch = 1\n", encoding="utf-8")

        res, ctx = self._restart()

        self.assertTrue(res["ok"], res)
        commit = res["data"]["git_commit"]
        self.assertTrue(commit["ok"])
        self.assertFalse(commit["committed"])
        self.assertEqual(self._head(), self.base_head)
        self.assertEqual(self._commit_count(), 1)
        self.assertNotIn("scratch.py", self._tracked())
        self.assertIn("scratch.py", self._git("status", "--porcelain").stdout)
        # 重启请求确实发生了（mock HTTP），没被空提交错误阻断
        self.assertTrue(any(url.endswith("/v1/admin/restart") for url, _ in ctx.http.posts))

    def test_untracked_files_never_enter_checkpoint(self) -> None:
        (self.root / "a.py").write_text("a = 3\n", encoding="utf-8")
        (self.root / "scratch.txt").write_text("scratch\n", encoding="utf-8")

        res, _ctx = self._restart()

        self.assertTrue(res["data"]["git_commit"]["committed"], res)
        self.assertNotIn("scratch.txt", self._tracked())
        self.assertTrue((self.root / "scratch.txt").exists())

    def test_tracked_change_is_committed(self) -> None:
        (self.root / "a.py").write_text("a = 2\n", encoding="utf-8")

        res, _ctx = self._restart()

        commit = res["data"]["git_commit"]
        self.assertTrue(commit["committed"], commit)
        self.assertEqual(self._commit_count(), 2)
        self.assertNotEqual(self._head(), self.base_head)
        self.assertIn("checkpoint before restart", self._git("log", "-1", "--pretty=%B").stdout)
        self.assertEqual(self._tracked_status(), "")

    def test_staged_and_unstaged_changes_are_committed_together(self) -> None:
        (self.root / "a.py").write_text("a = 'staged'\n", encoding="utf-8")
        self._git("add", "a.py")
        (self.root / "b.py").write_text("b = 'unstaged'\n", encoding="utf-8")

        res, _ctx = self._restart()

        commit = res["data"]["git_commit"]
        self.assertTrue(commit["committed"], commit)
        self.assertEqual(self._git("show", "HEAD:a.py").stdout, "a = 'staged'\n")
        self.assertEqual(self._git("show", "HEAD:b.py").stdout, "b = 'unstaged'\n")
        self.assertEqual(self._tracked_status(), "")
        # 快照 diff 必须同时覆盖已暂存与未暂存改动（否则 checkpoint 视图与实际提交不一致）
        diff_ref = res["data"]["git"]["git_diff_ref"]
        self.assertTrue(diff_ref)
        diff_text = (self.root / diff_ref).read_text(encoding="utf-8")
        self.assertIn("staged", diff_text)
        self.assertIn("unstaged", diff_text)

    def test_clean_repo_skips_commit_without_error(self) -> None:
        res, _ctx = self._restart()

        self.assertTrue(res["ok"], res)
        commit = res["data"]["git_commit"]
        self.assertTrue(commit["ok"])
        self.assertFalse(commit["committed"])
        self.assertEqual(self._commit_count(), 1)

    def test_staged_only_change_is_committed(self) -> None:
        (self.root / "a.py").write_text("a = 'only-staged'\n", encoding="utf-8")
        self._git("add", "a.py")

        res, _ctx = self._restart()

        self.assertTrue(res["data"]["git_commit"]["committed"], res)
        self.assertEqual(self._git("show", "HEAD:a.py").stdout, "a = 'only-staged'\n")

    def test_non_git_workspace_still_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            ctx = FakeCtx(plain)
            res = asyncio.run(system_mod.request_restart({"target": "engine"}, ctx))

        self.assertTrue(res["ok"], res)
        self.assertFalse(res["data"]["git"]["git_ok"])
        self.assertFalse(res["data"]["git_commit"]["ok"])
        self.assertTrue(any(url.endswith("/v1/admin/restart") for url, _ in ctx.http.posts))

    def test_restart_endpoint_failure_is_reported(self) -> None:
        ctx = FakeCtx(self.root, http_status=500)
        res = asyncio.run(system_mod.request_restart({"target": "engine"}, ctx))
        self.assertFalse(res["ok"])
        self.assertIn("git_commit", res["data"])


# ── search_in_files replace: template validation and write isolation ──────────


class ReplaceTemplateAndIsolationTests(unittest.TestCase):
    """Audit B: a bad template fails before any write; one file cannot sink the batch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _replace(self, args: dict[str, Any], ctx: FakeCtx | None = None) -> dict[str, Any]:
        ctx = ctx or FakeCtx(self.root)
        self._ctx = ctx
        return asyncio.run(search_in_files(args, ctx))

    def test_invalid_group_is_rejected_before_any_write(self) -> None:
        a = self._write("a.txt", "item_1\n")
        b = self._write("b.txt", "item_2\n")

        # $3 -> \\g<3> with only two groups: re rejects this on every file, so it
        # must be rejected once, up front, instead of escaping the worker thread.
        res = self._replace({
            "query": r"([a-z]+)_(\d+)", "isRegex": True,
            "mode": "replace", "replace": "$3:x",
        })

        self.assertFalse(res["ok"], res)
        self.assertIn("invalid replacement", res["error"])
        self.assertIn("invalid group reference 3", res["error"])
        # Nothing was written and no report claims a write happened.
        self.assertEqual(a.read_text(encoding="utf-8"), "item_1\n")
        self.assertEqual(b.read_text(encoding="utf-8"), "item_2\n")
        self.assertNotIn("filesModified", res["data"])
        self.assertNotIn("results", res["data"])
        # Rejected before path collection: not even a policy decision was asked.
        self.assertEqual(self._ctx.requests, [])

    def test_unknown_group_name_is_rejected_before_any_write(self) -> None:
        a = self._write("a.txt", "item_1\n")
        b = self._write("b.txt", "item_2\n")

        # re raises IndexError (not re.error) for an unknown group name.
        res = self._replace({
            "query": r"([a-z]+)_(\d+)", "isRegex": True,
            "mode": "replace", "replace": r"\g<missing>",
        })

        self.assertFalse(res["ok"], res)
        self.assertIn("invalid replacement", res["error"])
        self.assertEqual(a.read_text(encoding="utf-8"), "item_1\n")
        self.assertEqual(b.read_text(encoding="utf-8"), "item_2\n")
        self.assertEqual(self._ctx.requests, [])

    def test_second_file_io_failure_keeps_the_completed_write_reported(self) -> None:
        a = self._write("a.txt", "beta\n")
        b = self._write("b.txt", "beta\n")
        real_apply = sf_mod._apply_replace_file

        def _flaky(target: Path, rel: str, regex, repl: str, is_regex: bool):
            if rel == "b.txt":
                raise OSError("simulated write failure")
            return real_apply(target, rel, regex, repl, is_regex)

        with patch.object(sf_mod, "_apply_replace_file", _flaky):
            res = self._replace({"query": "beta", "mode": "replace", "replace": "gamma"})

        # The batch did not blow up, and the file that *was* written is reported.
        self.assertTrue(res["ok"], res)
        self.assertEqual(a.read_text(encoding="utf-8"), "gamma\n")
        self.assertEqual(b.read_text(encoding="utf-8"), "beta\n")
        self.assertEqual(res["data"]["filesModified"], 1)
        self.assertEqual(res["data"]["totalReplacements"], 1)
        self.assertEqual(res["data"]["results"], [{"file": "a.txt", "replacements": 1}])
        self.assertEqual(len(res["data"]["failures"]), 1)
        failure = res["data"]["failures"][0]
        self.assertEqual(failure["file"], "b.txt")
        self.assertIn("OSError: simulated write failure", failure["error"])
        self.assertIn("1 files modified, 1 replacements", res["data"]["result"])
        self.assertIn("failed: b.txt", res["data"]["result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)