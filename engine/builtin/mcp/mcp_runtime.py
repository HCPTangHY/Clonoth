from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import json
import logging
import math
import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from clonoth_runtime import resolve_env_ref

logger = logging.getLogger(__name__)

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
_MCP_IMAGE_INLINE_DATA_THRESHOLD = 10000
_MCP_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
}


def _ensure_sdk() -> None:
    if _MCP_IMPORT_ERROR is not None:
        raise RuntimeError(f"MCP Python SDK not installed or failed to import: {_MCP_IMPORT_ERROR}")


def _config_path(workspace_root: Path) -> Path:
    return workspace_root / _CONFIG_REL_PATH


def _default_config() -> dict[str, Any]:
    return {"version": 1, "clients": {}}



_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable": "streamable_http",
    "streamable_http": "streamable_http",
    "streamablehttp": "streamable_http",
}


def normalize_transport(value: Any, *, default: str | None = "stdio") -> str:
    """Return the canonical transport name or raise on an unknown one.

    Why: a typo in a hand-edited config used to fall through to stdio, which
    then tried to spawn a process named after a URL. How: normalize the three
    accepted spellings and reject everything else; ``default=None`` makes the
    value mandatory (used by the mutation path). Purpose: an unknown transport
    is a configuration error, never a silent downgrade.
    """
    raw = None if value is None else str(value).strip()
    if not raw:
        if default is None:
            raise ValueError("transport is required (stdio, sse or streamable_http)")
        return default
    key = raw.lower().replace("-", "_")
    canonical = _TRANSPORT_ALIASES.get(key)
    if canonical is None:
        raise ValueError(
            f"unknown transport: {raw!r} (expected stdio, sse or streamable_http)"
        )
    return canonical


def _require_scalar_mapping(value: Any, field: str) -> dict[str, str]:
    """Validate one string->scalar mapping (env/headers) and stringify it."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object of scalar values")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, (dict, list, tuple, set)):
            raise ValueError(f"{field} value for {key!r} must be a scalar")
        cleaned[str(key)] = "" if item is None else str(item)
    return cleaned


def validate_client_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate one client spec and return the record to persist.

    Why: upsert used to coerce whatever the model sent (`args` as a string,
    `enabled` as "yes", an unknown transport as stdio), so one bad payload
    produced a config the runtime could not honour. How: validate every field
    here, before any approval request or file write, and return the exact
    record to store. Purpose: one validation point for the mutation path and
    for anything else that must decide whether a spec is usable.
    """
    if not isinstance(spec, dict):
        raise ValueError("MCP client spec must be an object")
    client_id = _normalize_client_id(spec.get("id"))
    if "transport" not in spec:
        raise ValueError("transport is required (stdio, sse or streamable_http)")
    transport = normalize_transport(spec.get("transport"), default=None)
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    record: dict[str, Any] = {"transport": transport, "enabled": enabled}
    description = spec.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        if description.strip():
            record["description"] = description.strip()

    if transport == "stdio":
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("stdio client requires a non-empty command")
        record["command"] = command.strip()
        args = spec.get("args")
        if args is None:
            args = []
        if not isinstance(args, list):
            raise ValueError("args must be a list of strings")
        record["args"] = [str(item) for item in args]
        record["env"] = _require_scalar_mapping(spec.get("env"), "env")
        if "stateful" in spec and spec["stateful"] is not None:
            if not isinstance(spec["stateful"], bool):
                raise ValueError("stateful must be a boolean")
            record["stateful"] = spec["stateful"]
        ttl = spec.get("idle_ttl_sec")
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
                raise ValueError("idle_ttl_sec must be a positive number of seconds")
            if not math.isfinite(float(ttl)):
                raise ValueError("idle_ttl_sec must be finite")
            if float(ttl) <= 0:
                raise ValueError("idle_ttl_sec must be a positive number of seconds")
            record["idle_ttl_sec"] = float(ttl)
    else:
        if "stateful" in spec or "idle_ttl_sec" in spec:
            raise ValueError("stateful/idle_ttl_sec only apply to stdio clients")
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"{transport} client requires a non-empty url")
        record["url"] = url.strip()
        record["headers"] = _require_scalar_mapping(spec.get("headers"), "headers")

    return {"id": client_id, **record}


def _normalize_client_id(client_id: Any) -> str:
    s = str(client_id or "").strip()
    if not s:
        raise ValueError("empty client id")
    if not _CLIENT_ID_RE.fullmatch(s):
        raise ValueError("invalid client id: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed")
    return s


