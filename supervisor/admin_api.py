from __future__ import annotations

import ast
import os
import shutil
import secrets
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class RawContent(BaseModel):
    content: str

class NodeCreate(BaseModel):
    id: str
    content: str

class WorkflowCreate(BaseModel):
    id: str
    content: str

class FragmentCreate(BaseModel):
    content: str


# ---------------------------------------------------------------------------
#  Admin Token 认证
# ---------------------------------------------------------------------------

_admin_token: str = ""


def get_admin_token() -> str:
    global _admin_token
    if _admin_token:
        return _admin_token
    token = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
        print(f"[admin] 自动生成管理 token (未设置 CLONOTH_ADMIN_TOKEN): {token}", flush=True)
    _admin_token = token
    return _admin_token


def verify_admin_token(request: Request) -> None:
    token = get_admin_token()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip() == token:
        return
    if request.query_params.get("token") == token:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def create_admin_router(workspace_root: Path) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    def _safe_path(base_dir: Path, rel_path: str, suffix: str = "") -> Path:
        name = rel_path if not suffix or rel_path.endswith(suffix) else rel_path + suffix
        p = (base_dir / name).resolve()
        if not str(p).startswith(str(base_dir.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
        return p

    def _read_yaml(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _normalize_workflow_nodes(raw_nodes: Any) -> dict[str, Any]:
        """YAML 1.1 parses bare `on` as boolean True. Normalize it back to string 'on'."""
        if not isinstance(raw_nodes, dict):
            return {}
        out: dict[str, Any] = {}
        for nid, val in raw_nodes.items():
            if not isinstance(val, dict):
                out[str(nid)] = val
                continue
            fixed: dict[str, Any] = {}
            for k, v in val.items():
                key = "on" if k is True else str(k)
                fixed[key] = v
            out[str(nid)] = fixed
        return out

    def _read_text(p: Path) -> dict[str, str]:
        if not p.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return {"content": p.read_text(encoding="utf-8")}

    def _write_text(p: Path, content: str) -> dict[str, Any]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True}

    def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text
        end = text.find("\n---\n", 4)
        if end < 0:
            return {}, text
        head = text[4:end]
        body = text[end + 5:]
        try:
            meta = yaml.safe_load(head) or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, body

    def _extract_tool_spec_ast(py_path: Path) -> tuple[dict[str, Any] | None, float | None]:
        try:
            text = py_path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(py_path))
        except Exception:
            return None, None
        vals: dict[str, Any] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in {"SPEC", "TIMEOUT_SEC"}:
                    try:
                        vals[tgt.id] = ast.literal_eval(node.value)
                    except Exception:
                        continue
        spec = vals.get("SPEC")
        timeout = float(vals["TIMEOUT_SEC"]) if isinstance(vals.get("TIMEOUT_SEC"), (int, float)) else None
        return (spec if isinstance(spec, dict) else None), timeout

    # ----- Nodes -----
    @router.get("/nodes")
    def list_nodes() -> list[dict[str, Any]]:
        nodes_dir = workspace_root / "config" / "nodes"
        if not nodes_dir.exists():
            return []
        res = []
        for f in nodes_dir.glob("*.yaml"):
            data = _read_yaml(f)
            ta_raw = data.get("tool_access", {})
            if isinstance(ta_raw, str):
                ta_raw = {"mode": ta_raw}
            elif not isinstance(ta_raw, dict):
                ta_raw = {"mode": "none"}
            res.append({
                "id": data.get("id", f.stem),
                "name": data.get("name", ""),
                "type": data.get("type", ""),
                "model_route": data.get("model_route", ""),
                "output_mode": data.get("output_mode", ""),
                "tool_access": ta_raw,
                "description": data.get("description", ""),
            })
        return res

    @router.get("/nodes/{node_id}/raw")
    def get_node_raw(node_id: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        return _read_text(p)

    @router.put("/nodes/{node_id}/raw")
    def update_node_raw(node_id: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        return _write_text(p, payload.content)

    @router.post("/nodes")
    def create_node(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", payload.id, ".yaml")
        if p.exists():
            raise HTTPException(status_code=409, detail="Node already exists")
        return _write_text(p, payload.content)

    @router.delete("/nodes/{node_id}")
    def delete_node(node_id: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        if p.exists():
            p.unlink()
        return {"ok": True}

    # ----- Workflows -----
    @router.get("/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        w_dir = workspace_root / "config" / "workflows"
        if not w_dir.exists():
            return []
        res = []
        for f in w_dir.glob("*.yaml"):
            data = _read_yaml(f)
            raw_nodes = data.get("nodes") or data.get(True) or {}
            res.append({
                "id": data.get("id", f.stem),
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "entry_node": data.get("entry_node", ""),
                "nodes": _normalize_workflow_nodes(raw_nodes),
                "ui_pos": data.get("ui_pos", {})
            })
        return res

    @router.get("/workflows/{workflow_id}/raw")
    def get_workflow_raw(workflow_id: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "config" / "workflows", workflow_id, ".yaml")
        return _read_text(p)

    @router.put("/workflows/{workflow_id}/raw")
    def update_workflow_raw(workflow_id: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "workflows", workflow_id, ".yaml")
        return _write_text(p, payload.content)

    @router.put("/workflows/{workflow_id}")
    def update_workflow_parsed(workflow_id: str, payload: dict = Body(...)) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "workflows", workflow_id, ".yaml")
        content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        return _write_text(p, content)

    @router.post("/workflows")
    def create_workflow(payload: WorkflowCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "workflows", payload.id, ".yaml")
        if p.exists():
            raise HTTPException(status_code=409, detail="Workflow already exists")
        return _write_text(p, payload.content)

    @router.delete("/workflows/{workflow_id}")
    def delete_workflow(workflow_id: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "workflows", workflow_id, ".yaml")
        if p.exists():
            p.unlink()
        return {"ok": True}

    # ----- Prompt Packs -----
    @router.get("/prompt-packs")
    def list_prompt_packs() -> list[dict[str, Any]]:
        packs_dir = workspace_root / "config" / "prompt_packs"
        if not packs_dir.exists():
            return []
        res = []
        for p in packs_dir.iterdir():
            if p.is_dir():
                mf = p / "manifest.yaml"
                if mf.exists():
                    data = _read_yaml(mf)
                    res.append({
                        "id": data.get("id", p.name),
                        "name": data.get("name", ""),
                        "description": data.get("description", "")
                    })
        return res

    @router.get("/prompt-packs/{pack_id}/manifest/raw")
    def get_prompt_pack_manifest(pack_id: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        return _read_text(mf)

    @router.put("/prompt-packs/{pack_id}/manifest/raw")
    def update_prompt_pack_manifest(pack_id: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        return _write_text(mf, payload.content)

    @router.get("/prompt-packs/{pack_id}/fragments")
    def list_fragments(pack_id: str) -> list[str]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        manifest = _read_yaml(mf)
        fragments_root = str(manifest.get("fragments_root", "fragments"))
        frag_dir = _safe_path(p, fragments_root)
        
        if not frag_dir.exists():
            return []
        res = []
        for f in frag_dir.rglob("*.md"):
            try:
                rel = f.relative_to(frag_dir).as_posix()
                res.append(rel)
            except Exception:
                pass
        return res

    @router.get("/prompt-packs/{pack_id}/fragments/{fragment_path:path}/raw")
    def get_fragment(pack_id: str, fragment_path: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        manifest = _read_yaml(mf)
        fragments_root = str(manifest.get("fragments_root", "fragments"))
        frag_dir = _safe_path(p, fragments_root)
        
        fp = _safe_path(frag_dir, fragment_path)
        return _read_text(fp)

    @router.put("/prompt-packs/{pack_id}/fragments/{fragment_path:path}/raw")
    def update_fragment(pack_id: str, fragment_path: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        manifest = _read_yaml(mf)
        fragments_root = str(manifest.get("fragments_root", "fragments"))
        frag_dir = _safe_path(p, fragments_root)
        
        fp = _safe_path(frag_dir, fragment_path)
        return _write_text(fp, payload.content)

    @router.post("/prompt-packs/{pack_id}/fragments/{fragment_path:path}/raw")
    def create_fragment(pack_id: str, fragment_path: str, payload: FragmentCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        manifest = _read_yaml(mf)
        fragments_root = str(manifest.get("fragments_root", "fragments"))
        frag_dir = _safe_path(p, fragments_root)
        
        fp = _safe_path(frag_dir, fragment_path)
        if fp.exists():
            raise HTTPException(status_code=409, detail="Fragment already exists")
        return _write_text(fp, payload.content)

    @router.delete("/prompt-packs/{pack_id}/fragments/{fragment_path:path}")
    def delete_fragment(pack_id: str, fragment_path: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "prompt_packs", pack_id)
        mf = p / "manifest.yaml"
        manifest = _read_yaml(mf)
        fragments_root = str(manifest.get("fragments_root", "fragments"))
        frag_dir = _safe_path(p, fragments_root)
        
        fp = _safe_path(frag_dir, fragment_path)
        if fp.exists():
            fp.unlink()
        return {"ok": True}

    # ----- Model Routing -----
    @router.get("/model-routing/raw")
    def get_model_routing() -> dict[str, str]:
        p = workspace_root / "config" / "model_routing.yaml"
        return _read_text(p)
        
    @router.put("/model-routing/raw")
    def update_model_routing(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "config" / "model_routing.yaml"
        return _write_text(p, payload.content)

    # ----- Runtime config -----
    @router.get("/runtime/raw")
    def get_runtime() -> dict[str, str]:
        p = workspace_root / "config" / "runtime.yaml"
        return _read_text(p)
        
    @router.put("/runtime/raw")
    def update_runtime(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "config" / "runtime.yaml"
        return _write_text(p, payload.content)

    # ----- Policy -----
    @router.get("/policy/raw")
    def get_policy() -> dict[str, str]:
        p = workspace_root / "data" / "policy.yaml"
        if not p.exists():
            p = workspace_root / "policy.example.yaml"
        return _read_text(p)
        
    @router.put("/policy/raw")
    def update_policy(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "policy.yaml"
        return _write_text(p, payload.content)

    # ----- Schedules -----
    @router.get("/schedules/raw")
    def get_schedules() -> dict[str, str]:
        p = workspace_root / "data" / "schedules.yaml"
        if not p.exists():
            return {"content": "schedules: []"}
        return _read_text(p)
        
    @router.put("/schedules/raw")
    def update_schedules(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "schedules.yaml"
        return _write_text(p, payload.content)

    # ----- Skills -----
    @router.get("/skills")
    def list_skills() -> list[dict[str, Any]]:
        skills_dir = workspace_root / "skills"
        if not skills_dir.exists():
            return []
        res = []
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
                meta, body = _parse_skill_frontmatter(text)
                res.append({
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "strategy": str(meta.get("strategy") or "normal"),
                    "keywords": [str(k) for k in (meta.get("keywords") or []) if isinstance(k, str)],
                    "body_preview": body.strip()[:200] if body else "",
                })
            except Exception:
                res.append({"name": skill_md.parent.name, "error": "parse failed"})
        return res

    @router.get("/skills/{name}/raw")
    def get_skill_raw(name: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "skills", name)
        md = p / "SKILL.md"
        return _read_text(md)

    @router.put("/skills/{name}/raw")
    def update_skill_raw(name: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", name)
        md = p / "SKILL.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        return _write_text(md, payload.content)

    @router.post("/skills")
    def create_skill(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", payload.id)
        md = p / "SKILL.md"
        if md.exists():
            raise HTTPException(status_code=409, detail="Skill already exists")
        md.parent.mkdir(parents=True, exist_ok=True)
        return _write_text(md, payload.content)

    @router.delete("/skills/{name}")
    def delete_skill(name: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", name)
        if p.exists() and p.is_dir():
            shutil.rmtree(p)
        return {"ok": True}

    # ----- Tools (external scripts) -----
    @router.get("/tools")
    def list_tools() -> list[dict[str, Any]]:
        tools_dir = workspace_root / "tools"
        if not tools_dir.exists():
            return []
        res = []
        for f in sorted(tools_dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            spec_data, timeout = _extract_tool_spec_ast(f)
            item: dict[str, Any] = {
                "name": f.stem,
                "file": f.name,
                "has_spec": spec_data is not None,
            }
            if spec_data:
                item["description"] = spec_data.get("description", "")
                item["input_schema"] = spec_data.get("input_schema", {})
            if timeout is not None:
                item["timeout_sec"] = timeout
            res.append(item)
        return res

    @router.get("/tools/{name}/raw")
    def get_tool_raw(name: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "tools", name, ".py")
        return _read_text(p)

    @router.put("/tools/{name}/raw")
    def update_tool_raw(name: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "tools", name, ".py")
        return _write_text(p, payload.content)

    @router.post("/tools")
    def create_tool(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "tools", payload.id, ".py")
        if p.exists():
            raise HTTPException(status_code=409, detail="Tool already exists")
        return _write_text(p, payload.content)

    @router.delete("/tools/{name}")
    def delete_tool(name: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "tools", name, ".py")
        if p.exists():
            p.unlink()
        return {"ok": True}

    # ----- MCP Clients -----
    @router.get("/mcp-clients")
    def list_mcp_clients() -> list[dict[str, Any]]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        if not p.exists():
            return []
        data = _read_yaml(p)
        clients = data.get("clients")
        if not isinstance(clients, dict):
            return []
        res = []
        for cid, spec in sorted(clients.items()):
            if not isinstance(spec, dict):
                continue
            item = {"id": str(cid)}
            item.update(spec)
            res.append(item)
        return res

    @router.get("/mcp-clients/raw")
    def get_mcp_clients_raw() -> dict[str, str]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        if not p.exists():
            return {"content": "version: 1\nclients: {}\n"}
        return _read_text(p)

    @router.put("/mcp-clients/raw")
    def update_mcp_clients_raw(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        return _write_text(p, payload.content)

    # ----- All tool names (builtin + external) -----
    @router.get("/all-tool-names")
    def all_tool_names() -> list[str]:
        from toolbox import meta_tools
        builtin = set(meta_tools._RESERVED_TOOL_NAMES)
        # Also include tools registered but not in _RESERVED (like cancel_active_tasks)
        extra_builtins = {'cancel_active_tasks'}
        names = builtin | extra_builtins
        # Scan external tools
        tools_dir = workspace_root / "tools"
        if tools_dir.exists():
            for f in tools_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                spec, _ = _extract_tool_spec_ast(f)
                if spec and isinstance(spec.get("name"), str):
                    names.add(spec["name"])
        return sorted(names)

    return router
