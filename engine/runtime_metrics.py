"""Bounded, metadata-only worker memory samples for long-running diagnostics.

No forced GC, heap dumps, task contents, credentials, or new dependencies.
Optional tracemalloc measures Python allocations, not all native allocations.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_float

logger = logging.getLogger(__name__)


def process_memory_kib(proc_dir: Path = Path('/proc/self')) -> dict[str, int]:
    """Prefer proportional/private accounting; gracefully fall back off Linux."""
    result: dict[str, int] = {}
    try:
        status = (proc_dir / 'status').read_text(encoding='utf-8')
        for line in status.splitlines():
            key, _, value = line.partition(':')
            field = {'VmRSS': 'rss_kib', 'VmSwap': 'swap_kib', 'Threads': 'threads'}.get(key)
            if field:
                result[field] = int(value.split()[0])
    except (OSError, ValueError):
        pass
    try:
        fields = {}
        for line in (proc_dir / 'smaps_rollup').read_text(encoding='utf-8').splitlines():
            key, _, value = line.partition(':')
            if key in ('Rss', 'Pss', 'Private_Clean', 'Private_Dirty', 'Swap'):
                fields[key] = int(value.split()[0])
        if fields:
            result.update(rss_kib=fields.get('Rss', 0), pss_kib=fields.get('Pss', 0),
                          uss_kib=fields.get('Private_Clean', 0) + fields.get('Private_Dirty', 0),
                          swap_kib=fields.get('Swap', 0))
    except (OSError, ValueError):
        pass
    if 'rss_kib' in result:
        result['rss_swap_kib'] = result['rss_kib'] + result.get('swap_kib', 0)
    try:
        result['file_descriptors'] = sum(1 for _ in (proc_dir / 'fd').iterdir())
    except OSError:
        pass
    return result


class WorkerMetrics:
    """One sample/minute by default, at most ~10 MiB on disk per worker."""

    def __init__(self, root: Path, worker_id: str, generation_id: str, cfg: dict) -> None:
        self.worker_id = worker_id
        self.generation_id = generation_id
        self.started = time.monotonic()
        self.next_sample = 0.0
        self.claimed = 0
        self.reaped = 0
        self.interval = get_float(cfg, 'engine.memory_metrics.interval_sec', 60.0,
                                  min_value=10.0, max_value=3600.0)
        self.sink: logging.Logger | None = None
        self.handler: RotatingFileHandler | None = None
        self.owns_trace = False
        if not get_bool(cfg, 'engine.memory_metrics.enabled', True):
            return
        try:
            directory = root / 'data' / 'diagnostics'
            directory.mkdir(parents=True, exist_ok=True)
            name = re.sub(r'[^A-Za-z0-9_.-]', '_', worker_id)[:64] or 'worker'
            self.handler = RotatingFileHandler(directory / f'memory-{name}.jsonl',
                                              maxBytes=5 * 1024 * 1024, backupCount=1,
                                              encoding='utf-8')
            self.handler.setFormatter(logging.Formatter('%(message)s'))
            self.sink = logging.Logger(f'worker.memory.{worker_id}', level=logging.INFO)
            self.sink.propagate = False
            self.sink.addHandler(self.handler)
            if get_bool(cfg, 'engine.memory_metrics.tracemalloc', False):
                import tracemalloc
                if not tracemalloc.is_tracing():
                    tracemalloc.start(5)
                    self.owns_trace = True
        except Exception:
            logger.warning('Worker memory diagnostics initialization failed', exc_info=True)
            self.close()

    def sample(self, active: set[asyncio.Task], tools: Any) -> None:
        now = time.monotonic()
        if self.sink is None or now < self.next_sample:
            return
        self.next_sample = now + self.interval
        try:
            from engine.hooks import hook_registry
            from engine.faces.prompt_sections import prompt_section_registry
            from engine.inference.async_tools import _async_tool_tasks

            owners = hook_registry._ledger.counts()
            row = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'pid': os.getpid(), 'worker_id': self.worker_id, 'generation_id': self.generation_id,
                'uptime_sec': round(now - self.started, 1),
                **process_memory_kib(),
                'tasks_claimed': self.claimed, 'tasks_reaped': self.reaped,
                'active_tasks': sum(not task.done() for task in active),
                'asyncio_tasks': len(asyncio.all_tasks()),
                'async_tools': dict(Counter(item.get('status', 'unknown') for item in _async_tool_tasks.values())),
                'plugin_disposers': owners, 'plugin_disposers_total': sum(owners.values()),
                'hooks': sum(len(items) for items in hook_registry._hooks.values()),
                'prompt_sections': len(prompt_section_registry._sections),
                'registered_tools': len(tools.list_specs()),
                'python_allocated_blocks': sys.getallocatedblocks(),
                'gc_counts': gc.get_count(),
                'gc_collections': [stat['collections'] for stat in gc.get_stats()],
            }
            if self.owns_trace:
                import tracemalloc
                current, peak = tracemalloc.get_traced_memory()
                row.update(python_traced_bytes=current, python_traced_peak_bytes=peak)
            self.sink.info(json.dumps(row, ensure_ascii=False, separators=(',', ':')))
        except Exception:
            logger.warning('Worker memory sample failed', exc_info=True)

    def close(self) -> None:
        if self.handler is not None:
            self.handler.close()
            if self.sink is not None:
                self.sink.removeHandler(self.handler)
        self.handler = None
        self.sink = None
        if self.owns_trace:
            import tracemalloc
            tracemalloc.stop()
            self.owns_trace = False