def _parse_raw_config(text: str) -> dict[str, Any]:
    """Reject damaged documents instead of treating them as an empty store."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("MCP configuration contains invalid YAML") from exc
    if data is None:
        return _default_config()  # Production treats an empty document as empty config.
    if not isinstance(data, dict):
        raise ValueError("MCP configuration must be a mapping")

    raw_clients = data.get("clients")
    if raw_clients is None:
        raw_clients = {}
    clients: dict[str, Any] = {}
    if isinstance(raw_clients, dict):
        for cid, spec in raw_clients.items():
            if not isinstance(cid, str) or not cid.strip() or not isinstance(spec, dict):
                raise ValueError("MCP clients must map non-empty string ids to objects")
            clients[cid] = dict(spec)
    elif isinstance(raw_clients, list):
        # Keep the production reader's legacy list format, without silently
        # dropping malformed or duplicate records during a later mutation.
        for item in raw_clients:
            if not isinstance(item, dict):
                raise ValueError("Each MCP client must be an object")
            cid = item.get("id")
            if not isinstance(cid, str) or not cid.strip():
                raise ValueError("Each MCP client must have a non-empty string id")
            cid = cid.strip()
            if cid in clients:
                raise ValueError(f"Duplicate MCP client id: {cid}")
            copied = dict(item)
            copied.pop("id", None)
            clients[cid] = copied
    else:
        raise ValueError("MCP configuration clients must be a mapping or a list")
    try:
        version = int(data.get("version") or 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("MCP configuration version must be an integer") from exc
    return {**data, "version": version, "clients": clients}


def _load_raw_config(workspace_root: Path) -> dict[str, Any]:
    try:
        text = _config_path(workspace_root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default_config()
    return _parse_raw_config(text)


def save_raw_config_content(workspace_root: Path, text: str) -> None:
    """Validate and atomically replace a config, including admin-panel writes."""
    _parse_raw_config(text)
    p = _config_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=p.parent,
            prefix=f".{p.name}.", suffix=".tmp", delete=False,
        ) as stream:
            tmp = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), p.stat().st_mode & 0o777)
            except FileNotFoundError:
                pass  # New configs retain the temporary file's private mode.
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _save_raw_config(workspace_root: Path, cfg: dict[str, Any]) -> None:
    save_raw_config_content(
        workspace_root, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
    )


def _normalize_public_spec(client_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    transport = normalize_transport(spec.get("transport"))
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
        if "stateful" in spec:
            out["stateful"] = bool(spec.get("stateful"))
        if isinstance(spec.get("idle_ttl_sec"), (int, float)) and spec["idle_ttl_sec"] > 0:
            out["idle_ttl_sec"] = float(spec["idle_ttl_sec"])
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
        if not isinstance(spec, dict):
            continue
        try:
            out.append(_normalize_public_spec(client_id, spec))
        except ValueError as exc:
            # Why: one hand-edited broken record must not hide every other
            # client from the listing. How: keep the id and the reason, drop
            # the unusable fields. Purpose: the operator sees what to fix while
            # discovery skips the record (get_client_spec still raises).
            out.append({
                "id": client_id,
                "enabled": bool(spec.get("enabled", True)),
                "transport": str(spec.get("transport") or ""),
                "transport_error": str(exc),
            })
    return out


def _redact_url(url: str) -> tuple[str, bool, bool]:
    """Strip credentials and the query string from a remote MCP endpoint."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", False, False
    hidden_userinfo = bool(parts.username or parts.password)
    hidden_query = bool(parts.query)
    host = parts.hostname or ""
    try:
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        pass
    return urlunsplit((parts.scheme, host, parts.path, "", "")), hidden_query, hidden_userinfo


