"""Session-owned Todo storage, migration and task authorization (offline).

Run: TMPDIR=$PWD/data/temp python3.11 -B -m pytest tests/test_todo_session_store.py
"""
import copy
import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.ide import todo


def document(sid="main", count=0, revision=0):
    return {
        "schema_version": 2, "session_id": sid, "revision": revision,
        "items": [{"id": f"item-{n}", "title": f"Goal step {n}", "status": "pending",
                   "note": f"Evidence requirement {n}", "created_at": "2026-09-10T01:00:00+00:00",
                   "updated_at": "2026-09-10T01:00:00+00:00",
                   "updated_by": {"type": "model", "task_id": "original"}}
                  for n in range(count)],
        "updated_at": "2026-09-10T01:00:00+00:00", "updated_by": {"type": "model", "task_id": "original"},
    }


def update(sid="main", revision=0, **changes):
    return {"expected_session_id": sid, "expected_revision": revision,
            "changes": [{"title": "A"}], **changes}


def legacy(root, data, workspace="/missing-directory"):
    data = {**copy.deepcopy(data), "schema_version": 1, "workspace": {"name": "project", "path": workspace}}
    key = hashlib.sha256(json.dumps([data["session_id"], workspace], ensure_ascii=False).encode()).hexdigest()
    path = root / "data" / "ide" / "todos" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def state(root):
    records = {
        "main": {"node_id": "entry"},
        "other": {"node_id": "entry"},
        "branch": {"node_id": "__entry_branch__", "parent_session_id": "main"},
        "child": {"node_id": "coder", "is_child": True, "parent_session_id": "branch"},
        "nested": {"node_id": "coder", "is_child": True, "parent_session_id": "child"},
        "async-child": {"node_id": "coder", "is_child": True, "parent_session_id": "main"},
        "async-branch": {"node_id": "__entry_branch__", "parent_session_id": "async-child"},
        "child-entry": {"node_id": "__entry_branch__", "parent_session_id": "child"},
    }
    return SimpleNamespace(workspace_root=root, _lock=threading.RLock(),
                           _session_store=SimpleNamespace(_registry=records), tasks={},
                           session_generations={"main": 4, "async-child": 6, "child": 9})


def task(sid="main", generation=4, caller=None, inp=None, **kw):
    data = dict(session_id=sid, session_generation=generation, caller_task_id=caller,
                input=inp or {}, worker_id="worker", node_id="coder", status="running", cancel_requested=False)
    data.update(kw)
    return SimpleNamespace(**data)


def node():
    return SimpleNamespace(tool_access=SimpleNamespace(mode="all", allow=[], deny=[]))


def execute(st, name="task", patch=None, generation=None, worker="worker", arbitrary_sid=""):
    return todo.operate(st, arbitrary_sid, patch, task_id=name, worker_id=worker,
                        generation=st.tasks[name].session_generation if generation is None else generation)


def test_seven_legacy_goal_items_migrate_without_changing_records(tmp_path):
    old_data = document(count=7, revision=1)
    old_path = legacy(tmp_path, old_data)
    before = old_path.read_bytes()
    loaded = todo.read_list(tmp_path, "main")
    assert loaded == old_data
    assert old_path.read_bytes() == before
    assert json.loads(todo._path(tmp_path, "main").read_text()) == old_data
    assert todo.read_list(tmp_path, "main") == old_data


def test_v2_is_authoritative_after_migration_and_old_backup_remains(tmp_path):
    old = legacy(tmp_path, document(count=1, revision=7))
    st = state(tmp_path)
    updated = todo.operate(st, "main", update(revision=7))
    assert updated["revision"] == 8 and len(updated["items"]) == 2
    assert todo.read_list(tmp_path, "main") == updated
    assert json.loads(old.read_text())["revision"] == 7
    # Legacy snapshots are backups, never replayed over newer v2 state.
    old.write_text("broken backup")
    assert todo.read_list(tmp_path, "main") == updated


def test_multiple_legacy_workspace_lists_are_not_silently_merged(tmp_path):
    paths = [legacy(tmp_path, document(count=1), ws) for ws in ("/one", "/two")]
    before = [p.read_bytes() for p in paths]
    with pytest.raises(HTTPException) as caught:
        todo.operate(state(tmp_path), "main", update())
    assert caught.value.status_code == 409
    assert not todo._path(tmp_path, "main").exists()
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize("bad", ["{broken", '{"session_id":"main","schema_version":1}', '[]'])
def test_corrupt_legacy_data_does_not_become_an_empty_new_list(tmp_path, bad):
    p = legacy(tmp_path, document())
    p.write_text(bad)
    with pytest.raises(HTTPException) as caught:
        todo.operate(state(tmp_path), "main", update())
    assert caught.value.status_code == 500
    assert p.read_text() == bad
    assert not todo._path(tmp_path, "main").exists()


def test_no_legacy_lists_means_no_write_on_empty_read(tmp_path):
    st = state(tmp_path)
    assert todo.operate(st, "main")["items"] == []
    assert not todo._path(tmp_path, "main").exists()
    todo.operate(st, "main", update())
    assert todo.operate(st, "other")["items"] == []


