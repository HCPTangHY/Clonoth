#!/usr/bin/env python3
"""data_cleanup.py — Periodic cleanup for Clonoth data directory.

Crontab every 6h. Pure Python, no LLM calls.

1. events.jsonl rotation (>50MB → keep 3 backups)
2. temp files: chat_dump/chunk/compact/messages, temp_summary (>24h)
3. artifacts (>24h)
4. attachments (>24h)
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
ARTIFACT_MAX_AGE = 24 * 3600          # 24 h
ATTACH_MAX_AGE = 24 * 3600            # 24 h

TEMP_GLOBS = [
    "chat_dump_*", "chat_chunk_*", "chat_compact_*", "chat_messages_*",
]

# Child Session 隔离（Phase C）：过期 child session 的 JSONL 文件清理
# child_*.jsonl 超过此时间未修改则删除。与 runtime.yaml 中 child_session.ttl_hours 对齐。
CHILD_SESSION_MAX_AGE = 24 * 3600     # 24 h（与默认 TTL 一致）

# Phase D：node_contexts 目录清理。child session 已替代 snapshot 机制，
# 保留 48h 宽裕期后清理旧文件（比 child session TTL 长一倍，确保兼容期充分）。
NODE_CONTEXTS_MAX_AGE = 48 * 3600     # 48 h


def _log_init():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
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
    if not directory.exists():
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


# ── Child Session JSONL cleanup ───────────────
def purge_expired_child_sessions():
    """清理过期的 child session JSONL 文件。

    Child Session 隔离（Phase C）：扫描 data/conversations/ 下所有 child_*.jsonl，
    按文件修改时间判断是否超过 CHILD_SESSION_MAX_AGE。超期则删除文件。
    映射表的清理由 supervisor 侧在 dispatch 时懒过期处理。
    """
    conv_dir = DATA_DIR / "conversations"
    if not conv_dir.exists():
        return
    cutoff = time.time() - CHILD_SESSION_MAX_AGE
    deleted = 0
    for p in conv_dir.glob("child_*.jsonl"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            pass
    logging.info("[child_sessions] deleted %d expired files", deleted)


# ── main ──────────────────────────────────────
def main():
    _log_init()
    logging.info("=== data cleanup start ===")

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        # Phase D：node_contexts 已被 child session 替代，启用定期清理
        (purge_dir, dict(directory=DATA_DIR / "node_contexts",
                         max_age=NODE_CONTEXTS_MAX_AGE, label="node_contexts",
                         recursive=True)),
        (purge_expired_child_sessions, {}),
        (purge_temp_globs, {}),
        (purge_dir, dict(directory=DATA_DIR / "temp",
                         max_age=TEMP_MAX_AGE, label="temp", recursive=True)),
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