def redact_client_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Return the model-facing view of one client with every secret removed.

    Why: the CRUD tool's answer and listing text land in model context and in
    conversation history, so command paths, argv, env values, URL credentials,
    URL query strings and header values must not survive. How: keep the
    structural facts (transport, enabled, command basename, argument count,
    env/header key names) and replace values with ``***``. Purpose: an operator
    can still tell which client is broken without the model ever reading a
    credential. The config file keeps the real values for the runtime.
    """
    transport = str(spec.get("transport") or "")
    out: dict[str, Any] = {
        "id": str(spec.get("id") or ""),
        "enabled": bool(spec.get("enabled", True)),
        "transport": transport,
    }
    error = spec.get("transport_error")
    if error:
        out["transport_error"] = str(error)
        out["redacted"] = True
        return out
    description = spec.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()

    if transport == "stdio":
        command = str(spec.get("command") or "")
        out["command"] = Path(command).name or command
        args = spec.get("args")
        out["args_count"] = len(args) if isinstance(args, list) else 0
        env = spec.get("env")
        out["env"] = {str(key): "***" for key in env} if isinstance(env, dict) else {}
        if "stateful" in spec:
            out["stateful"] = bool(spec.get("stateful"))
        if "idle_ttl_sec" in spec:
            out["idle_ttl_sec"] = spec.get("idle_ttl_sec")
    else:
        url, hidden_query, hidden_userinfo = _redact_url(str(spec.get("url") or ""))
        out["url"] = url
        out["url_query_hidden"] = hidden_query
        out["url_userinfo_hidden"] = hidden_userinfo
        headers = spec.get("headers")
        out["headers"] = {str(key): "***" for key in headers} if isinstance(headers, dict) else {}
    out["redacted"] = True
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
    """Validate and persist one client record; returns its public shape.

    The whole record is validated before the config file is touched, so a bad
    payload never leaves a half-written or unusable entry behind.
    """
    validated = validate_client_spec(spec)
    cid = str(validated.pop("id"))
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients")
    if not isinstance(clients, dict):
        clients = {}
        cfg["clients"] = clients
    clients[cid] = validated
    _save_raw_config(workspace_root, cfg)
    return _normalize_public_spec(cid, validated)


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


def _field(obj: Any, *names: str) -> Any:
    """Read a field from SDK objects or already-plain dictionaries."""
    # Why: MCP SDK content parts may be Pydantic objects, dataclasses, or plain
    # dictionaries depending on SDK version and transport. How: try dict keys and
    # attributes without first converting the full result. Purpose: save large
    # image data before generic serialization can alter or drop SDK-specific
    # fields.
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _image_extension_from_mime(mime_type: Any) -> str:
    """Return a safe file extension for an MCP image MIME type."""
    # Why: saved MCP image attachments need stable names that downstream clients
    # can open. How: normalize MIME types and use a conservative allow-list.
    # Purpose: avoid writing ambiguous or path-like extensions from untrusted
    # tool output.
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    return _MCP_IMAGE_MIME_EXTENSIONS.get(mime, ".bin")


def _save_large_mcp_image_part(workspace_root: Path, client_id: str, part: Any, attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Save one large MCP image content part and return its text replacement."""
    # Why: some MCP servers return multi-megabyte base64 image content, which can
    # overflow conversation context when preserved inline. How: only intercept
    # image parts whose encoded data is above the configured threshold, decode
    # them, save them under data/attachments/mcp/{client_id}, and emit a short
    # text marker. Purpose: keep the context small while preserving the generated
    # image through the existing attachment collection pipeline.
    if _field(part, "type") != "image":
        return None

    data = _field(part, "data")
    if not isinstance(data, str) or len(data) <= _MCP_IMAGE_INLINE_DATA_THRESHOLD:
        return None

    try:
        image_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None

    safe_client_id = _normalize_client_id(client_id)
    ext = _image_extension_from_mime(_field(part, "mimeType", "mime_type", "mime"))
    filename = f"{hashlib.md5(image_bytes).hexdigest()[:12]}{ext}"
    attachment_dir = workspace_root / "data" / "attachments" / "mcp" / safe_client_id
    attachment_dir.mkdir(parents=True, exist_ok=True)
    attachment_path = attachment_dir / filename
    if not attachment_path.exists():
        attachment_path.write_bytes(image_bytes)

    relative_path = attachment_path.relative_to(workspace_root).as_posix()
    attachments.append({"path": relative_path, "name": filename, "type": "image"})
    return {
        "type": "text",
        "text": f"[Image saved to disk: {relative_path} ({len(image_bytes)} bytes)]",
    }


def _postprocess_mcp_content(workspace_root: Path, client_id: str, content: Any) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """Convert MCP content parts while spilling oversized image data to disk."""
    # Why: call_tool must return plain JSON-compatible data, but image spilling
    # must happen before the large base64 field is exposed to conversation
    # history. How: walk the original content sequence, replace only qualifying
    # image parts, and serialize every other part with _to_plain. Purpose: keep
    # small icons and non-image content unchanged while protecting context size.
    if not isinstance(content, (list, tuple)):
        return [], [], False

    attachments: list[dict[str, Any]] = []
    processed: list[Any] = []
    for part in content:
        replacement = _save_large_mcp_image_part(workspace_root, client_id, part, attachments)
        processed.append(replacement if replacement is not None else _to_plain(part))
    return processed, attachments, True


