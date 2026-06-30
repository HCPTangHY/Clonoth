from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from providers.base import BaseProvider, ProviderResponse, ToolCall
    from engine.node import Node


@dataclass
class HookContext:
    """Context passed to hook handlers.

    Why: hook handlers need a stable data object instead of importing ai_step
    internals. How: keep mutable references to the current message/tool state and
    optional per-hook data. Purpose: move business checks out of ai_step.py while
    allowing handlers to read or update the loop context intentionally.
    """

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    node: Any
    provider: Any
    rctx: Any
    step: int = 0
    response: Any = None
    tool_call: Any = None
    tool_calls: list = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """Decision returned by a hook handler.

    Why: handlers must communicate the same small set of decisions to the loop.
    How: use booleans for blocking/skipping, an optional TaskAction for terminal
    control flow, and message fields for model-visible refusal text. Purpose:
    keep hook execution predictable and easy to test.
    """

    block: bool = False
    skip_step: bool = False
    action: Any = None
    reason: str = ""
    error_message: str = ""
    modified: bool = False


class Handler(ABC):
    """Base class for all hook handlers."""

    name: str = "unnamed"
    priority: int = 0

    @abstractmethod
    async def handle(self, ctx: HookContext) -> HookResult | None:
        """Handle one hook event.

        Returning None means the handler did not intervene. Returning HookResult
        lets the registry decide whether to stop the chain or continue.
        """
        ...
