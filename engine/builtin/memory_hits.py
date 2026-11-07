"""Scoped memory hit timestamps shared by injection and Dream."""
from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)
_lock = threading.Lock()
_pending: dict[Path, dict[str, str | None]] = {}
_last_flush: dict[Path, float] = {}
_FLUSH_INTERVAL = 600


def hit_key(root: Path, book_path: Path, entry_id: str) -> str:
    """Use the physical book path, not its optional display name, as identity."""
    relative = book_path.resolve().relative_to((root / "data" / "memory").resolve())
    return json.dumps([relative.as_posix(), entry_id], ensure_ascii=False, separators=(",", ":"))


def read_hits(root: Path) -> dict[str, str]:
    path = root / "data" / "memory" / ".hit_cache.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid memory hit cache")
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _contains_id(path: Path, entry_id: str) -> bool:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    entries = data.get("entries") if isinstance(data, dict) else None
    return isinstance(entries, list) and any(
        isinstance(e, dict) and str(e.get("id") or "").strip() == entry_id
        for e in entries
    )


def _scoped_entry_exists(root: Path, key: str) -> bool:
    identity = json.loads(key)
    if (not isinstance(identity, list) or len(identity) != 2
            or not all(isinstance(part, str) and part for part in identity)):
        raise ValueError("invalid scoped memory hit key")
    relative, entry_id = identity
    if Path(relative).is_absolute() or Path(relative).suffix != ".yaml":
        raise ValueError("invalid memory book path")
    base = (root / "data" / "memory").resolve()
    path = (base / relative).resolve()
    path.relative_to(base)
    return _contains_id(path, entry_id)


def _flush_locked(root: Path) -> None:
    changes = _pending.get(root)
    if not changes:
        return
    directory = root / "data" / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    # Merge only our changes under a process-shared lock; Dream never rewrites
    # a filtered view of the file. Other namespaces' timestamps stay intact.
    with (directory / ".hit_cache.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        data = read_hits(root)
        for key, timestamp in changes.items():
            if timestamp is None or not _scoped_entry_exists(root, key):
                data.pop(key, None)
            else:
                data[key] = max(timestamp, data.get(key, ""))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".hit_cache-", delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, directory / ".hit_cache.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    changes.clear()
    _last_flush[root] = time.monotonic()


def flush_hits(root: Path | None = None) -> None:
    with _lock:
        for target in ([root.resolve()] if root is not None else list(_pending)):
            try:
                _flush_locked(target)
            except Exception:
                log.warning("Memory hit cache flush failed; pending changes retained", exc_info=True)


def record_hits(root: Path, entries: list[dict[str, Any]]) -> None:
    from datetime import datetime, timezone

    root = root.resolve()
    timestamp = datetime.now(timezone.utc).isoformat()
    with _lock:
        changes = _pending.setdefault(root, {})
        for entry in entries:
            key = entry.get("_hit_key")
            if isinstance(key, str) and key:
                changes[key] = timestamp
        last = _last_flush.setdefault(root, time.monotonic())
        due = bool(changes) and time.monotonic() - last > _FLUSH_INTERVAL
    if due:
        flush_hits(root)


def forget_hit(root: Path, book_path: Path, entry_id: str) -> None:
    """Delete the exact scope, including a cold sidecar, without guessing legacy IDs."""
    root = root.resolve()
    with _lock:
        changes = _pending.setdefault(root, {})
        changes[hit_key(root, book_path, entry_id)] = None
        # Old ID-only hits have no reliable owner. Preserve them as unscoped
        # evidence while any same-ID memory survives; never assign them to it.
        try:
            if not any(_contains_id(p, entry_id) for p in (root / "data" / "memory").rglob("*.yaml")):
                changes[entry_id] = None
            _flush_locked(root)
        except Exception:
            log.warning("Memory deleted but hit cache cleanup is pending", exc_info=True)


# One registration on the shared module, rather than one per injectable plugin.
# Abrupt process termination may still lose the unflushed interval, as before.
atexit.register(flush_hits)
