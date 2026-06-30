from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class HookResultLike:
    """Small hook-result shape used by built-in handlers without hook-package imports.

    Why: engine.builtin must hold both engine and supervisor built-ins without
    depending on the hook registry package or supervisor packages, otherwise the
    new shared location can reintroduce import cycles. How: provide the same attributes that
    HookRegistry and ai_step read at runtime. Purpose: keep built-in handlers
    duck-compatible with HookResult while preserving the callback-only boundary.
    """

    block: bool = False
    skip_step: bool = False
    action: Any = None
    reason: str = ""
    error_message: str = ""
    modified: bool = False


def hook_result(
    *,
    block: bool = False,
    skip_step: bool = False,
    action: Any = None,
    reason: str = "",
    error_message: str = "",
    modified: bool = False,
) -> HookResultLike:
    """Build a HookResult-compatible object without hook-package imports.

    Why: handler modules must not depend on the hook registry package after the
    relocation. How: centralize construction in this helper instead of duplicating
    SimpleNamespace defaults in every handler. Purpose: guarantee every returned
    result exposes all attributes consumed by the existing registry.
    """
    return HookResultLike(
        block=block,
        skip_step=skip_step,
        action=action,
        reason=reason,
        error_message=error_message,
        modified=modified,
    )
