"""Policy engine audit fixes: construction order + install-root guard.

Two production failures are pinned here, both against the **real**
``supervisor.policy.PolicyEngine`` (no fake policy, no absolute-path pattern
shortcut):

1. ``PolicyEngine.__init__`` had its initialisation block stranded after a
   ``return`` inside the ``extra_roots`` property, so no ``_read_default`` /
   ``_write_rules`` was ever assigned and *every* ``evaluate_*`` raised
   ``AttributeError``. The defaults must also be byte-compiled *before* the
   policy file is created or read, so a missing / unreadable / corrupt
   ``policy.yaml`` never leaves the engine without rules (which would fail open
   on ``.env`` and ``data/policy.yaml``).

2. ``classify_path`` reports the *active workspace* first. With the workspace
   set to an install subdirectory (``data/``, ``engine/``) or to an ancestor of
   the install root, ``data/policy.yaml`` arrives as ``policy.yaml`` and
   ``engine/x.py`` as ``x.py``, so the fixed install-root rules silently missed
   and the far looser workspace default applied. Policy now re-derives the
   install-root-relative path and applies both rule sets, strictest wins.

Everything runs in a tempdir; no real ``data/`` config is read, no service is
started, no network is used.

Run:
    python3.11 -B tests/test_policy_audit_fixes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from supervisor.policy import PolicyEngine


def _level(decision) -> str:
    return decision.safety_level.value


class _PolicyCase(unittest.TestCase):
    """Install root under a base dir, so an *ancestor* workspace is testable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "proj"
        (self.root / "data").mkdir(parents=True)
        (self.root / "engine").mkdir(parents=True)
        (self.root / "supervisor").mkdir(parents=True)
        (self.root / "engine" / "runner.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.root / "a.txt").write_text("plain\n", encoding="utf-8")
        self.engine = PolicyEngine(workspace_root=self.root)

    def outside_file(self, name: str = "ext.txt") -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name).resolve() / name
        target.write_text("external\n", encoding="utf-8")
        return target

    def write_policy(self, data: dict) -> None:
        path = self.root / "data" / "policy.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        # force a reload even on coarse-mtime filesystems
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 10))


# ---------------------------------------------------------------------------
#  construction order + broken / missing configuration
# ---------------------------------------------------------------------------

class PolicyConstructionTest(_PolicyCase):
    def test_defaults_are_compiled_at_construction(self) -> None:
        # Regression: these all raised AttributeError before the fix.
        self.assertEqual(_level(self.engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="data/policy.yaml")), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="engine/x.py")), "approval_required")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="a.txt")), "auto")
        self.assertEqual(_level(self.engine.evaluate_tool(name="manage_schedule")), "auto")
        self.assertEqual(_level(self.engine.evaluate_tool(name="unlisted")), "approval_required")
        self.assertEqual(_level(self.engine.evaluate_execute_command(command="ls")), "approval_required")
        self.assertEqual(_level(self.engine.evaluate_restart(target="engine")), "approval_required")
        self.assertEqual(self.engine.extra_roots, [])

    def test_default_policy_file_is_created_for_a_fresh_workspace(self) -> None:
        self.assertTrue((self.root / "data" / "policy.yaml").is_file())

    def test_uncreatable_policy_file_still_denies_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(PolicyEngine, "_ensure_policy_file_exists"):
                engine = PolicyEngine(workspace_root=Path(directory))
            self.assertEqual(_level(engine.evaluate_read_file(path=".env")), "deny")
            self.assertEqual(_level(engine.evaluate_read_file(path="sub/.env")), "deny")
            self.assertEqual(_level(engine.evaluate_write_file(path=".env")), "deny")
            self.assertEqual(_level(engine.evaluate_write_file(path="data/policy.yaml")), "deny")
            self.assertEqual(_level(engine.evaluate_tool(name="unlisted_tool")), "approval_required")
            self.assertEqual(engine.extra_roots, [])

    def test_policy_path_that_is_a_directory_is_unreadable_not_fatal(self) -> None:
        directory_policy = self.root / "data" / "as_dir.yaml"
        directory_policy.mkdir()
        engine = PolicyEngine(workspace_root=self.root, policy_path=directory_policy)
        self.assertEqual(_level(engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(engine.evaluate_write_file(path="data/policy.yaml")), "deny")
        self.assertEqual(_level(engine.evaluate_write_file(path="engine/runner.py")), "approval_required")

    def test_corrupt_yaml_falls_back_to_safe_defaults(self) -> None:
        corrupt = self.root / "data" / "corrupt.yaml"
        corrupt.write_text("read_file: [unclosed\n\t bad: : :\n", encoding="utf-8")
        engine = PolicyEngine(workspace_root=self.root, policy_path=corrupt)
        self.assertEqual(_level(engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(engine.evaluate_write_file(path="data/policy.yaml")), "deny")

    def test_non_mapping_document_falls_back_to_safe_defaults(self) -> None:
        plain = self.root / "data" / "plain.yaml"
        plain.write_text("- just\n- a\n- list\n", encoding="utf-8")
        engine = PolicyEngine(workspace_root=self.root, policy_path=plain)
        self.assertEqual(_level(engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(engine.evaluate_write_file(path="data/policy.yaml")), "deny")

    def test_broken_section_type_keeps_the_safe_builtin_section(self) -> None:
        # A section that is not a mapping is a broken config, not "go auto":
        # dropping the rules would turn .env reads into an auto-approve.
        self.write_policy({"read_file": "oops", "write_file": 42, "tools": ["nope"]})
        self.assertEqual(_level(self.engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="data/policy.yaml")), "deny")
        self.assertEqual(_level(self.engine.evaluate_tool(name="unlisted")), "approval_required")

    def test_empty_policy_document_keeps_every_default(self) -> None:
        self.write_policy({})
        self.assertEqual(_level(self.engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="engine/x.py")), "approval_required")

    def test_policy_unlinked_after_load_keeps_the_last_known_rules(self) -> None:
        (self.root / "data" / "policy.yaml").unlink()
        # stat() now fails: the engine must keep the compiled rules, not fall
        # back to the loosest possible answer.
        self.assertEqual(_level(self.engine.evaluate_read_file(path=".env")), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path="data/policy.yaml")), "deny")

    def test_edited_policy_is_picked_up_lazily(self) -> None:
        self.assertEqual(_level(self.engine.evaluate_write_file(path="notes/x.md")), "auto")
        self.write_policy({"write_file": {"default": "auto", "rules": [
            {"pattern": "notes/**", "decision": "deny", "reason": "locked notes"},
        ]}})
        decision = self.engine.evaluate_write_file(path="notes/x.md")
        self.assertEqual(_level(decision), "deny")
        self.assertIn("locked notes", decision.reason)

    def test_bad_extra_roots_do_not_break_construction(self) -> None:
        self.write_policy({"extra_roots": [None, 7, "", "   ", "../shared"]})
        self.assertEqual(self.engine.extra_roots, [(self.base / "shared")])

    def test_unknown_op_and_blank_arguments_are_denied(self) -> None:
        self.assertEqual(_level(self.engine.evaluate(op="teleport", parameters={})), "deny")
        self.assertEqual(_level(self.engine.evaluate_execute_command(command="   ")), "deny")
        self.assertEqual(_level(self.engine.evaluate_restart(target="everything")), "deny")


