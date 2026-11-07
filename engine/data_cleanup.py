#!/usr/bin/env python3
"""data_cleanup.py — Periodic cleanup for Clonoth data directory.

Scheduling is configured externally. Pure Python, no LLM calls.

1. Local events/signals rotation with bounded backup counts.
2. Temporary files older than 24h; data/temp also has a 1GiB size cap.
3. Artifacts and attachments older than 24h.

Supervisor owns session and context-snapshot cleanup, including active-task
protection. This script never deletes those histories or snapshots.
"""

import logging
import os
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS_FILE = DATA_DIR / "events.jsonl"
SIGNALS_FILE = DATA_DIR / "signals.jsonl"
LOG_FILE = DATA_DIR / "logs" / "cleanup.log"

# ── Thresholds ─────────────────────────────────
EVENTS_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
EVENTS_BACKUPS = 3

TEMP_MAX_AGE = 24 * 3600              # 24 h
TEMP_MAX_BYTES = 1024 * 1024 * 1024   # 1 GiB hard cap for data/temp
ARTIFACT_MAX_AGE = 24 * 3600          # 24 h
ATTACH_MAX_AGE = 24 * 3600            # 24 h

TEMP_GLOBS = [
    "chat_dump_*", "chat_chunk_*", "chat_compact_*", "chat_messages_*",
]


def _log_init():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=handlers,
    )


