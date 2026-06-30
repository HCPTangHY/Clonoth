"""Signal → JSONL file bridge.

Writes every signal as a JSON line to data/signals.jsonl.
Zero dependency on supervisor — works purely within the engine process.

Usage:
    from engine.signals.bridge import install_event_bridge
    install_event_bridge(bus)  # idempotent, call once at startup
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.signals.types import Signal
from engine.signals.bus import SignalBus

log = logging.getLogger(__name__)

_bridge_installed = False
_SIGNALS_LOG: Optional[Path] = None
_write_lock = threading.Lock()


def _bridge_handler(signal: Signal) -> None:
    """Append signal as a JSON line to the signals log file."""
    if _SIGNALS_LOG is None:
        return
    # Filter by bridge_patterns if configured
    if _bridge_patterns is not None:
        import fnmatch
        if not any(fnmatch.fnmatch(signal.name, p) for p in _bridge_patterns):
            return
    entry = {
        "ts": datetime.fromtimestamp(signal.ts, tz=timezone.utc).isoformat(),
        "name": signal.name,
        "payload": signal.payload,
    }
    if signal.trace_id:
        entry["trace_id"] = signal.trace_id
    if signal.span_id:
        entry["span_id"] = signal.span_id
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _write_lock:
            with _SIGNALS_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        log.debug("Failed to write signal %s to log", signal.name, exc_info=True)


# Lock for idempotent install check
_install_lock = threading.Lock()
# Glob patterns for filtering which signals to bridge (None = all)
_bridge_patterns: Optional[list] = None


def install_event_bridge(bus: SignalBus, log_dir: Optional[Path] = None,
                        patterns: Optional[list] = None) -> None:
    """Install the JSONL file bridge on a SignalBus.

    Idempotent: multiple calls only register the handler once.

    Args:
        bus: The SignalBus instance to bridge.
        log_dir: Directory for signals.jsonl. Defaults to data/ relative to workspace root.
        patterns: Optional list of glob patterns to filter which signals are bridged.
                  If None, all signals are bridged.
    """
    global _bridge_installed, _SIGNALS_LOG, _bridge_patterns
    with _install_lock:
        if _bridge_installed:
            log.debug("Signal bridge already installed, skipping")
            return
        if log_dir is None:
            log_dir = Path(__file__).resolve().parent.parent.parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        _SIGNALS_LOG = log_dir / "signals.jsonl"
        _bridge_patterns = patterns
        _bridge_installed = True
        bus.subscribe("*", _bridge_handler)
        log.info("Signal → JSONL bridge installed: %s (patterns=%s)", _SIGNALS_LOG, patterns)
