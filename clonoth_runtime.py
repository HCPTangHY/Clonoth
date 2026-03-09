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
            "engine_workers": 2,
        },
    },
}


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def runtime_config_path(workspace_root: Path) -> Path:
    return workspace_root / "config" / "runtime.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    path = runtime_config_path(workspace_root)
    try:
        mtime = path.stat().st_mtime if path.exists() else -1.0
    except Exception:
        mtime = -1.0

    key = str(path.resolve())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return copy.deepcopy(cached[1])

    user_cfg: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                user_cfg = loaded
        except Exception:
            user_cfg = {}

    merged = _deep_merge(DEFAULT_RUNTIME_CONFIG, user_cfg)
    _CACHE[key] = (mtime, merged)
    return copy.deepcopy(merged)


def get_str(data: dict[str, Any], dotted_key: str, default: str = "") -> str:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return str(cur) if cur is not None else default


def get_int(data: dict[str, Any], dotted_key: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = get_str(data, dotted_key, "")
    try:
        val = int(raw)
    except Exception:
        val = int(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


def get_float(data: dict[str, Any], dotted_key: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = get_str(data, dotted_key, "")
    try:
        val = float(raw)
    except Exception:
        val = float(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


def get_bool(data: dict[str, Any], dotted_key: str, default: bool = False) -> bool:
    raw = get_str(data, dotted_key, "")
    if not raw:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_yaml_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_text_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def resolve_env_ref(text: str) -> str:
    s = str(text or "")
    if s.startswith("$ENV{") and s.endswith("}"):
        return os.getenv(s[5:-1], "")
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return os.getenv(s[2:-1], "")
    return s


async def fetch_openai_secret(http: httpx.AsyncClient, supervisor_url: str) -> dict[str, Any]:
    r = await http.get(f"{supervisor_url.rstrip('/')}/v1/config/openai/secret")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def normalize_openai_secret(data: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(data, dict):
        return "", "", ""
    api_key = resolve_env_ref(str(data.get("api_key") or "").strip())
    base_url = resolve_env_ref(str(data.get("base_url") or "").strip())
    model = resolve_env_ref(str(data.get("model") or "gpt-4o-mini").strip()) or "gpt-4o-mini"
    return api_key, base_url, model


def load_policy_config(workspace_root: Path) -> dict[str, Any]:
    p = workspace_root / "data" / "policy.yaml"
    data = load_yaml_dict(p)
    return data if isinstance(data, dict) else {}


def parse_extra_roots(workspace_root: Path, raw: Any) -> list[Path]:
    items = raw if isinstance(raw, list) else []
    out: list[Path] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        p = Path(item)
        p = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
        out.append(p)
    return out


def strip_tool_trace_blocks(text: str) -> str:
    s = str(text or "")
    start = "[CLONOTH_TOOL_TRACE v1]"
    end = "[/CLONOTH_TOOL_TRACE]"
    while True:
        i = s.find(start)
        if i < 0:
            break
        j = s.find(end, i)
        if j < 0:
            s = s[:i].rstrip()
            break
        s = (s[:i] + s[j + len(end):]).strip()
    return s.strip()
