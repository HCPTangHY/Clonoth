from __future__ import annotations

import ast
import hmac
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

class FragmentCreate(BaseModel):
    content: str


# ---------------------------------------------------------------------------
#  Admin Token 认证 + Web JWT 双模验证
# ---------------------------------------------------------------------------

_admin_token: str = ""
_web_auth: "WebAuthManager | None" = None


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


def get_web_auth() -> "WebAuthManager | None":
    return _web_auth


def init_web_auth(data_dir: Path) -> None:
    """Initialize the WebAuthManager. Called once at app setup."""
    global _web_auth
    from .web_auth import WebAuthManager
    _web_auth = WebAuthManager(data_dir)
    if _web_auth.available:
        if _web_auth.setup_completed:
            print("[admin] Web JWT 认证已启用 (web_auth.json)", flush=True)
        else:
            print("[admin] Web JWT 认证待初始化，请访问 /admin/ 完成设置", flush=True)
    else:
        print("[admin] Web JWT 不可用 (缺少 bcrypt/PyJWT)，仅使用 admin token", flush=True)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def verify_admin_token(
    request: Request,
    *,
    allow_jwt: bool = True,
    allow_query: bool = False,
) -> str:
    """Dual-mode auth: try JWT first (if allowed), then fall back to machine admin token.

    Returns ``"jwt"`` or ``"machine"``.
    """
    auth = request.headers.get("Authorization", "")
    bearer_value = auth[7:].strip() if auth.startswith("Bearer ") else ""

    # 1) Try JWT if web_auth is configured and allowed for this endpoint
    if allow_jwt:
        wa = get_web_auth()
        if wa and wa.available and wa.setup_completed and bearer_value:
            payload = wa.verify_jwt(bearer_value)
            if payload is not None:
                request.state.auth_kind = "jwt"
                request.state.auth_user = str(payload.get("sub") or "")
                return "jwt"

    # 2) Fall back to machine admin token (constant-time comparison)
    token = get_admin_token()
    if bearer_value and hmac.compare_digest(bearer_value, token):
        request.state.auth_kind = "machine"
        return "machine"
    if allow_query:
        qtok = request.query_params.get("token", "")
        if qtok and hmac.compare_digest(qtok, token):
            request.state.auth_kind = "machine"
            return "machine"

    raise HTTPException(status_code=401, detail="Unauthorized")


def verify_machine_admin_token(request: Request) -> str:
    """Require machine-level CLONOTH_ADMIN_TOKEN (rejects Web JWT unless explicitly enabled)."""
    allow_jwt = _env_truthy("CLONOTH_ALLOW_JWT_HIGH_RISK")
    return verify_admin_token(request, allow_jwt=allow_jwt, allow_query=False)


def _require_http_feature(env_name: str, action_desc: str) -> None:
    if not _env_truthy(env_name):
        raise HTTPException(
            status_code=403,
            detail=f"Forbidden via HTTP API: {action_desc} is disabled by default (set {env_name}=1 to override)",
        )


def _extract_script_schedules(raw_text: str) -> list[dict[str, Any]]:
    data = yaml.safe_load(raw_text) if raw_text.strip() else {}
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError("schedules.yaml root must be a mapping")
    items = data.get("schedules")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("'schedules' must be a list")
    scripts: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each schedule entry must be a mapping")
        stype = str(item.get("type") or "message").strip().lower()
        has_cmd = bool(str(item.get("command") or "").strip())
        if stype == "script" or has_cmd:
            scripts.append(dict(item))
    return scripts


