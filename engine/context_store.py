from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


_BASE_DIR = Path("data") / "node_contexts"


def new_context_id() -> str:
    return uuid.uuid4().hex


def _base_dir(workspace_root: Path) -> Path:
    return workspace_root / _BASE_DIR


def _resolve_ref(workspace_root: Path, context_ref: str) -> Path:
    p = (workspace_root / str(context_ref or "")).resolve()
    base = _base_dir(workspace_root).resolve()
    try:
        p.relative_to(base)
    except ValueError as e:
        raise ValueError("context_ref escapes node_contexts") from e
    return p


def save_context_snapshot(
    workspace_root: Path,
    session_id: str,
    snapshot: dict[str, Any],
    *,
    context_id: str = "",
) -> str:
    cid = (context_id or "").strip() or new_context_id()
    d = _base_dir(workspace_root) / str(session_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cid}.json"
    payload = dict(snapshot or {})
    payload.setdefault("version", 1)
    payload.setdefault("context_id", cid)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p.relative_to(workspace_root).as_posix()


def write_context_snapshot(workspace_root: Path, context_ref: str, snapshot: dict[str, Any]) -> str:
    p = _resolve_ref(workspace_root, context_ref)
    payload = dict(snapshot or {})
    payload.setdefault("version", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p.relative_to(workspace_root).as_posix()


def load_context_snapshot(workspace_root: Path, context_ref: str) -> dict[str, Any] | None:
    ref = (context_ref or "").strip()
    if not ref:
        return None
    try:
        p = _resolve_ref(workspace_root, ref)
    except Exception:
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def append_context_message(
    workspace_root: Path,
    context_ref: str,
    *,
    role: str,
    content: str,
) -> dict[str, Any] | None:
    snapshot = load_context_snapshot(workspace_root, context_ref)
    if snapshot is None:
        return None
    messages = snapshot.get("messages")
    if not isinstance(messages, list):
        messages = []
    messages = list(messages)
    messages.append({"role": str(role or "user"), "content": str(content or "")})
    snapshot["messages"] = messages
    write_context_snapshot(workspace_root, context_ref, snapshot)
    return snapshot


def delete_context_snapshot(workspace_root: Path, context_ref: str) -> bool:
    """删除单个上下文快照文件。"""
    ref = (context_ref or "").strip()
    if not ref:
        return False
    try:
        p = _resolve_ref(workspace_root, ref)
    except Exception:
        return False
    if p.exists() and p.is_file():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False


def cleanup_session_contexts(
    workspace_root: Path,
    session_id: str,
    *,
    keep_refs: set[str] | None = None,
) -> int:
    """删除某个 session 下不在 keep_refs 集合中的上下文快照。返回删除数量。"""
    d = _base_dir(workspace_root) / str(session_id or "unknown")
    if not d.exists() or not d.is_dir():
        return 0
    keep = set(keep_refs or set())
    count = 0
    for p in d.iterdir():
        if not p.is_file() or not p.name.endswith(".json"):
            continue
        rel = p.relative_to(workspace_root).as_posix()
        if rel in keep:
            continue
        try:
            p.unlink()
            count += 1
        except Exception:
            pass
    # 目录为空则删除
    try:
        remaining = list(d.iterdir())
        if not remaining:
            d.rmdir()
    except Exception:
        pass
    return count


def cleanup_old_contexts(
    workspace_root: Path,
    *,
    max_age_seconds: float = 86400.0,
    keep_refs: set[str] | None = None,
) -> int:
    """删除 node_contexts 下所有超过 max_age_seconds 的快照文件。返回删除数量。"""
    base = _base_dir(workspace_root)
    if not base.exists() or not base.is_dir():
        return 0
    keep = set(keep_refs or set())
    cutoff = time.time() - max_age_seconds
    count = 0
    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        for p in session_dir.iterdir():
            if not p.is_file() or not p.name.endswith(".json"):
                continue
            rel = p.relative_to(workspace_root).as_posix()
            if rel in keep:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except Exception:
                pass
        # 目录为空则删除
        try:
            remaining = list(session_dir.iterdir())
            if not remaining:
                session_dir.rmdir()
        except Exception:
            pass
    return count