# ── 1. events.jsonl rotation ──────────────────
def rotate_events():
    if not EVENTS_FILE.exists():
        return
    sz = EVENTS_FILE.stat().st_size
    if sz < EVENTS_MAX_BYTES:
        logging.info("[events] %dKB < %dMB threshold, skip",
                     sz // 1024, EVENTS_MAX_BYTES // (1024 * 1024))
        return
    logging.info("[events] %dMB — rotating", sz // (1024 * 1024))
    for i in range(EVENTS_BACKUPS, 0, -1):
        p = DATA_DIR / f"events.jsonl.{i}"
        if i == EVENTS_BACKUPS and p.exists():
            p.unlink()
        elif p.exists():
            p.rename(DATA_DIR / f"events.jsonl.{i + 1}")
    EVENTS_FILE.rename(DATA_DIR / "events.jsonl.1")
    logging.info("[events] done")


# ── 1b. signals.jsonl rotation ────────────────
SIGNALS_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
SIGNALS_BACKUPS = 2

def rotate_signals():
    if not SIGNALS_FILE.exists():
        return
    sz = SIGNALS_FILE.stat().st_size
    if sz < SIGNALS_MAX_BYTES:
        logging.info("[signals] %dKB < %dMB threshold, skip",
                     sz // 1024, SIGNALS_MAX_BYTES // (1024 * 1024))
        return
    logging.info("[signals] %dMB — rotating", sz // (1024 * 1024))
    for i in range(SIGNALS_BACKUPS, 0, -1):
        p = DATA_DIR / f"signals.jsonl.{i}"
        if i == SIGNALS_BACKUPS and p.exists():
            p.unlink()
        elif p.exists():
            p.rename(DATA_DIR / f"signals.jsonl.{i + 1}")
    SIGNALS_FILE.rename(DATA_DIR / "signals.jsonl.1")
    logging.info("[signals] done")


# ── generic dir purge ─────────────────────────
def purge_dir(directory: Path, max_age: float, label: str, recursive=False):
    """Delete files older than max_age seconds. Remove empty sub-dirs if recursive."""
    if directory.is_symlink() or not directory.is_dir():
        return
    cutoff = time.time() - max_age
    deleted = 0

    targets = list(directory.rglob("*")) if recursive else list(directory.iterdir())
    for p in targets:
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            pass

    if recursive:
        for dirpath, _, _ in os.walk(str(directory), topdown=False):
            dp = Path(dirpath)
            if dp == directory:
                continue
            try:
                next(dp.iterdir())         # non-empty → skip
            except StopIteration:
                dp.rmdir()                 # empty → remove
            except Exception:
                pass

    logging.info("[%s] deleted %d files", label, deleted)


# ── temp glob purge ───────────────────────────
def purge_temp_globs():
    cutoff = time.time() - TEMP_MAX_AGE
    deleted = 0
    for pat in TEMP_GLOBS:
        for p in DATA_DIR.glob(pat):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except Exception:
                pass
    logging.info("[temp] deleted %d files", deleted)


# ── Tool-result artifact cleanup ──────────────
def purge_expired_tool_result_artifacts():
    """Delete stale task-scoped tool result artifact directories."""
    # [AutoC 2026-06-08] Why: supervisor normally deletes tool_results/{task_id}
    # when a task reaches a terminal state, but abnormal exits can skip that path.
    # How: scan the task directories and remove any directory whose newest file is
    # older than ARTIFACT_MAX_AGE. Purpose: raw large tool outputs cannot remain on
    # disk indefinitely after crashes or forced process stops.
    root = DATA_DIR / "artifacts" / "tool_results"
    if not root.exists():
        return
    cutoff = time.time() - ARTIFACT_MAX_AGE
    deleted = 0
    for task_dir in list(root.iterdir()):
        if not task_dir.is_dir():
            continue
        try:
            newest = max((p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()), default=task_dir.stat().st_mtime)
            if newest < cutoff:
                import shutil
                shutil.rmtree(task_dir)
                deleted += 1
        except Exception:
            pass
    logging.info("[tool_results] deleted %d stale task dirs", deleted)


# ── Temporary-directory capacity ─────────────
def enforce_dir_size_cap(directory: Path, max_bytes: int, label: str):
    """Delete oldest regular files until the data directory is within its cap."""
    if max_bytes < 0:
        raise ValueError("directory size cap must be non-negative")
    if directory.is_symlink() or not directory.is_dir():
        return
    # Do not authorize an external directory through a symlinked parent.
    data_root = DATA_DIR.parent.resolve() / DATA_DIR.name
    if not directory.resolve().is_relative_to(data_root):
        logging.warning("[%s_cap] directory is outside data/: %s", label, directory)
        return

    files = []
    total = 0
    for dirpath, _, filenames in os.walk(directory, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                st = path.stat()
                total += st.st_size
                files.append((st.st_mtime_ns, st.st_size, path))
            except OSError:
                continue
    if total <= max_bytes:
        logging.info("[%s_cap] %d bytes <= %d byte cap, skip", label, total, max_bytes)
        return

    files.sort(key=lambda item: (item[0], str(item[2])))
    deleted = 0
    freed = 0
    for _, size, path in files:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError as exc:
            logging.warning("[%s_cap] could not remove %s: %s", label, path, exc)
            continue
        total -= size
        freed += size
        deleted += 1

    for dirpath, _, _ in os.walk(directory, topdown=False, followlinks=False):
        path = Path(dirpath)
        if path == directory:
            continue
        try:
            path.rmdir()
        except OSError:
            pass  # Nonempty, unavailable or concurrently removed directory.
    report = logging.warning if total > max_bytes else logging.info
    report("[%s_cap] removed %d files (%d bytes freed), remaining %d bytes (cap %d)",
           label, deleted, freed, total, max_bytes)


# ── main ──────────────────────────────────────
def main():
    _log_init()
    logging.info("=== data cleanup start ===")

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        (purge_temp_globs, {}),
        (purge_dir, dict(directory=DATA_DIR / "temp",
                         max_age=TEMP_MAX_AGE, label="temp", recursive=True)),
        (enforce_dir_size_cap, dict(directory=DATA_DIR / "temp",
                                   max_bytes=TEMP_MAX_BYTES, label="temp")),
        (purge_dir, dict(directory=DATA_DIR / "temp_summary",
                         max_age=TEMP_MAX_AGE, label="temp_summary")),
        (purge_expired_tool_result_artifacts, {}),
        (purge_dir, dict(directory=DATA_DIR / "artifacts",
                         max_age=ARTIFACT_MAX_AGE, label="artifacts")),
        (purge_dir, dict(directory=DATA_DIR / "attachments",
                         max_age=ATTACH_MAX_AGE, label="attachments", recursive=True)),
    ]

    for fn, kw in tasks:
        try:
            fn(**kw)
        except Exception as e:
            logging.error("[%s] %s", fn.__name__, e)

    logging.info("=== data cleanup done ===")


if __name__ == "__main__":
    main()
