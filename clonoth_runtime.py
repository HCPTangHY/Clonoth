from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "kernel": {
        "max_steps": 32,
        "history_limit": 80,
        "task_poll_interval_sec": 1.0,
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
        "executor": {
            "profile_id": "bootstrap.kernel_executor",
            "model": "",
        },
    },
    "providers": {
        "openai": {
            # HTTP timeout for OpenAI-compatible endpoint calls (seconds)
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
        "orchestrator": {
            "profile_id": "bootstrap.shell_orchestrator",
            "model": "",
        },
        "responder": {
            "profile_id": "bootstrap.task_responder",
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
        "upgrade_watchdog": {
            "poll_interval_sec": 0.5,
            "timeout_sec": 20.0,
            "max_attempts": 2,
        },
    },
}


# path(str) -> (mtime, cfg)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def runtime_config_path(workspace_root: Path) -> Path:
    return workspace_root / "config" / "runtime.yaml"


def _deep_update(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)  # type: ignore[index]
        else:
            dst[k] = v
    return dst


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    """Load workspace runtime config from `config/runtime.yaml`.

    - Safe defaults are always applied.
    - Invalid YAML falls back to defaults.
    - Uses a best-effort mtime cache.
    """

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
