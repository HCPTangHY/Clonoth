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

    # [AutoC 2026-08-22] 瀑布链控制。handler 调用 ctx.stop_chain() 后，
    # 后续优先级更低的 handler 不再执行，但当前 handler 的 HookResult
    # 仍正常处理。与 action 终止的区别：stop_chain 不产生 TaskAction，
    # 流程继续执行，只是后续 handler 被跳过。
    _chain_stopped: bool = field(default=False, repr=False)

    def stop_chain(self) -> None:
        """Stop the hook chain after the current handler returns.

        Subsequent lower-priority handlers will not execute. The current
        handler's HookResult is still processed normally. This does NOT
        produce a TaskAction — the inference loop continues its flow.
        """
        self._chain_stopped = True


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
    # [AutoC 2026-08-23] Point-specific channels live in one dict instead of
    # dedicated fields. Why: each new hook point used to add a public field to
    # this shared return type (result_override, execution, intercepted), growing
    # the common contract for point-local semantics. How: handlers return
    # channels={"execution": ...}; the registry aggregates per CHANNEL_SEMANTICS
    # and fire sites read result.channels.get(name). Purpose: adding a channel
    # is a data entry, not a schema change.
    channels: dict[str, Any] = field(default_factory=dict)


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
