from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    """A rust-like provider result.

    - ok=True  -> text/tool_calls are valid
    - ok=False -> error contains a human-readable error message

    Worker loops should NOT rely on exceptions for normal provider failures
    (timeout, 4xx/5xx, invalid payload...).
    """

    ok: bool
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    status_code: int | None = None
    raw: dict[str, Any] | None = None


class BaseProvider(ABC):
    def __init__(self, *, model: str):
        self.model = model

    @abstractmethod
    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        raise NotImplementedError
