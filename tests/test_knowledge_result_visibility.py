"""Knowledge management results must survive the real model-output formatter."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from engine.builtin.knowledge_inject import (
    build_knowledge_context,
    manage_memory,
    manage_skill,
)
from engine.node import _node_from_dict
from engine.tool_step import result_to_raw


def context(root: Path, workspace: str = "work"):
    return SimpleNamespace(workspace_root=root, workspace_name=workspace,
                           node_id="reader", _node_extra={})


def book(root: Path, namespace: str, name: str, entries: list[dict]):
    path = root / "data" / "memory" / namespace / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"book": name, "entries": entries}, allow_unicode=True))


def raw_data(tool: str, result: dict):
    assert result["ok"]
    kind, raw = result_to_raw(tool, result)
    assert kind == "text"
    return json.loads(raw)


@pytest.mark.asyncio
async def test_memory_list_and_full_id_pass_through_real_formatter(tmp_path):
    body = '完整正文\n"quoted"\\path\n' + "正文" * 6000 + "END-MARKER"
    book(tmp_path, "reader/@work", "notes", [{"id": "long", "content": body}])
    ctx = context(tmp_path)
    listed = await manage_memory({"action": "list"}, ctx)
    assert raw_data("manage_memory", listed) == listed["data"]["entries"]
    assert raw_data("manage_memory", listed)[0]["content"] == body[:200]
    full = await manage_memory({"action": "list", "book": "notes", "id": "long"}, ctx)
    assert raw_data("manage_memory", full)[0]["content"] == body


@pytest.mark.asyncio
async def test_model_output_preserves_workspace_and_namespace_isolation(tmp_path):
    for namespace, identifier in (("reader", "node"), ("reader/@work", "own"),
                                  ("reader/@other", "other_workspace"), ("other", "other_node")):
        book(tmp_path, namespace, "notes", [{"id": identifier, "content": identifier}])
    result = await manage_memory({}, context(tmp_path))
    assert {e["id"] for e in raw_data("manage_memory", result)} == {"node", "own"}
    for hidden in ("other_workspace", "other_node"):
        result = await manage_memory({"id": hidden}, context(tmp_path))
        assert result["data"]["entries"] == []
        assert result_to_raw("manage_memory", result)[1] == f"memory not found: {hidden}"


@pytest.mark.asyncio
async def test_default_remains_read_only_and_book_filter_is_retained(tmp_path):
    book(tmp_path, "reader", "notes", [{"id": "one", "content": "original"}])
    book(tmp_path, "reader", "rules", [{"id": "two", "content": "rule"}])
    path = tmp_path / "data/memory/reader/notes.yaml"
    before = path.read_bytes()
    result = await manage_memory({"book": "notes", "id": "one", "content": "not a write"}, context(tmp_path))
    assert raw_data("manage_memory", result)[0]["content"] == "original"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_skill_management_lists_metadata_not_bodies_and_normalizes_visibility(tmp_path):
    for name, visibility in (("public_one", "public"), ("private_one", "PRIVATE"), ("default_one", "invalid")):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\n" + yaml.safe_dump({"name": name, "description": name + " description",
                            "visibility": visibility, "strategy": "normal", "keywords": ["trigger"]})
                        + "---\nSECRET_BODY_" + name)
    result = await manage_skill({}, context(tmp_path))
    items = raw_data("manage_skill", result)
    assert items == result["data"]["skills"]
    assert {i["name"]: i["visibility"] for i in items} == {
        "default_one": "public", "private_one": "private", "public_one": "public"}
    assert "SECRET_BODY_" not in result_to_raw("manage_skill", result)[1]


@pytest.mark.asyncio
async def test_empty_results_remain_readable(tmp_path):
    for tool, handler, expected in (("manage_skill", manage_skill, "0 skills"),
                                    ("manage_memory", manage_memory, "0 memories")):
        assert result_to_raw(tool, await handler({}, context(tmp_path))) == ("text", expected)


def test_private_skill_injection_stays_hidden_until_pinned(tmp_path):
    path = tmp_path / "skills" / "private_skill" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: private_skill\nvisibility: private\nstrategy: normal\nkeywords: [trigger]\n---\nPRIVATE_BODY")
    node = _node_from_dict(tmp_path, "reader", {"id": "reader", "type": "ai", "prompt": "sys"})
    unpinned = build_knowledge_context(tmp_path, node, "trigger", [], {}, {"workspace_name": "work"})
    assert "PRIVATE_BODY" not in str(unpinned)
    from unittest.mock import patch
    with patch("engine.builtin.knowledge_inject.load_skill_pins", return_value={"work": ["private_skill"]}):
        pinned = build_knowledge_context(tmp_path, node, "trigger", [], {}, {"workspace_name": "work"})
    assert "PRIVATE_BODY" in str(pinned)
