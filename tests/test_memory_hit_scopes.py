"""Scoped hit sidecar integration, including actual CRUD and Dream readers."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from engine.builtin import memory_hits as hits
from engine.builtin import knowledge_inject as knowledge
from engine.builtin.dream import DreamHandler
from engine.tool_step import result_to_raw


@pytest.fixture(autouse=True)
def isolated_pending(monkeypatch):
    monkeypatch.setattr(hits, "_pending", {})
    monkeypatch.setattr(hits, "_last_flush", {})
    monkeypatch.setattr(knowledge, "_guard_mutation", AsyncMock(return_value=None))


def book(root, scope, name="notes", entry_id="same", **extra):
    path = root / "data/memory" / scope / (name + ".yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"book": name, "entries": [
        {"id": entry_id, "content": scope + "/" + name, "keywords": ["trigger"], **extra}
    ]}))
    return path


def ctx(root, namespace="owner", workspace=""):
    return SimpleNamespace(workspace_root=root, node_id=namespace,
                           workspace_name=workspace, _node_extra={})


def record(root, scope):
    knowledge.invalidate_memory_cache(root, memory_book=scope)
    entries = knowledge.normalize_memory_entries(knowledge.load_memory_catalog(root, memory_book=scope))
    knowledge._record_hits(root, entries)
    return {e["_hit_key"] for e in entries}


def sidecar(root, data):
    path = root / "data/memory/.hit_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_catalog_record_delete_flush_reload_is_scoped(tmp_path):
    a = book(tmp_path, "owner", "a")
    b = book(tmp_path, "owner", "b")
    c = book(tmp_path, "owner/@work")
    d = book(tmp_path, "other")
    for scope in ("owner", "owner/@work", "other"):
        record(tmp_path, scope)
    knowledge._flush_hit_cache(tmp_path)
    keys = {hits.hit_key(tmp_path, p, "same") for p in (a, b, c, d)}
    assert set(hits.read_hits(tmp_path)) == keys
    # Cold process: deletion cannot rely on an initialized in-memory cache.
    hits._pending.clear()
    result = asyncio.run(knowledge.manage_memory({"action": "delete", "book": "a", "id": "same"}, ctx(tmp_path)))
    assert result["ok"] and not a.exists()
    hits.flush_hits(tmp_path)
    hits._pending.clear()
    assert set(hits.read_hits(tmp_path)) == keys - {hits.hit_key(tmp_path, a, "same")}
    assert b.exists() and c.exists() and d.exists()
    record(tmp_path, "owner")
    hits.flush_hits(tmp_path)
    assert hits.hit_key(tmp_path, a, "same") not in hits.read_hits(tmp_path)


def test_legacy_ids_not_guessed_and_last_deleted_id_is_removed(tmp_path):
    a = book(tmp_path, "owner", "a")
    b = book(tmp_path, "other", "b")
    now = datetime.now(timezone.utc).isoformat()
    path = sidecar(tmp_path, {"same": now, "unknown": now})
    assert asyncio.run(knowledge.delete_memory({"book": "a", "id": "same"}, ctx(tmp_path)))["ok"]
    assert hits.read_hits(tmp_path)["same"] == now
    assert json.loads(DreamHandler()._load_hit_cache_json(tmp_path)) == {}
    assert hits.read_hits(tmp_path)["same"] == now  # Dream is read-only.
    assert asyncio.run(knowledge.delete_memory({"book": "b", "id": "same"}, ctx(tmp_path, "other")))["ok"]
    assert json.loads(path.read_text()) == {"unknown": now}
    assert not a.exists() and not b.exists()


def test_dream_scoped_view_does_not_rewrite_other_scopes_or_old_hits(tmp_path):
    a = book(tmp_path, "owner")
    b = book(tmp_path, "other/@work")
    scratch = book(tmp_path, "system.dream")
    record(tmp_path, "owner")
    record(tmp_path, "other/@work")
    hits.flush_hits(tmp_path)
    data = hits.read_hits(tmp_path)
    data[hits.hit_key(tmp_path, scratch, "same")] = datetime.now(timezone.utc).isoformat()
    data["legacy"] = "2000-01-01T00:00:00+00:00"
    path = sidecar(tmp_path, data)
    before = path.read_bytes()
    dream = DreamHandler()
    entries = dream._load_memory_topology_entries(tmp_path)
    view = json.loads(dream._load_hit_cache_json(tmp_path, entries=entries))
    assert set(view) == {hits.hit_key(tmp_path, p, "same") for p in (a, b)}
    assert json.loads(dream._load_hit_cache_json(tmp_path, valid_ids=set())) == {}
    assert path.read_bytes() == before
    clusters = json.loads(dream._build_keyword_topology_json(tmp_path))["clusters"]
    assert {e["hit_key"] for e in clusters[0]["entries"]} == set(view)


def test_root_isolation_and_flush_all(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        book(root, "owner")
        record(root, "owner")
    hits.flush_hits()
    assert hits.read_hits(first) and hits.read_hits(second)
    asyncio.run(knowledge.delete_memory({"book": "notes", "id": "same"}, ctx(first)))
    assert hits.read_hits(first) == {}
    assert hits.read_hits(second)


def test_stale_pending_hit_cannot_resurrect_deleted_entry(tmp_path):
    path = book(tmp_path, "owner")
    record(tmp_path, "owner")
    path.unlink()  # Another process has removed the book before this flush.
    hits.flush_hits(tmp_path)
    assert hits.read_hits(tmp_path) == {}


def test_atomic_failure_retains_sidecar_and_pending_changes(tmp_path, monkeypatch):
    book(tmp_path, "owner")
    path = sidecar(tmp_path, {"legacy": "2026-01-01"})
    before = path.read_bytes()
    record(tmp_path, "owner")
    original = hits.os.replace
    monkeypatch.setattr(hits.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("simulated")))
    hits.flush_hits(tmp_path)
    assert path.read_bytes() == before
    assert hits._pending[tmp_path.resolve()]
    assert not list(path.parent.glob(".hit_cache-*"))
    monkeypatch.setattr(hits.os, "replace", original)
    hits.flush_hits(tmp_path)
    assert len(hits.read_hits(tmp_path)) == 2
    assert not hits._pending[tmp_path.resolve()]


def test_delete_cleanup_retries_after_sidecar_failure(tmp_path, monkeypatch):
    book(tmp_path, "owner")
    record(tmp_path, "owner")
    hits.flush_hits(tmp_path)
    original = hits.os.replace
    monkeypatch.setattr(hits.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("simulated")))
    assert asyncio.run(knowledge.delete_memory({"book": "notes", "id": "same"}, ctx(tmp_path)))["ok"]
    assert hits.read_hits(tmp_path)  # Failed sidecar replacement left original intact.
    monkeypatch.setattr(hits.os, "replace", original)
    hits.flush_hits(tmp_path)
    assert hits.read_hits(tmp_path) == {}


def test_failed_or_denied_book_delete_preserves_hits(tmp_path, monkeypatch):
    path = book(tmp_path, "owner")
    record(tmp_path, "owner")
    hits.flush_hits(tmp_path)
    before = hits.read_hits(tmp_path)
    monkeypatch.setattr(knowledge, "_guard_mutation", AsyncMock(return_value={"ok": False, "error": "denied"}))
    assert not asyncio.run(knowledge.delete_memory({"book": "notes", "id": "same"}, ctx(tmp_path)))["ok"]
    monkeypatch.setattr(knowledge, "_guard_mutation", AsyncMock(return_value=None))
    original = Path.unlink
    def fail(target, *args, **kwargs):
        if target == path:
            raise PermissionError("simulated")
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    assert not asyncio.run(knowledge.delete_memory({"book": "notes", "id": "same"}, ctx(tmp_path)))["ok"]
    assert hits.read_hits(tmp_path) == before
    assert path.exists()


def test_manual_read_same_id_scope_and_node_ids_contract_unchanged(tmp_path):
    for scope in ("owner", "owner/@work", "owner/@other", "elsewhere"):
        book(tmp_path, scope, node_ids=["another_node"])
    response = asyncio.run(knowledge.manage_memory({"action": "list", "book": "notes", "id": "same"}, ctx(tmp_path, workspace="work")))
    kind, raw = result_to_raw("manage_memory", response)
    assert kind == "text"
    assert {e["content"] for e in json.loads(raw)} == {"owner/notes", "owner/@work/notes"}
    assert {e["scope"] for e in json.loads(raw)} == {"node", "workspace"}


def test_corrupt_sidecar_is_not_overwritten(tmp_path):
    book(tmp_path, "owner")
    path = sidecar(tmp_path, {})
    path.write_text("{not json")
    record(tmp_path, "owner")
    hits.flush_hits(tmp_path)
    assert path.read_text() == "{not json"
    assert hits._pending[tmp_path.resolve()]


def test_scoped_key_rejects_escape(tmp_path):
    with pytest.raises(ValueError):
        hits.hit_key(tmp_path, tmp_path / "outside.yaml", "same")
    with pytest.raises(ValueError):
        hits._scoped_entry_exists(tmp_path, json.dumps(["../outside.yaml", "same"]))
