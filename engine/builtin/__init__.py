from __future__ import annotations

from .loader import auto_discover_and_register

# Why: built-in registration is now metadata-driven. How: expose only the auto
# discovery entry point from this package root. Purpose: remove hard-coded handler
# wiring while keeping a compact import path for startup code and tests.
__all__ = ["auto_discover_and_register"]
