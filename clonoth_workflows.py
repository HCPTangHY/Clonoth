from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import get_str


@dataclass
class WorkflowBinding:
    workflow_id: str
    route_profile_id: str
    executor_profile_id: str
    responder_profile_id: str
    raw: dict[str, Any] | None = None


def _load_yaml_dict(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def load_workflow(*, workspace_root: Path, workflow_id: str) -> dict[str, Any] | None:
    wid = (workflow_id or "").strip()
    if not wid:
        return None
    path = workspace_root / "config" / "workflows" / f"{wid}.yaml"
    data = _load_yaml_dict(path)
    if data is None:
        return None
    if str(data.get("kind") or "workflow").strip() != "workflow":
        return None
    return data


def resolve_default_workflow_id(*, runtime_cfg: dict[str, Any]) -> str:
    return get_str(runtime_cfg, "shell.workflow_id", "bootstrap.default_chat").strip() or "bootstrap.default_chat"


def _iter_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _find_step_agent_by_role(workflow: dict[str, Any], role: str) -> str:
    want = (role or "").strip().lower()
    if not want:
        return ""
    for step in _iter_steps(workflow):
        step_role = str(step.get("role") or "").strip().lower()
        agent = str(step.get("agent") or "").strip()
        if step_role == want and agent:
            return agent
    return ""


def _find_first_runtime_agent(workflow: dict[str, Any], runtime: str) -> str:
    want = (runtime or "").strip().lower()
    if not want:
        return ""
    for step in _iter_steps(workflow):
        step_runtime = str(step.get("runtime") or "").strip().lower()
        agent = str(step.get("agent") or "").strip()
        if step_runtime == want and agent:
            return agent
    return ""


def _find_last_shell_agent_after_kernel(workflow: dict[str, Any]) -> str:
    seen_kernel = False
    candidate = ""
    for step in _iter_steps(workflow):
        step_runtime = str(step.get("runtime") or "").strip().lower()
        agent = str(step.get("agent") or "").strip()
        if step_runtime == "kernel":
            seen_kernel = True
            continue
        if seen_kernel and step_runtime == "shell" and agent:
            candidate = agent
    return candidate


def resolve_workflow_binding(
    *,
    workspace_root: Path,
    runtime_cfg: dict[str, Any],
    workflow_id: str = "",
    task_context: dict[str, Any] | None = None,
    fallback_route_profile_id: str = "bootstrap.shell_orchestrator",
    fallback_executor_profile_id: str = "bootstrap.kernel_executor",
    fallback_responder_profile_id: str = "bootstrap.task_responder",
) -> WorkflowBinding:
    ctx = task_context if isinstance(task_context, dict) else {}
    default_wid = resolve_default_workflow_id(runtime_cfg=runtime_cfg)
    wid = (workflow_id or "").strip() or default_wid
    workflow = load_workflow(workspace_root=workspace_root, workflow_id=wid)
    if workflow is None and wid != default_wid:
        wid = default_wid
        workflow = load_workflow(workspace_root=workspace_root, workflow_id=wid)

    route_profile_id = str(ctx.get("route_profile_id") or "").strip()
    executor_profile_id = str(ctx.get("executor_profile_id") or "").strip()
    responder_profile_id = str(ctx.get("responder_profile_id") or "").strip()

    if isinstance(workflow, dict):
        if not route_profile_id:
            route_profile_id = _find_step_agent_by_role(workflow, "route")
        if not route_profile_id:
            entry = workflow.get("entrypoint")
            if isinstance(entry, dict):
                route_profile_id = str(entry.get("default_entry_agent") or "").strip()
        if not route_profile_id:
            route_profile_id = _find_first_runtime_agent(workflow, "shell")

        if not executor_profile_id:
            executor_profile_id = _find_step_agent_by_role(workflow, "execute")
        if not executor_profile_id:
            executor_profile_id = _find_first_runtime_agent(workflow, "kernel")

        if not responder_profile_id:
            responder_profile_id = _find_step_agent_by_role(workflow, "finalize")
        if not responder_profile_id:
            responder_profile_id = _find_last_shell_agent_after_kernel(workflow)

    return WorkflowBinding(
        workflow_id=wid,
        route_profile_id=route_profile_id or fallback_route_profile_id,
        executor_profile_id=executor_profile_id or fallback_executor_profile_id,
        responder_profile_id=responder_profile_id or fallback_responder_profile_id,
        raw=workflow,
    )
