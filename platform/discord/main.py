"""Compatibility entry point for PM2.

[2026-05-14 refactor note] The production bot implementation now lives in
platform.discord.app. This file stays small so old process managers can keep
executing platform/discord/main.py without losing the import path setup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Hardcoded environment ---
# PM2 may inherit wrong env vars if started from another workspace.
# Force correct values here before anything else runs.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
os.environ.setdefault("CLONOTH_WORKSPACE", _REPO_ROOT)
os.environ.setdefault("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765")
os.environ.setdefault("CLONOTH_PORT", "8765")

sys.path.insert(0, _REPO_ROOT)

from platform.discord.app import main


if __name__ == "__main__":
    main()
