from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import httpx
import yaml


DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "kernel": {
        "max_steps": 32,
        "history_limit": 80,
        "poll_interval_sec": 1.0,
        "approval_poll_interval_sec": 0.5,
        "http": {"client_timeout_sec": 60.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "tool_trace": {
            "max_inline_chars": 8000,
            "max_progress_arg_chars": 320,
        },
        "model": "",
    },
    "providers": {
        "openai": {
            "timeout_sec": 60.0,
        }
    },
    "meta": {
        "execute_command": {
            "default_timeout_sec": 90.0,
            "max_output_chars": 12000,
        },
        "git": {"diff_max_chars": 600000},
        "search": {"max_file_size_bytes": 3000000, "max_matches": 100},
    },
    "tools": {
        "command": {
            "default_timeout_sec": 60.0,
        }
    },
    "shell": {
        "default_conversation_key": "cli:default",
        "workflow_id": "bootstrap.default_chat",
        "entry_node": {
            "history_limit": 40,
            "model": "",
        },
        "reply_node": {
            "tool_trace_history_limit": 200,
            "model": "",
        },
        "http": {"client_timeout_sec": 10.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "events_poll_interval_sec": 0.5,
    },
    "supervisor": {
        "process_manager": {
            "stop_wait_timeout_sec": 5.0,
            "shell_new_console": None,
        },
    },
}


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def runtime_config_path(workspace_root: Path) -> Path:
    return workspace_root / "config" / "runtime.yaml"


def policy_config_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "policy.yaml"


def load_yaml_dict(path: Path) -> dict[str, Any] | None:
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


def load_policy_config(workspace_root: Path) -> dict[str, Any] | None:
    return load_yaml_dict(policy_config_path(workspace_root))


def parse_extra_roots(workspace_root: Path, raw: Any) -> list[Path]:
    roots: list[Path] = []
    if not isinstance(raw, list):
        return roots

    for it in raw:
        if not isinstance(it, str) or not it.strip():
            continue
        try:
            p = Path(it.strip()).expanduser()
            if not p.is_absolute():
                continue
            rp = p.resolve()
        except Exception:
            continue

        if rp == workspace_root:
            continue
        if rp not in roots:
            roots.append(rp)
    return roots


def load_text_file(path: Path, max_chars: int = 200_000) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return ""
        if len(text) > max_chars:
            return text[:max_chars] + "\n...<truncated>"
        return text
    except Exception:
        return ""


def _deep_update(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)  # type: ignore[index]
        else:
            dst[k] = v
    return dst


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    p = runtime_config_path(workspace_root)

    try:
        mtime = float(p.stat().st_mtime)
    except Exception:
        return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)

    cache_key = str(p.resolve())
    cached = _CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else None
    except Exception:
        data = None

    cfg = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    if isinstance(data, dict):
        try:
            _deep_update(cfg, data)
        except Exception:
            cfg = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)

    _CACHE[cache_key] = (mtime, cfg)
    return cfg


def get_value(cfg: dict[str, Any], key_path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in (key_path or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur.get(part)
    return cur


def get_int(
    cfg: dict[str, Any],
    key_path: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    v = get_value(cfg, key_path, default)
    try:
        out = int(v)
    except Exception:
        out = int(default)

    if min_value is not None and out < min_value:
        out = min_value
    if max_value is not None and out > max_value:
        out = max_value
    return out


def get_float(
    cfg: dict[str, Any],
    key_path: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    v = get_value(cfg, key_path, default)
    try:
        out = float(v)
    except Exception:
        out = float(default)

    if min_value is not None and out < min_value:
        out = min_value
    if max_value is not None and out > max_value:
        out = max_value
    return out


def get_bool(cfg: dict[str, Any], key_path: str, default: bool | None) -> bool | None:
    v = get_value(cfg, key_path, default)

    if isinstance(v, bool):
        return v

    if v is None:
        return default

    if isinstance(v, (int, float)):
        return bool(v)

    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False

    return default


def get_str(cfg: dict[str, Any], key_path: str, default: str) -> str:
    v = get_value(cfg, key_path, default)
    if isinstance(v, str):
        return v
    if v is None:
        return default
    return str(v)


def resolve_env_ref(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return (os.getenv(s[2:-1]) or "").strip()
    return s


async def fetch_json_dict(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def fetch_openai_secret(client: httpx.AsyncClient, supervisor_url: str) -> dict[str, Any]:
    return await fetch_json_dict(client, f"{supervisor_url}/v1/config/openai/secret")


def normalize_openai_secret(
    cfg: dict[str, Any],
) -> tuple[str, str, str]:
    openai_cfg = cfg.get("openai") if isinstance(cfg, dict) else None
    if isinstance(openai_cfg, dict):
        base_url = resolve_env_ref(openai_cfg.get("base_url") or "")
        api_key = resolve_env_ref(openai_cfg.get("api_key") or "")
        model = resolve_env_ref(openai_cfg.get("model") or "")
    else:
        base_url = resolve_env_ref(cfg.get("base_url") or "") if isinstance(cfg, dict) else ""
        api_key = resolve_env_ref(cfg.get("api_key") or "") if isinstance(cfg, dict) else ""
        model = resolve_env_ref(cfg.get("model") or "") if isinstance(cfg, dict) else ""

    if not base_url:
        base_url = "https://api.openai.com/v1"
    if not model:
        model = "gpt-4o-mini"
    return api_key, base_url, model


import re as _re

_TOOL_TRACE_RE = _re.compile(
    r"\[CLONOTH_TOOL_TRACE[^\]]*\].*?\[/CLONOTH_TOOL_TRACE\]",
    _re.DOTALL,
)


def strip_tool_trace_blocks(text: str) -> str:
    """Remove [CLONOTH_TOOL_TRACE ...] blocks from text for display."""
    if not text:
        return text
    cleaned = _TOOL_TRACE_RE.sub("", text).strip()
    return cleaned
