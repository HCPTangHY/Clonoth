from __future__ import annotations

import dataclasses
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import resolve_env_ref

_MCP_IMPORT_ERROR: Exception | None = None

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client

    try:
        from mcp.client.streamable_http import streamable_http_client
    except Exception:
        from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
except Exception as e:  # pragma: no cover - depends on optional dependency at runtime
    _MCP_IMPORT_ERROR = e
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]


_CONFIG_REL_PATH = "data/mcp_clients.yaml"
_CLIENT_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _ensure_sdk() -> None:
    if _MCP_IMPORT_ERROR is not None:
        raise RuntimeError(f"MCP Python SDK 未安装或导入失败：{_MCP_IMPORT_ERROR}")


def _config_path(workspace_root: Path) -> Path:
    return workspace_root / _CONFIG_REL_PATH


def _default_config() -> dict[str, Any]:
    return {"version": 1, "clients": {}}


def _normalize_transport(v: Any) -> str:
    s = str(v or "stdio").strip().lower().replace("-", "_")
    if s in {"streamablehttp", "streamable_http", "http", "streamable"}:
        return "streamable_http"
    if s in {"sse"}:
        return "sse"
    return "stdio"


def _normalize_client_id(client_id: Any) -> str:
    s = str(client_id or "").strip()
    if not s:
        raise ValueError("empty client id")
    if not _CLIENT_ID_RE.fullmatch(s):
        raise ValueError("invalid client id: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed")
    return s


def _load_raw_config(workspace_root: Path) -> dict[str, Any]:
    p = _config_path(workspace_root)
    if not p.exists():
        return _default_config()

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return _default_config()

    if not isinstance(data, dict):
        return _default_config()

    raw_clients = data.get("clients")
    clients: dict[str, Any] = {}

    if isinstance(raw_clients, dict):
        for cid, spec in raw_clients.items():
            if isinstance(cid, str) and isinstance(spec, dict):
                clients[cid] = dict(spec)
    elif isinstance(raw_clients, list):
        for item in raw_clients:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if isinstance(cid, str) and cid.strip():
                copied = dict(item)
                copied.pop("id", None)
                clients[cid.strip()] = copied

    return {
        "version": int(data.get("version") or 1),
        "clients": clients,
    }


def _save_raw_config(workspace_root: Path, cfg: dict[str, Any]) -> None:
    p = _config_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    p.write_text(text, encoding="utf-8")


