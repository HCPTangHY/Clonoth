from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import BaseProvider, ProviderResponse, ToolCall


class ProviderRegistry:
    """Registry for provider implementations discovered from this package."""

    def __init__(self) -> None:
        # [provider-registry 2026-05-03] 这里保存 provider_name 到类的映射。
        # 原因：runner 需要通过配置字符串找到 provider；做法：集中到 registry；
        # 目的：新增 provider 时只需新增 providers/*.py，不再修改 engine if-elif 路由。
        self._registry: dict[str, type[BaseProvider]] = {}

    def register(self, name: str, cls: type[BaseProvider]) -> None:
        """Register a provider class by its public provider name."""
        provider_name = (name or "").strip().lower()
        if not provider_name:
            raise ValueError("provider name is empty")
        if not issubclass(cls, BaseProvider):
            raise TypeError(f"{cls!r} is not a BaseProvider subclass")
        # [provider-registry 2026-05-03] 重复注册采用后写覆盖。
        # 原因：测试和热重载可能重新导入模块；做法：同名 key 覆盖为最新类；
        # 目的：保持注册操作幂等，避免重复发现导致启动失败。
        self._registry[provider_name] = cls

    def get(self, name: str) -> type[BaseProvider] | None:
        """Return the registered provider class for ``name`` if present."""
        return self._registry.get((name or "").strip().lower())

    def list(self) -> list[str]:
        """List all registered provider names in deterministic order."""
        return sorted(self._registry)


registry = ProviderRegistry()


def auto_discover() -> ProviderRegistry:
    """Import provider modules and register every concrete BaseProvider subclass."""
    package_dir = Path(__file__).resolve().parent
    package_name = __name__
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.ispkg or module_info.name in {"__init__", "base"}:
            continue
        # [provider-registry 2026-05-03] 通过文件扫描导入 provider 模块。
        # 原因：旧路由必须手写 import 和 if-elif；做法：遍历 providers/*.py；
        # 目的：让新增 provider 文件可以自动进入注册表。
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is BaseProvider or not issubclass(obj, BaseProvider):
                continue
            if obj.__module__ != module.__name__:
                continue
            provider_name = str(getattr(obj, "provider_name", "") or "").strip().lower()
            if not provider_name:
                continue
            registry.register(provider_name, obj)
    return registry


# [provider-registry 2026-05-03] 包加载时立即发现内置 provider。
# 原因：engine/node 需要同步读取可用 provider 名称；做法：初始化时扫描一次；
# 目的：保持调用方只需 import providers.registry 即可使用。
auto_discover()

# Compatibility exports: existing code may still import provider classes directly
# from providers. Discovery above does not depend on these imports; they only keep
# the public package surface stable while routing moves to the registry.
from .anthropic import AnthropicProvider  # noqa: E402
from .gemini import GeminiProvider  # noqa: E402
from .openai import OpenAIProvider  # noqa: E402
from .openai_responses import OpenAIResponsesProvider  # noqa: E402

__all__ = [
    "BaseProvider",
    "ProviderResponse",
    "ToolCall",
    "ProviderRegistry",
    "registry",
    "auto_discover",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAIResponsesProvider",
]