def _validate_schedules_raw_update(workspace_root: Path, new_content: str) -> None:
    import base64
    import re
    from .policy import PolicyEngine, SafetyLevel

    try:
        new_scripts = _extract_script_schedules(new_content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedules YAML: {exc}") from exc

    if not new_scripts:
        return

    policy = PolicyEngine(workspace_root=workspace_root)
    _b64_pipe_pat = re.compile(
        r"(?:base64\s+(?:-d|--decode)|xxd\s+-r).*?\|\s*(?:ba|da|z|k|a)?sh\b|\|\s*(?:ba|da|z|k|a)?sh\b",
        re.IGNORECASE,
    )
    _b64_tok_pat = re.compile(r"[A-Za-z0-9+/]{8,}={0,2}")

    for item in new_scripts:
        cmd = str(item.get("command") or "").strip()
        if not cmd:
            continue
        decision = policy.evaluate_execute_command(command=cmd)
        if decision.safety_level == SafetyLevel.deny:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden schedule command ({decision.reason})",
            )
        # Also inspect decoded base64 fragments so encoded blacklist commands cannot bypass PolicyEngine
        for tok in _b64_tok_pat.findall(cmd):
            try:
                raw = base64.b64decode(tok, validate=True)
                decoded = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            if len(decoded) >= 4:
                dec_res = policy.evaluate_execute_command(command=decoded)
                if dec_res.safety_level == SafetyLevel.deny:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Forbidden encoded schedule command ({dec_res.reason})",
                    )
        if "base64" in cmd.lower() and _b64_pipe_pat.search(cmd):
            raise HTTPException(
                status_code=403,
                detail="Forbidden schedule command (obfuscated base64 pipe to shell is blocked)",
            )


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

    def _read_text(p: Path) -> dict[str, str]:
        if not p.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return {"content": p.read_text(encoding="utf-8")}

    def _write_text(p: Path, content: str) -> dict[str, Any]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True}

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
    def _node_row(nid: str, data: dict[str, Any], source: str) -> dict[str, Any]:
        ta_raw = data.get("tool_access", {})
        if isinstance(ta_raw, str):
            ta_raw = {"mode": ta_raw}
        elif not isinstance(ta_raw, dict):
            ta_raw = {"mode": "none"}
        return {
            "id": nid,
            "name": data.get("name", ""),
            "type": data.get("type", ""),
            "model": data.get("model", ""),
            "provider": data.get("provider", ""),
            "base_url": data.get("base_url", ""),
            "tool_access": ta_raw,
            "skills": data.get("skills", {}),
            "description": data.get("description", ""),
            "delegate_targets": list(data.get("delegate_targets") or []),
            "source": source,
        }

    @router.get("/nodes")
    def list_nodes() -> list[dict[str, Any]]:
        # 扫描 config/nodes/ 目录，插件声明节点优先级最高（Paradox 式覆盖），
        # source 标记为 "plugin"（附带声明者插件名，便于 UI 区分）。
        from engine.faces.nodes import iter_declared_nodes
        # [2026-09-13] Why: _worker 节点从插件声明改为 config/nodes 物理文件，
        # 设置页需要它总是可见可编辑。How: 列出前确保默认文件已落盘。
        # Purpose: 前端可直接修改临时子节点的 provider/model。
        try:
            from engine.builtin.agent_manage import ensure_worker_node_yaml
            ensure_worker_node_yaml(workspace_root)
        except Exception:
            pass
        dirs = [
            (workspace_root / "config" / "nodes", "user"),
        ]
        res = []
        seen_ids: set[str] = set()
        for entry in iter_declared_nodes():
            nid = entry["id"]
            if nid in seen_ids:
                continue
            seen_ids.add(nid)
            row = _node_row(nid, entry["data"], "plugin")
            row["plugin"] = entry["owner"]
            res.append(row)
        for nodes_dir, source in dirs:
            if not nodes_dir.exists():
                continue
            for f in nodes_dir.glob("*.yaml"):
                data = _read_yaml(f)
                nid = data.get("id", f.stem)
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                res.append(_node_row(nid, data, source))
        return res

    def _resolve_node_path(node_id: str) -> Path:
        """Resolve the file path for a node.

        [AutoC 2026-09-02] Plugin-declared nodes resolve to their declaration
        file inside the plugin package (e.g. engine/builtin/nodes/*.yaml);
        otherwise fall back to config/nodes. Why first: plugin declarations are
        the authoritative source and the only place whose edit takes effect
        (on plugin reload).
        """
        from engine.faces.nodes import declared_node_path
        plugin_path = declared_node_path(node_id)
        if plugin_path:
            pp = Path(plugin_path)
            if pp.is_file():
                return pp
        return _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")

    @router.get("/nodes/{node_id}/raw")
    def get_node_raw(node_id: str) -> dict[str, str]:
        p = _resolve_node_path(node_id)
        return _read_text(p)

    @router.put("/nodes/{node_id}/raw")
    def update_node_raw(node_id: str, payload: RawContent) -> dict[str, Any]:
        p = _resolve_node_path(node_id)
        return _write_text(p, payload.content)

    @router.post("/nodes")
    def create_node(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", payload.id, ".yaml")
        if p.exists():
            raise HTTPException(status_code=409, detail="Node already exists")
        return _write_text(p, payload.content)

    @router.delete("/nodes/{node_id}")
    def delete_node(node_id: str) -> dict[str, Any]:
        # 插件声明节点不受此端点管理：删 config/nodes 下的文件不会影响声明，
        # 静默成功会误导。卸载或修改归属插件才是正确操作。
        from engine.faces.nodes import declared_node
        if declared_node(node_id) is not None:
            raise HTTPException(
                status_code=400,
                detail="node is declared by a plugin; manage it via the owning plugin",
            )
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        if p.exists():
            p.unlink()
        return {"ok": True}

    # ----- Runtime config -----
    # ----- Config (data/config.yaml) -----
    @router.get("/config/raw")
    def get_config_raw() -> dict[str, str]:
        p = workspace_root / "data" / "config.yaml"
        return _read_text(p)

    @router.put("/config/raw")
    def update_config_raw(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "config.yaml"
        return _write_text(p, payload.content)

    # ----- Runtime -----
    @router.get("/runtime/raw")
    def get_runtime() -> dict[str, str]:
        p = workspace_root / "config" / "runtime.yaml"
        return _read_text(p)
        
    @router.put("/runtime/raw")
    def update_runtime(payload: RawContent) -> dict[str, Any]:
        from .runtime_config import write_raw
        write_raw(workspace_root, payload.content)
        return {"ok": True}

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
        _validate_schedules_raw_update(workspace_root, payload.content)
        return _write_text(p, payload.content)

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
    # [AutoC 2026-08-27] 三个 mcp-clients 端点迁移到 engine/builtin/mcp 插件
    # （mount="admin/config"，路径与响应形状不变），此处不再持有 MCP 专属代码。

    # ----- All tool names (builtin + external) -----
    @router.get("/all-tool-names")
    def all_tool_names() -> list[str]:
        from toolbox.builtins import RESERVED_TOOL_NAMES
        builtin = set(RESERVED_TOOL_NAMES)
        # Also include tools registered but not in _RESERVED
        extra_builtins: set[str] = set()
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