def _normalize_public_spec(client_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    transport = _normalize_transport(spec.get("transport"))
    out: dict[str, Any] = {
        "id": client_id,
        "enabled": bool(spec.get("enabled", True)),
        "transport": transport,
    }

    description = spec.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()

    if transport == "stdio":
        out["command"] = str(spec.get("command") or "")
        args = spec.get("args")
        out["args"] = [str(x) for x in args] if isinstance(args, list) else []
        env = spec.get("env")
        out["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    else:
        out["url"] = str(spec.get("url") or "")
        headers = spec.get("headers")
        out["headers"] = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}

    return out


def list_clients(workspace_root: Path) -> list[dict[str, Any]]:
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients") or {}
    if not isinstance(clients, dict):
        return []

    out: list[dict[str, Any]] = []
    for client_id in sorted(clients.keys()):
        spec = clients.get(client_id)
        if isinstance(spec, dict):
            out.append(_normalize_public_spec(client_id, spec))
    return out


def get_client_spec(workspace_root: Path, client_id: str) -> dict[str, Any]:
    cid = _normalize_client_id(client_id)
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients") or {}
    if not isinstance(clients, dict):
        raise ValueError(f"client not found: {cid}")
    spec = clients.get(cid)
    if not isinstance(spec, dict):
        raise ValueError(f"client not found: {cid}")
    return _normalize_public_spec(cid, spec)


def upsert_client(workspace_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    cid = _normalize_client_id(spec.get("id"))
    transport = _normalize_transport(spec.get("transport"))
    enabled = bool(spec.get("enabled", True))
    description = str(spec.get("description") or "").strip()

    record: dict[str, Any] = {
        "transport": transport,
        "enabled": enabled,
    }
    if description:
        record["description"] = description

    if transport == "stdio":
        command = str(spec.get("command") or "").strip()
        if not command:
            raise ValueError("stdio client requires non-empty command")
        args = spec.get("args")
        env = spec.get("env")
        record["command"] = command
        record["args"] = [str(x) for x in args] if isinstance(args, list) else []
        record["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    else:
        url = str(spec.get("url") or "").strip()
        if not url:
            raise ValueError(f"{transport} client requires non-empty url")
        headers = spec.get("headers")
        record["url"] = url
        record["headers"] = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}

    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients")
    if not isinstance(clients, dict):
        clients = {}
        cfg["clients"] = clients
    clients[cid] = record
    _save_raw_config(workspace_root, cfg)
    return _normalize_public_spec(cid, record)


def delete_client(workspace_root: Path, client_id: str) -> bool:
    cid = _normalize_client_id(client_id)
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients")
    if not isinstance(clients, dict) or cid not in clients:
        return False
    clients.pop(cid, None)
    _save_raw_config(workspace_root, cfg)
    return True


def _to_plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_plain(v) for v in obj]
    if dataclasses.is_dataclass(obj):
        return _to_plain(dataclasses.asdict(obj))
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_plain(model_dump(mode="json"))
        except TypeError:
            return _to_plain(model_dump())
        except Exception:
            pass
    dict_method = getattr(obj, "dict", None)
    if callable(dict_method):
        try:
            return _to_plain(dict_method())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return _to_plain(vars(obj))
        except Exception:
            pass
    return str(obj)


def _normalize_tool_entry(tool: Any) -> dict[str, Any]:
    raw = _to_plain(tool)
    if not isinstance(raw, dict):
        return {"name": str(raw), "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}

    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    input_schema = raw.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = raw.get("input_schema")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}
    return {"name": name, "description": description, "input_schema": input_schema}


@asynccontextmanager
async def open_session(workspace_root: Path, client_id: str):
    _ensure_sdk()
    spec = get_client_spec(workspace_root, client_id)
    if not bool(spec.get("enabled", True)):
        raise RuntimeError(f"MCP client is disabled: {client_id}")

    transport = str(spec.get("transport") or "stdio")

    async with AsyncExitStack() as stack:
        if transport == "stdio":
            command = str(spec.get("command") or "").strip()
            if not command:
                raise RuntimeError("stdio client missing command")
            args = spec.get("args") or []
            env = os.environ.copy()
            for k, v in (spec.get("env") or {}).items():
                env[str(k)] = resolve_env_ref(v)
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=command, args=[str(x) for x in args], env=env))
            )
        elif transport == "sse":
            url = str(spec.get("url") or "").strip()
            if not url:
                raise RuntimeError("sse client missing url")
            transport_obj = await stack.enter_async_context(
                sse_client(url, headers={str(k): resolve_env_ref(v) for k, v in (spec.get("headers") or {}).items()})
            )
            if not isinstance(transport_obj, tuple) or len(transport_obj) < 2:
                raise RuntimeError("invalid SSE transport object")
            read_stream, write_stream = transport_obj[0], transport_obj[1]
        else:
            url = str(spec.get("url") or "").strip()
            if not url:
                raise RuntimeError("streamable_http client missing url")
            transport_obj = await stack.enter_async_context(
                streamable_http_client(url, headers={str(k): resolve_env_ref(v) for k, v in (spec.get("headers") or {}).items()})
            )
            if not isinstance(transport_obj, tuple) or len(transport_obj) < 2:
                raise RuntimeError("invalid streamable HTTP transport object")
            read_stream, write_stream = transport_obj[0], transport_obj[1]

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield session


async def list_tools(workspace_root: Path, client_id: str) -> dict[str, Any]:
    async with open_session(workspace_root, client_id) as session:
        res = await session.list_tools()
        raw = _to_plain(res)
        tools = raw.get("tools") if isinstance(raw, dict) else []
        items = [_normalize_tool_entry(t) for t in tools] if isinstance(tools, list) else []
        return {"ok": True, "client_id": client_id, "tools": items}


async def call_tool(workspace_root: Path, client_id: str, tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    async with open_session(workspace_root, client_id) as session:
        res = await session.call_tool(tool_name, arguments=arguments or {})
        return {"ok": True, "client_id": client_id, "tool_name": tool_name, "result": _to_plain(res)}