# ---------------------------------------------------------------------------
#  install-root protection under a non-root active workspace
# ---------------------------------------------------------------------------

class InstallRootGuardTest(_PolicyCase):
    """The workspace may be a subdirectory, the root itself, or an ancestor."""

    def test_workspace_at_install_root_is_unchanged(self) -> None:
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="data/policy.yaml", workspace=self.root)), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="engine/runner.py", workspace=self.root)), "approval_required")
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="a.txt", workspace=self.root)), "auto")

    def test_data_subdir_workspace_cannot_rewrite_policy(self) -> None:
        workspace = self.root / "data"
        for path in ("policy.yaml", str(self.root / "data" / "policy.yaml"), "./policy.yaml"):
            decision = self.engine.evaluate_write_file(path=path, workspace=workspace)
            self.assertEqual(_level(decision), "deny", path)
            self.assertIn("policy", decision.reason)

    def test_data_subdir_workspace_still_denies_a_plain_env_read(self) -> None:
        workspace = self.root / "data"
        self.assertEqual(_level(self.engine.evaluate_read_file(path=".env", workspace=workspace)), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(path=".env", workspace=workspace)), "deny")
        # and the real install-root .env is denied by its root-relative name
        self.assertEqual(_level(self.engine.evaluate_read_file(
            path=str(self.root / ".env"), workspace=workspace)), "deny")

    def test_engine_subdir_workspace_keeps_engine_source_approval(self) -> None:
        workspace = self.root / "engine"
        decision = self.engine.evaluate_write_file(path="runner.py", workspace=workspace)
        self.assertEqual(_level(decision), "approval_required")
        self.assertIn("engine", decision.reason)
        # every file the active workspace resolves inside engine/ is source
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="runner.py", workspace=workspace)), "approval_required")
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="new_module.py", workspace=workspace)), "approval_required")
        # but an unruled file of the install root reached from this workspace
        # keeps its ordinary auto default (no over-tightening)
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="../a.txt", workspace=workspace)), "auto")

    def test_supervisor_subdir_workspace_keeps_source_approval(self) -> None:
        workspace = self.root / "supervisor"
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path="policy.py", workspace=workspace)), "approval_required")

    def test_ancestor_workspace_still_protects_the_install_root(self) -> None:
        # workspace = the directory *containing* the install root
        for rel, expected in (("data/policy.yaml", "deny"),
                              ("engine/runner.py", "approval_required"),
                              ("a.txt", "auto")):
            decision = self.engine.evaluate_write_file(
                path=f"proj/{rel}", workspace=self.base)
            self.assertEqual(_level(decision), expected, rel)
        self.assertEqual(_level(self.engine.evaluate_read_file(
            path="proj/.env", workspace=self.base)), "deny")

    def test_absolute_paths_are_guarded_the_same_way(self) -> None:
        workspace = self.root / "data"
        absolute = str(self.root / "data" / "policy.yaml")
        self.assertEqual(_level(self.engine.evaluate_write_file(path=absolute, workspace=workspace)), "deny")
        # absolute install-root path from a *foreign* workspace: trusted tier
        external_ws = self.outside_file().parent
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path=str(self.root / "data" / "policy.yaml"), workspace=external_ws)), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path=str(self.root / "engine" / "runner.py"), workspace=external_ws)), "approval_required")

    def test_stricter_workspace_rule_is_not_loosened_by_the_root_pass(self) -> None:
        self.write_policy({"write_file": {"default": "auto", "rules": [
            {"pattern": "secret.txt", "decision": "deny", "reason": "workspace locked"},
        ]}})
        decision = self.engine.evaluate_write_file(path="secret.txt", workspace=self.root / "data")
        self.assertEqual(_level(decision), "deny")
        self.assertIn("workspace locked", decision.reason)

    def test_looser_workspace_rule_cannot_override_the_install_root(self) -> None:
        # The workspace-relative name of data/policy.yaml is just "policy.yaml";
        # a rule that auto-allows it must not beat the install-root deny.
        self.write_policy({"write_file": {"default": "auto", "rules": [
            {"pattern": "policy.yaml", "decision": "auto", "reason": "workspace loosened"},
            {"pattern": "data/policy.yaml", "decision": "deny", "reason": "policy is human-only"},
        ]}})
        decision = self.engine.evaluate_write_file(path="policy.yaml", workspace=self.root / "data")
        self.assertEqual(_level(decision), "deny")
        self.assertIn("human-only", decision.reason)
        # the display/trust semantics themselves are untouched
        self.assertEqual(decision.trust_level, "workspace")

    def test_looser_workspace_rule_cannot_override_engine_approval(self) -> None:
        self.write_policy({"write_file": {"default": "auto", "rules": [
            {"pattern": "runner.py", "decision": "auto"},
            {"pattern": "engine/**", "decision": "approval_required", "reason": "engine source"},
        ]}})
        decision = self.engine.evaluate_write_file(path="runner.py", workspace=self.root / "engine")
        self.assertEqual(_level(decision), "approval_required")
        self.assertIn("engine source", decision.reason)

    def test_external_project_workspace_keeps_workspace_trust(self) -> None:
        # The fix must not push legitimate out-of-tree projects into approval.
        project = self.outside_file("notes.txt").parent
        (project / "notes.txt").write_text("hi\n", encoding="utf-8")
        decision = self.engine.evaluate_write_file(path="notes.txt", workspace=project)
        self.assertEqual(_level(decision), "auto")
        self.assertEqual(decision.trust_level, "workspace")

    def test_external_project_workspace_cannot_write_install_root_sources(self) -> None:
        project = self.outside_file("notes.txt").parent
        decision = self.engine.evaluate_write_file(
            path=str(self.root / "data" / "policy.yaml"), workspace=project)
        self.assertEqual(_level(decision), "deny")
        self.assertEqual(_level(self.engine.evaluate_write_file(
            path=str(self.root / "engine" / "runner.py"), workspace=project)), "approval_required")

    def test_trusted_extra_root_is_not_treated_as_install_root(self) -> None:
        shared = self.base / "shared"
        shared.mkdir()
        (shared / "doc.txt").write_text("shared\n", encoding="utf-8")
        self.write_policy({"extra_roots": ["../shared"]})
        decision = self.engine.evaluate_write_file(path=str(shared / "doc.txt"))
        self.assertEqual(decision.trust_level, "trusted")
        self.assertEqual(_level(decision), "auto")
        # and rules written for install-root names never leak onto it
        self.write_policy({"extra_roots": ["../shared"], "write_file": {"default": "auto", "rules": [
            {"pattern": "data/policy.yaml", "decision": "deny"},
        ]}})
        self.assertEqual(_level(self.engine.evaluate_write_file(path=str(shared / "doc.txt"))), "auto")

    def test_external_absolute_path_is_still_approval(self) -> None:
        target = self.outside_file()
        for workspace in (self.root, self.root / "data", self.root / "engine", None):
            decision = self.engine.evaluate_write_file(path=str(target), workspace=workspace)
            self.assertEqual(decision.trust_level, "external", workspace)
            self.assertEqual(_level(decision), "approval_required", workspace)
            self.assertEqual(_level(self.engine.evaluate_read_file(
                path=str(target), workspace=workspace)), "approval_required")

    def test_relative_escape_is_denied_from_any_workspace(self) -> None:
        for workspace in (self.root, self.root / "data", self.root / "engine", self.base):
            decision = self.engine.evaluate_write_file(
                path="../../etc/passwd", workspace=workspace)
            self.assertEqual(_level(decision), "deny", workspace)

    def test_symlink_escaping_the_root_is_external(self) -> None:
        target = self.outside_file()
        link = self.root / "data" / "link.txt"
        link.symlink_to(target)
        decision = self.engine.evaluate_write_file(
            path=str(link), workspace=self.root / "data")
        self.assertEqual(decision.trust_level, "external")
        self.assertEqual(_level(decision), "approval_required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