def _to_plain_mcp_result(workspace_root: Path, client_id: str, result: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Serialize an MCP tool result after replacing large image parts."""
    # Why: the generic _to_plain helper intentionally preserves arbitrary fields,
    # including huge image data. How: first inspect the SDK result's original
    # content list and save large image parts, then patch the serialized result's
    # content field with the processed list. Purpose: guarantee the final tool
    # result contains paths instead of large inline base64 payloads.
    processed_content, attachments, has_content = _postprocess_mcp_content(
        workspace_root,
        client_id,
        _field(result, "content"),
    )
    plain = _to_plain(result)

    if isinstance(plain, dict):
        if has_content:
            plain["content"] = processed_content
        elif isinstance(plain.get("content"), list):
            processed_content, attachments, _ = _postprocess_mcp_content(
                workspace_root,
                client_id,
                plain.get("content"),
            )
            plain["content"] = processed_content

    return plain, attachments


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
    annotations = raw.get("annotations")
    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        # Kept because tool purity must come from the server's own annotations
        # (readOnlyHint/destructiveHint), never from name matching.
        "annotations": annotations if isinstance(annotations, dict) else {},
    }


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
            _hdrs = {str(k): resolve_env_ref(v) for k, v in (spec.get("headers") or {}).items()}
            _sh_kw: dict[str, Any] = {}
            if _hdrs:
                import httpx as _httpx
                # The SDK closes only clients it creates itself. Own the supplied
                # client here, after transport/session cleanup, including errors.
                _sh_kw["http_client"] = await stack.enter_async_context(
                    _httpx.AsyncClient(headers=_hdrs)
                )
            transport_obj = await stack.enter_async_context(
                streamable_http_client(url, **_sh_kw)
            )
            if not isinstance(transport_obj, tuple) or len(transport_obj) < 2:
                raise RuntimeError("invalid streamable HTTP transport object")
            read_stream, write_stream = transport_obj[0], transport_obj[1]

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield session


# ---------------------------------------------------------------------------
#  Persistent stdio session actor pool (single-task lifecycle per client)
# ---------------------------------------------------------------------------

STDIO_IDLE_TTL_SEC = 900.0        # 普通 stdio MCP 默认空闲 15 分钟 (900s) 自动退出
STDIO_STATEFUL_IDLE_TTL_SEC = 1800.0  # spec 标记 stateful 的 stdio MCP 空闲 30 分钟 (1800s) 自动退出
_START_TIMEOUT_SEC = 45.0
_STOP_TIMEOUT_SEC = 5.0
# 每个请求最多一次“从未到达服务器”的重新派发（不是重放）。
_SESSION_ATTEMPTS = 2
_SESSION_CLOSED_MESSAGE = (
    "MCP session closed before the request completed; the call was not retried"
)


def resolve_idle_ttl(spec: dict[str, Any] | None) -> float:
    """Resolve the idle TTL (seconds) for one stdio client spec.

    Behaviour used to be decided by a hard-coded list of client names; now only
    the spec decides, so an operator can mark any server as stateful.
    """
    data = spec or {}
    raw_ttl = data.get("idle_ttl_sec")
    if isinstance(raw_ttl, (int, float)) and not isinstance(raw_ttl, bool) and raw_ttl > 0:
        return float(raw_ttl)
    return STDIO_STATEFUL_IDLE_TTL_SEC if bool(data.get("stateful")) else STDIO_IDLE_TTL_SEC


class MCPConnectionError(RuntimeError):
    """The MCP transport is gone while a request was in flight.

    Why: a business call must never be replayed, so a dead pipe has to be a
    distinguishable failure instead of a generic exception a caller might retry.
    """

    reason = "connection_lost"


class MCPConnectError(MCPConnectionError):
    """No live session could be obtained for the request (retired/failed start)."""

    reason = "connect_failed"


class _SessionReset(MCPConnectionError):
    """Internal: the request was still queued when the session ended.

    It never reached the server, so dispatching it again is not a replay.
    """

    reason = "session_reset"


def _consume_task(task: Any) -> None:
    """Retrieve a finished task's exception so asyncio stops warning about it."""
    if task is None or not getattr(task, "done", lambda: False)() or task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, EOFError)):
        return True
    name = type(exc).__name__
    return any(
        marker in name
        for marker in (
            "BrokenResource", "ClosedResource", "EndOfStream",
            "ConnectionReset", "ConnectionError", "BrokenPipe", "EOF",
        )
    )


def _map_sdk_error(exc: BaseException) -> BaseException:
    """Translate a session exception into the failure a caller may act on."""
    if isinstance(exc, MCPConnectionError):
        return exc
    if _is_transport_error(exc):
        return MCPConnectionError(
            f"MCP transport failed ({type(exc).__name__}: {exc}); the call was not retried"
        )
    # A business/tool error: reported once, and the session stays usable.
    return exc


class _Call:
    """One queued request plus the future its caller awaits."""

    __slots__ = ("op", "tool_name", "arguments", "future", "dispatched", "abandoned")

    def __init__(self, op: str, tool_name: str, arguments: dict[str, Any]) -> None:
        self.op = op
        self.tool_name = tool_name
        self.arguments = arguments
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.dispatched = False
        self.abandoned = False


