from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict


@dataclass
class Workflow:
    id: str
    name: str
    entry_node: str
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, str]] = field(default_factory=dict)  # node_id -> {outcome -> target_node_id}


def load_workflow(workspace_root: Path, workflow_id: str) -> Workflow | None:
    wid = (workflow_id or "").strip()
    if not wid:
        return None
    data = load_yaml_dict(workspace_root / "config" / "workflows" / f"{wid}.yaml")
    if not isinstance(data, dict) or str(data.get("kind") or "workflow").strip() != "workflow":
        return None
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict):
        return None
    edges: dict[str, dict[str, str]] = {}
    handoffs: dict[str, dict[str, str]] = {}
    for key, val in raw_nodes.items():
        if not isinstance(val, dict):
            continue
        nid = str(val.get("node_id") or key).strip()
        on = val.get("on") or val.get(True)  # YAML 1.1: bare `on` is parsed as True
        if isinstance(on, dict):
            edges[nid] = {
                str(k).strip(): str(v).strip()
                for k, v in on.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        ho = val.get("handoffs")
        if isinstance(ho, dict):
            handoffs[nid] = {
                str(k).strip(): str(v).strip()
                for k, v in ho.items()
                if isinstance(k, str) and isinstance(v, str)
            }
    entry = str(data.get("entry_node") or "").strip()
    if not entry:
        entry = next(iter(edges), "")
    return Workflow(
        id=str(data.get("id") or wid).strip(),
        name=str(data.get("name") or wid).strip(),
        entry_node=entry,
        edges=edges,
        handoffs=handoffs,
    )


def next_node(wf: Workflow, node_id: str, outcome: str) -> str:
    """根据 (node_id, outcome) 查 workflow flow edges，返回下一跳。空字符串表示终止。"""
    on = wf.edges.get(node_id, {})
    return on.get(outcome, "") or on.get("default", "")


def handoff_target(wf: Workflow, node_id: str, outcome: str) -> str:
    """根据 (node_id, outcome) 查 workflow handoffs，返回回调目标。空字符串表示无。"""
    ho = wf.handoffs.get(node_id, {})
    return ho.get(outcome, "")


def allowed_outcomes(wf: Workflow, node_id: str) -> list[str]:
    """返回 workflow 中此节点声明的全部 outcome 名称（flow + handoff）。"""
    on = wf.edges.get(node_id, {})
    ho = wf.handoffs.get(node_id, {})
    merged = {**on, **ho}
    return [k for k in merged if k != "default"]
