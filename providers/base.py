from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# [AutoC 2026-07-28] Anthropic 要求 tool_call id 匹配 ^[a-zA-Z0-9_-]+$。
# 某些上游 OpenAI 兼容端点返回 "grep:9"、"shell:0" 等含冒号的 id。
_TOOL_ID_CLEAN_RE = re.compile(r'[^a-zA-Z0-9_-]')


def sanitize_tool_call_id(raw_id: str) -> str:
    """将 tool_call id 中不合规的字符替换为下划线。"""
    if not raw_id:
        return raw_id
    return _TOOL_ID_CLEAN_RE.sub('_', raw_id)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self):
        # 入口防御：阻止脏 id 进入系统
        if self.id:
            self.id = sanitize_tool_call_id(self.id)


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
    # Unified display text: API reasoning or a summary. Replay data belongs to
    # provider_meta; the engine must not turn this text into request fields.
    reasoning: str | None = None
    error: str | None = None
    status_code: int | None = None
    raw: dict[str, Any] | None = None
    usage: dict[str, int] | None = None  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    # [refactor 2026-04-18] 新增：provider 解析出的内联附件（图片等）
    inline_data: list[dict] = field(default_factory=list)
    # [refactor 2026-04-18] 新增：provider 私有元数据，engine 只搬运不解读
    provider_meta: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    # [provider-registry 2026-05-03] provider_name 是自动注册使用的稳定 key。
    # 原因：engine 不应再用 if-elif 认识每个 provider；做法：每个子类声明自己的 key；
    # 目的：新增 provider 文件后可以被 registry 发现，而不用修改 engine 路由代码。
    provider_name: str = ""

    # [fix 2026-04-18] 新增 name 参数：让 engine 能动态获取 provider 名称。
    # [provider-registry 2026-05-03] name 默认回落到 provider_name，避免实例名和注册 key 分叉。
    def __init__(self, *, model: str, name: str = ""):
        self.model = model
        self.name = name or self.provider_name

    def prepare_history(
        self, messages: list[dict[str, Any]], *, tool_mode: str = "native",
    ) -> list[dict[str, Any]]:
        """Choose a request-local history representation inside the provider.

        Inputs are internal messages, including provider metadata. Implementations
        may override this method; the engine never selects a provider's history
        representation. The default uses the configured text/native tool protocol.
        """
        from .tool_format import build_llm_messages, create_tool_formatter

        return build_llm_messages(messages, create_tool_formatter(tool_mode))

    def build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, tool_mode: str = "native", stream: bool = False,
    ) -> dict[str, Any]:
        """Build an API payload from internal history, also usable by diagnostics."""
        raise NotImplementedError

    @abstractmethod
    async def chat(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        tool_mode: str = "native",
    ) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_mode: str = "native",
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        # [provider-registry 2026-05-03] 所有内置 provider 已实现流式接口。
        # 在 ABC 中显式声明它，目的：插件作者能看到完整合约，抽象类也能阻止漏实现。
        # [tool-stream 2026-05-19] 新增 on_tool_delta。
        # 原因：tool_call 参数过去只在 provider 内部攒完后返回，前端无法实时预览。
        # 做法：把工具调用增量作为可选 callback 加入统一流式合约。
        # 目的：text、thinking、tool_call 共享同一条实时事件管道，同时不破坏 ProviderResponse。
        raise NotImplementedError