def test_migration_write_failure_preserves_legacy(tmp_path):
    p = legacy(tmp_path, document(count=7, revision=1))
    before = p.read_bytes()
    with mock_patch.object(todo.os, "replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            todo.read_list(tmp_path, "main")
    assert p.read_bytes() == before
    assert not todo._path(tmp_path, "main").exists()
    assert not list(todo._path(tmp_path, "main").parent.glob(".todos-*"))


@pytest.mark.parametrize("missing", [True, False])
def test_expected_session_id_required_even_for_revision_zero(tmp_path, missing):
    st = state(tmp_path)
    change = update("main")
    if missing:
        change.pop("expected_session_id")
    else:
        change["expected_session_id"] = "child"
    with pytest.raises(HTTPException) as caught:
        todo.operate(st, "main", change)
    assert caught.value.status_code == (422 if missing else 409)
    assert not todo._path(tmp_path, "main").exists()


def test_entry_branches_share_but_ordinary_children_do_not(tmp_path):
    st = state(tmp_path)
    main = todo.operate(st, "branch", update("main"))
    assert main == todo.operate(st, "main")
    assert todo.operate(st, "child")["items"] == []
    child = todo.operate(st, "child-entry", update("child"))
    assert child == todo.operate(st, "child")
    assert child != main
    assert todo.operate(st, "nested")["items"] == []


@pytest.mark.parametrize("style", ["main", "branch", "sync", "nested", "async", "async-branch", "own-child-with-caller"])
def test_task_owner_uses_trusted_execution_session(tmp_path, style):
    st = state(tmp_path)
    st.tasks["caller"] = task("branch")
    expected = "main"
    current = task()
    if style == "branch":
        current = task("branch")
    elif style == "sync":
        current = task("branch", caller="caller", inp={"child_session_id": "child"})
        expected = "child"
    elif style == "nested":
        st.tasks["caller"] = task("branch", inp={"child_session_id": "child"})
        current = task("branch", caller="caller", inp={"child_session_id": "nested"})
        expected = "nested"
    elif style in ("async", "async-branch"):
        current = task("async-child" if style == "async" else "async-branch", generation=6,
                       inp={"_dispatch_origin": {"parent_session_id": "main"}})
        expected = "async-child"
    elif style == "own-child-with-caller":
        current = task("child", generation=9, caller="caller")
        expected = "child"
    st.tasks["task"] = current
    with mock_patch("engine.node.load_node", return_value=node()):
        result = execute(st, patch=update(expected), arbitrary_sid="other")
    assert result["session_id"] == expected
    assert len(todo.read_list(tmp_path, expected)["items"]) == 1
    for sid in ("main", "child", "nested", "other", "async-child"):
        if sid != expected:
            assert todo.read_list(tmp_path, sid)["items"] == []


@pytest.mark.parametrize("caller_present", [True, False])
def test_legacy_child_without_own_session_cannot_write_parent(tmp_path, caller_present):
    st = state(tmp_path)
    if caller_present:
        st.tasks["caller"] = task("branch", inp={"child_session_id": "child"})
    st.tasks["task"] = task("child", generation=9, caller="caller")
    with mock_patch("engine.node.load_node", return_value=node()):
        with pytest.raises(HTTPException) as caught:
            execute(st, patch=update("child"))
    assert caught.value.status_code == 403
    assert not todo._path(tmp_path, "child").exists()


@pytest.mark.parametrize("origin", [{"parent_session_id": "main"}, {"caller_node_id": "coder"}])
def test_async_dispatch_without_independent_session_cannot_access_parent(tmp_path, origin):
    st = state(tmp_path)
    st.tasks["task"] = task("branch", inp={"_dispatch_origin": origin})
    with mock_patch("engine.node.load_node", return_value=node()):
        with pytest.raises(HTTPException) as caught:
            execute(st, patch=update("main"))
    assert caught.value.status_code == 403
    assert not todo._path(tmp_path, "main").exists()


@pytest.mark.parametrize("change", ["worker", "generation", "cancel", "status", "reset", "owner_generation", "system", "node_deny"])
def test_task_auth_checks_remain_enforced(tmp_path, change):
    st = state(tmp_path)
    st.tasks["task"] = task("branch", caller="caller", inp={"child_session_id": "child"})
    nd = node()
    kwargs = {}
    expected = 409
    if change == "worker":
        kwargs["worker"] = "other"
    elif change == "generation":
        kwargs["generation"] = 3
    elif change == "cancel":
        st.tasks["task"].cancel_requested = True
    elif change == "status":
        st.tasks["task"].status = "completed"
    elif change == "reset":
        st._session_store._registry["child"]["reset"] = True
        expected = 404
    elif change == "owner_generation":
        st.session_generations["main"] = 5
    elif change == "system":
        st.tasks["task"].input["_system_task"] = True
        expected = 403
    else:
        nd.tool_access.deny = [todo.TOOL_UPDATE]
        expected = 403
    with mock_patch("engine.node.load_node", return_value=nd):
        with pytest.raises(HTTPException) as caught:
            execute(st, patch=update("child"), **kwargs)
    assert caught.value.status_code == expected
    assert not todo._path(tmp_path, "child").exists()


def test_concurrent_same_revision_writes_have_one_winner(tmp_path):
    st = state(tmp_path)
    barrier = threading.Barrier(2)
    def attempt(title):
        barrier.wait()
        try:
            return todo.operate(st, "main", update(changes=[{"title": title}]))
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("first", "second")))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 409 in results
    assert todo.read_list(tmp_path, "main")["revision"] == 1