class _StdioSessionActor:
    """Maintains a persistent stdio_client + ClientSession inside one dedicated asyncio.Task.

    Why: anyio cancel scopes inside mcp.client.stdio require entering and exiting
    on the exact same asyncio.Task, while stateful MCP servers need their child
    process to survive across consecutive tool calls. How: run the context
    manager inside one background worker task, dispatch list_tools/call_tool
    through an asyncio.Queue, and auto-exit after the idle TTL so idle servers
    do not permanently hold RAM in every worker.

    Cancellation / retry contract (the reason for this shape):

    * a call that is still queued when it is cancelled or times out is skipped
      by the worker and never reaches the server;
    * a call that is already in flight and gets cancelled or times out takes
      down only this client's session (this actor has no independent per-call
      server-side cancellation path) — every
      other client's actor is untouched, and requests that were never
      dispatched are re-dispatched on the fresh session instead of failing;
    * a request the server already received is never re-sent, whatever failed.
    """

    def __init__(self, spec_sig: str, command: str, args: list[str], env: dict[str, str], *, client_id: str = "", idle_ttl_sec: float = STDIO_IDLE_TTL_SEC) -> None:
        self.spec_sig = spec_sig
        self.client_id = client_id
        self.command = command
        self.args = args
        self.env = env
        self.idle_ttl_sec = idle_ttl_sec
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._active: _Call | None = None
        self._accepting = False
        self._start_lock = asyncio.Lock()
        self.retired = False

    def is_alive(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._queue is not None
            and self._accepting
        )

    async def _ensure_started(self) -> asyncio.Queue:
        async with self._start_lock:
            if self.retired:
                raise MCPConnectError("MCP session was removed after a configuration change")
            if self.is_alive():
                return self._queue  # type: ignore[return-value]
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            ready = loop.create_future()
            self._queue = q
            self._accepting = True
            task = loop.create_task(
                self._worker_loop(q, ready),
                name=f"mcp-stdio-session:{self.client_id or 'client'}",
            )
            self._task = task
            try:
                await asyncio.wait_for(ready, timeout=_START_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await self._discard_worker(task)
                raise MCPConnectError(
                    f"MCP session for '{self.client_id}' did not initialize within "
                    f"{_START_TIMEOUT_SEC:.0f}s; the call was not retried"
                ) from None
            except BaseException:
                await self._discard_worker(task)
                raise
            return q

    async def _discard_worker(self, task: asyncio.Task) -> None:
        """Cancel a worker that never became usable and forget its state."""
        if not task.done():
            task.cancel()
        await asyncio.wait({task}, timeout=_STOP_TIMEOUT_SEC)
        _consume_task(task)
        if self._task is task:
            self._task = None
            self._queue = None
            self._accepting = False

    async def _worker_loop(self, q: asyncio.Queue, ready: asyncio.Future) -> None:
        me = asyncio.current_task()
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    if not ready.done():
                        ready.set_result(True)
                    while True:
                        try:
                            call: _Call = await asyncio.wait_for(
                                q.get(), timeout=self.idle_ttl_sec
                            )
                        except asyncio.TimeoutError:
                            # Idle TTL reached: exit the context managers on this
                            # task so the child process is released.
                            break
                        if call.abandoned or call.future.done():
                            # Cancelled or timed out while it was still queued:
                            # never execute it.
                            continue
                        call.dispatched = True
                        self._active = call
                        try:
                            if call.op == "list":
                                res = await session.list_tools()
                            else:
                                res = await session.call_tool(
                                    call.tool_name, arguments=call.arguments or {}
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if not call.future.done():
                                call.future.set_exception(_map_sdk_error(exc))
                            if _is_transport_error(exc):
                                # The pipe is gone: leave the loop so the next
                                # request reconnects instead of queueing forever.
                                break
                        else:
                            if not call.future.done():
                                call.future.set_result(res)
                        # Only a completed request clears the active slot: a
                        # cancelled or failed one must stay visible to
                        # _fail_pending so its caller is released.
                        self._active = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(MCPConnectError(f"MCP session failed to initialize: {exc}"))
        finally:
            active, self._active = self._active, None
            self._accepting = False
            if self._queue is q:
                self._queue = None
            if self._task is me:
                self._task = None
            self._fail_pending(q, active)
            if not ready.done():
                ready.set_exception(MCPConnectError("MCP session closed during initialization"))

    def _fail_pending(self, q: asyncio.Queue, active: _Call | None) -> None:
        """Resolve every request still owned by a session that just ended."""
        pending: list[_Call] = []
        if active is not None:
            pending.append(active)
        while True:
            try:
                pending.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for call in pending:
            if call.future.done():
                continue
            if call.dispatched or self.retired:
                # The server may have seen it (or this client is gone for good):
                # one explicit failure, never a retry.
                call.future.set_exception(MCPConnectionError(_SESSION_CLOSED_MESSAGE))
            else:
                # Never dispatched, so the server cannot have seen it: handing it
                # to the next session is not a replay.
                call.future.set_exception(
                    _SessionReset("MCP session ended before the request was dispatched")
                )

    async def stop(self) -> None:
        """End this session without waiting behind its in-flight request."""
        self._accepting = False
        q, t = self._queue, self._task
        self._queue = None
        self._task = None
        if q is None or t is None or t.done():
            _consume_task(t)
            return
        t.cancel()
        try:
            await asyncio.wait({t}, timeout=_STOP_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise
        _consume_task(t)

    async def request(self, op: str, tool_name: str = "", arguments: dict[str, Any] | None = None, *, timeout: float = 120.0) -> Any:
        if self.retired:
            raise MCPConnectError("MCP session was removed after a configuration change")
        last: BaseException | None = None
        for attempt in range(_SESSION_ATTEMPTS):
            q = await self._ensure_started()
            call = _Call(op, tool_name, dict(arguments or {}))
            await q.put(call)
            try:
                return await asyncio.wait_for(call.future, timeout=timeout)
            except asyncio.TimeoutError:
                await self._abandon(call)
                raise
            except asyncio.CancelledError:
                await self._abandon(call)
                raise
            except _SessionReset as exc:
                last = exc
                if attempt + 1 < _SESSION_ATTEMPTS and not self.retired:
                    # Undispatched request only: re-dispatching it cannot duplicate
                    # a side effect, and it is what keeps a concurrent caller
                    # alive when another caller's cancellation dropped the
                    # session.
                    continue
                raise MCPConnectionError(_SESSION_CLOSED_MESSAGE) from None
            except MCPConnectionError as exc:
                last = exc
                if (
                    op == "list"
                    and not call.dispatched
                    and not self.retired
                    and attempt + 1 < _SESSION_ATTEMPTS
                ):
                    # Schema discovery is idempotent: a bounded retry is allowed
                    # here. A business call is never retried.
                    continue
                raise
        raise (last if isinstance(last, BaseException) else MCPConnectionError(
            _SESSION_CLOSED_MESSAGE
        ))

    async def _abandon(self, call: _Call) -> None:
        """Drop one request; touch the shared session only if it was in flight."""
        call.abandoned = True
        if not call.future.done():
            call.future.cancel()
        if not call.dispatched:
            # Still queued: the worker skips it and the session keeps serving
            # every other caller of this client.
            return
        # This actor has no independent per-call server-side cancellation path:
        # close this client's session rather than risk leaving a pending call
        # on it. Undispatched queued requests move to the fresh session.
        await self.stop()


def _runtime_state() -> dict[str, Any]:
    """Return loop-owned state, including across a complete MCP package reload.

    load_single_builtin() removes this runtime module as well as the plugin.
    Keeping state on the event loop lets both incarnations hand off the same
    bridge, actor pool and cache while still loading new runtime code. No loop
    means no writable runtime state; callers must not create a fallback pool.
    """
    loop = asyncio.get_running_loop()
    state = getattr(loop, "_clonoth_mcp_state", None)
    if state is None:
        state = {
            "bridge": {
                "task": None,
                "generation": 0,
                "registry": None,
                "hooks": None,
                "workspace_root": None,
                "disposers": {},  # client_id -> [(tool name, disposer)]
                "specs": {},      # client_id -> live registration spec
                "failed_clients": set(),
                "retry_pending": True,
                "actors": {},     # diagnostic view of the current actor pool
            },
            # Keep (loop_id, client_id) keys to preserve the existing pool API.
            "stdio_actors": {},
            "tool_schema_cache": {},
        }
        setattr(loop, "_clonoth_mcp_state", state)
    return state


def shared_state() -> dict[str, Any]:
    """Return this loop's bridge state with a refreshed actor diagnostic view."""
    runtime = _runtime_state()
    state = runtime["bridge"]
    loop_id = id(asyncio.get_running_loop())
    state["actors"] = {
        client_id: actor
        for (pool_loop, client_id), actor in list(runtime["stdio_actors"].items())
        if pool_loop == loop_id
    }
    return state


def _cache_get(client_id: str, spec_sig: str) -> list[dict[str, Any]] | None:
    entry = _runtime_state()["tool_schema_cache"].get((id(asyncio.get_running_loop()), client_id))
    if entry is None or entry[0] != spec_sig:
        return None
    return list(entry[1])


def _cache_set(client_id: str, spec_sig: str, tools: list[dict[str, Any]]) -> None:
    _runtime_state()["tool_schema_cache"][(id(asyncio.get_running_loop()), client_id)] = (spec_sig, tools)


def _cache_drop(client_id: str) -> None:
    _runtime_state()["tool_schema_cache"].pop((id(asyncio.get_running_loop()), client_id), None)


def _cache_clear_loop(loop_id: int) -> None:
    cache = _runtime_state()["tool_schema_cache"]
    for key in [key for key in cache if key[0] == loop_id]:
        cache.pop(key, None)


def _stdio_spec_signature(spec: dict[str, Any]) -> str:
    """Use the same process identity for acquisition and configuration cleanup."""
    return json.dumps({
        "c": str(spec.get("command") or "").strip(),
        "a": [str(x) for x in (spec.get("args") or [])],
        "e": {str(k): resolve_env_ref(v) for k, v in (spec.get("env") or {}).items()},
    }, sort_keys=True)


def _stdio_actor_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_clonoth_stdio_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, "_clonoth_stdio_lock", lock)
    return lock


async def reconcile_stdio_actors(clients: list[dict[str, Any]]) -> bool:
    """Retire obsolete sessions without holding the pool lock during shutdown."""
    active = {
        str(spec["id"]): spec for spec in clients
        if spec.get("enabled", True) and spec.get("transport") == "stdio"
    }
    loop_id = id(asyncio.get_running_loop())
    actors = _runtime_state()["stdio_actors"]
    retired: list[tuple[tuple[int, str], _StdioSessionActor]] = []
    complete = True
    async with _stdio_actor_lock():
        for key, actor in list(actors.items()):
            if key[0] != loop_id:
                continue
            cid = key[1]
            spec = active.get(cid)
            try:
                if spec is not None and not actor.retired and actor.spec_sig == _stdio_spec_signature(spec):
                    actor.idle_ttl_sec = resolve_idle_ttl(spec)
                    continue
            except Exception:
                complete = False
                logger.warning("MCP session configuration invalid: %s", cid, exc_info=True)
            actor.retired = True
            _cache_drop(cid)
            retired.append((key, actor))
    for key, actor in retired:
        try:
            await actor.stop()
        except Exception:
            # Keep the retired pool entry so the next refresh can retry cleanup.
            complete = False
            logger.warning("MCP session cleanup failed: %s", key[1], exc_info=True)
            continue
        async with _stdio_actor_lock():
            if actors.get(key) is actor:
                actors.pop(key, None)
    return complete


async def _get_stdio_actor(workspace_root: Path, client_id: str) -> _StdioSessionActor | None:
    _ensure_sdk()
    loop = asyncio.get_running_loop()
    key = (id(loop), client_id)
    actors = _runtime_state()["stdio_actors"]
    while True:
        async with _stdio_actor_lock():
            # Read under the pool lock, including after awaiting old-session
            # shutdown: a stale snapshot must not recreate a deleted client.
            spec = get_client_spec(workspace_root, client_id)
            if not bool(spec.get("enabled", True)):
                raise RuntimeError(f"MCP client is disabled: {client_id}")
            if str(spec.get("transport") or "stdio") != "stdio":
                return None
            command = str(spec.get("command") or "").strip()
            if not command:
                raise RuntimeError("stdio client missing command")
            sig = _stdio_spec_signature(spec)
            idle_ttl = resolve_idle_ttl(spec)
            existing = actors.get(key)
            if existing is not None:
                same_loop = getattr(existing, "_loop_ref", lambda: loop)() is loop
                if same_loop and not existing.retired and existing.spec_sig == sig:
                    existing.idle_ttl_sec = idle_ttl
                    return existing
                existing.retired = True
                _cache_drop(client_id)
                if not same_loop:
                    actors.pop(key, None)
                    continue
            else:
                args = [str(x) for x in (spec.get("args") or [])]
                env = os.environ.copy()
                for k, v in (spec.get("env") or {}).items():
                    env[str(k)] = resolve_env_ref(v)
                actor = _StdioSessionActor(
                    sig, command, args, env, client_id=client_id, idle_ttl_sec=idle_ttl
                )
                try:
                    import weakref as _wr
                    actor._loop_ref = _wr.ref(loop)  # type: ignore[attr-defined]
                except Exception:
                    pass
                actors[key] = actor
                return actor
        # Keep shutdown outside the shared lock so other clients remain usable.
        await existing.stop()
        async with _stdio_actor_lock():
            if actors.get(key) is existing:
                actors.pop(key, None)


async def close_stdio_actor(client_id: str) -> bool:
    """Retire one client's session; return False when it had none.

    Why: operators and tests need to drop one stuck server without touching the
    others. How: mark the pool entry retired (so it can never be restarted),
    stop it outside the pool lock, then remove it. Purpose: one client's
    cleanup cannot break another client's session.
    """
    key = (id(asyncio.get_running_loop()), client_id)
    actors = _runtime_state()["stdio_actors"]
    async with _stdio_actor_lock():
        actor = actors.get(key)
        if actor is None:
            return False
        actor.retired = True
        _cache_drop(client_id)
    try:
        await actor.stop()
    finally:
        async with _stdio_actor_lock():
            if actors.get(key) is actor:
                actors.pop(key, None)
    return True


async def close_stdio_actors() -> int:
    """Stop all active stdio actors in the current event loop and return count stopped."""
    loop_id = id(asyncio.get_running_loop())
    _cache_clear_loop(loop_id)
    actors = _runtime_state()["stdio_actors"]
    keys = [k for k in list(actors.keys()) if k[0] == loop_id]
    stopped = 0
    for k in keys:
        actor = actors.pop(k, None)
        if actor is not None:
            actor.retired = True
            try:
                await actor.stop()
                stopped += 1
            except Exception:
                pass
    return stopped


_LIST_TIMEOUT_SEC = 45.0


async def list_tools(workspace_root: Path, client_id: str) -> dict[str, Any]:
    """Discover one client's tools (idempotent, so a bounded retry is allowed)."""
    actor = await _get_stdio_actor(workspace_root, client_id)
    if actor is not None:
        cached = _cache_get(client_id, actor.spec_sig)
        if cached is not None:
            return {"ok": True, "client_id": client_id, "tools": cached}
        was_alive = actor.is_alive()
        try:
            res = await actor.request("list", timeout=_LIST_TIMEOUT_SEC)
        finally:
            if not was_alive:
                # Stop right after schema discovery so startup/reload does not
                # leave every MCP server resident in RAM before it is called.
                await actor.stop()
    else:
        async with open_session(workspace_root, client_id) as session:
            res = await session.list_tools()
    raw = _to_plain(res)
    tools = raw.get("tools") if isinstance(raw, dict) else []
    items = [_normalize_tool_entry(t) for t in tools] if isinstance(tools, list) else []
    if actor is not None and items:
        _cache_set(client_id, actor.spec_sig, items)
    return {"ok": True, "client_id": client_id, "tools": items}


MCP_CALL_TIMEOUT = 120.0


def _tool_failure(client_id: str, tool_name: str, reason: str, message: str) -> dict[str, Any]:
    """Build one explicit tool failure; the caller never retries it."""
    return {
        "ok": False,
        "error": message,
        "data": {
            "result": f"ERROR: {message}",
            "client_id": client_id,
            "tool_name": tool_name,
            "reason": reason,
            "attachments": [],
        },
        "attachments": [],
    }


async def call_tool(workspace_root: Path, client_id: str, tool_name: str, arguments: dict[str, Any] | None, *, timeout: float = MCP_CALL_TIMEOUT) -> dict[str, Any]:
    """Run one MCP tool call exactly once and report the outcome.

    Why: the previous implementation retried any exception, which re-executed
    business calls that had already reached the server (double charges, double
    writes). How: a business call is dispatched once; each failure class is
    mapped to a distinct ``reason`` so callers can tell a dead transport from a
    tool error and from a timeout. Purpose: one call, one result.
    """
    try:
        actor = await _get_stdio_actor(workspace_root, client_id)
        if actor is not None:
            res = await actor.request("call", tool_name, arguments or {}, timeout=timeout)
        else:
            async with open_session(workspace_root, client_id) as session:
                res = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments or {}),
                    timeout=timeout,
                )
    except asyncio.TimeoutError:
        return _tool_failure(
            client_id, tool_name, "timeout",
            f"MCP tool '{tool_name}' on client '{client_id}' timed out after {timeout}s; "
            "the call was not retried",
        )
    except MCPConnectError as exc:
        return _tool_failure(client_id, tool_name, "connect_failed", str(exc))
    except MCPConnectionError as exc:
        return _tool_failure(client_id, tool_name, "connection_lost", str(exc))
    except Exception as exc:
        # A business/tool error: reported once, and the session stays usable.
        return _tool_failure(client_id, tool_name, "tool_error", str(exc))

    result, attachments = _to_plain_mcp_result(workspace_root, client_id, res)
    result_text_parts: list[str] = []
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for part in result["content"]:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    result_text_parts.append(str(part.get("text", "")))
                elif part.get("type") == "image":
                    result_text_parts.append(f"[image: {part.get('mimeType') or part.get('mime_type') or part.get('mime') or '?'}]")
                elif part.get("type") == "resource":
                    resource = part.get("resource")
                    uri = part.get("uri") or (resource.get("uri") if isinstance(resource, dict) else None)
                    result_text_parts.append(f"[resource: {uri or '?'}]")
    result_text = "\n".join(result_text_parts) if result_text_parts else json.dumps(result, ensure_ascii=False, indent=2)
    is_error = bool(result.get("isError") or result.get("is_error")) if isinstance(result, dict) else False
    if not is_error and result_text.lstrip().startswith("ERROR:"):
        is_error = True
    return {
        "ok": not is_error,
        "data": {
            "result": result_text,
            "mcp_result": result,
            "client_id": client_id,
            "tool_name": tool_name,
            "attachments": attachments or [],
        },
        "error": result_text if is_error else None,
        "attachments": attachments or [],
    }
