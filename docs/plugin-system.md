# Clonoth 插件系统

本文说明 Clonoth 当前的两类扩展机制：P1 Provider Registry 和 P3 Hook System。前者用于扩展模型服务提供方，后者用于在推理循环的固定位置插入处理逻辑。

## P1 Provider Registry

Provider Registry 位于 `providers/__init__.py`。它的职责是把配置中的 provider 名称解析为具体的 `BaseProvider` 子类，避免推理循环继续维护固定的 `if-elif` 分支。

### 自动发现

包加载时会调用 `auto_discover()`。它会扫描 `providers/` 目录下的普通 Python 模块，并跳过包、`__init__.py` 和 `base.py`。每个模块被导入后，注册表会检查其中定义的类：

1. 类必须是 `BaseProvider` 的子类。
2. 类不能是 `BaseProvider` 本身。
3. 类必须定义在当前被扫描的模块中。
4. 类属性 `provider_name` 必须是非空字符串。

满足条件的类会按小写后的 `provider_name` 注册到 `ProviderRegistry`。同名注册会用新的类覆盖旧的类，这使重复导入、热重载和测试环境中的重复注册保持幂等。

当前内置 provider 名称包括：

| provider_name | 类 |
| --- | --- |
| `openai` | `OpenAIProvider` |
| `anthropic` | `AnthropicProvider` |
| `gemini` | `GeminiProvider` |
| `openai-responses` | `OpenAIResponsesProvider` |

### BaseProvider ABC

`BaseProvider` 定义在 `providers/base.py`。所有 provider 都必须继承它，并实现统一的调用接口。

关键接口如下：

| 成员 | 说明 |
| --- | --- |
| `provider_name: str` | 自动发现和节点配置使用的稳定 key。 |
| `__init__(model: str, name: str = "")` | 保存模型名和 provider 实例名。未传 `name` 时回落到 `provider_name`。 |
| `chat(messages, tools)` | 非流式模型调用，返回 `ProviderResponse`。 |
| `chat_stream(messages, tools, on_text, on_thinking)` | 流式模型调用，返回 `ProviderResponse`。 |

`ProviderResponse` 是 provider 返回给引擎的统一结果结构。正常的供应商错误，例如超时、HTTP 4xx/5xx 或非法响应，应尽量返回 `ProviderResponse(ok=False, error="...")`，而不是依赖异常作为常规控制流。

### provider_name 与运行时解析

节点配置中的 `provider` 字段就是 `ProviderRegistry` 的 key。节点加载时会用 `provider_registry.list()` 校验该字段；未注册的值会回退为空字符串。模型解析阶段再把空值视为默认 `openai`。

运行器创建 provider 时会调用 `provider_registry.get(name)`。如果找不到请求的 provider，当前行为会回退到 `openai`，以保持旧配置的降级语义。实例化时会准备以下参数，并根据构造函数签名过滤不支持的参数：

- `http`
- `api_key`
- `base_url`
- `model`
- `provider_options`

非 OpenAI provider 的环境变量前缀由 provider 名称转换而来。例如 `my-provider` 会读取 `MY_PROVIDER_API_KEY` 和 `MY_PROVIDER_BASE_URL`。

## P3 Hook System

Hook System 位于 `engine/hooks/`。它提供一个进程内的 `hook_registry` 单例，也允许测试或局部逻辑创建新的 `HookRegistry` 实例。

Hook 的目标是把推理循环中的横切逻辑移出 `engine/inference/ai_step.py`，例如上下文压缩、取消检查、审批、附件收集和纯文本响应策略。

### HookRegistry 行为

`HookRegistry` 按 hook point 保存 handler 列表。注册时使用 `handler.name` 去重，同一 hook point 下同名 handler 会被新实例替换。handler 按 `priority` 从高到低执行。

`fire(hook_point, ctx)` 会依次调用 handler 的 `handle(ctx)` 方法。handler 抛出的异常会被记录日志，不会中断整个推理循环。返回规则如下：

- 返回 `None` 表示不介入。
- 返回 `HookResult(modified=True)` 表示修改了 `HookContext`，但允许后续 handler 继续执行。
- 返回 `HookResult(block=True)`、`HookResult(skip_step=True)` 或带有 `action` 的结果会停止当前 hook 链。
- 多个非终止 handler 的 `modified` 标记会被聚合。

### Handler 协议

所有 handler 都继承 `engine.hooks.Handler`。最小协议如下：

| 成员 | 说明 |
| --- | --- |
| `name` | handler 的唯一名称。用于去重和替换。 |
| `priority` | 执行优先级。数值越大越先执行。 |
| `async handle(ctx)` | 处理一次 hook 事件，返回 `HookResult` 或 `None`。 |

### HookContext

`HookContext` 是传给 handler 的上下文对象。字段如下：

| 字段 | 说明 |
| --- | --- |
| `messages` | 当前消息列表。部分 handler 会原地修改它。 |
| `tools` | 当前可见工具定义。 |
| `node` | 当前节点定义。 |
| `provider` | 当前 provider 实例。 |
| `rctx` | 当前 `RunContext`。 |
| `step` | 当前推理步数。 |
| `response` | 当前 provider 响应。只在相关 hook 点提供。 |
| `tool_call` | 当前单个工具调用。只在单工具 hook 点提供。 |
| `tool_calls` | 当前一批工具调用。 |
| `extra` | 每个 hook 点的附加数据。用于传递 loop state、工具结果、注入材料等。 |

常见 `extra` 字段如下：

| hook point | 常见 extra 字段 |
| --- | --- |
| `before_prompt_build` | `runtime_cfg`、`instruction_text`、`history`、`attachments`、`system_prompt`、`apply_injection` |
| `before_step` | `loop_state`、`step_count` |
| `before_tool_call` | 整轮检查时有 `pseudo_calls`、`real_tool_calls`；单个真实工具检查时有 `real_tool_calls` |
| `after_tool_call` | `loop_state`、`tool_result`、`tool_attachments` |
| `after_llm_call` | `loop_state` |
| `before_response` | `loop_state` |
| `on_task_end` | `loop_state`、`step_count`、`task_action` |
| `on_task_error` | `loop_state`、`step_count` |

### HookResult

`HookResult` 表示 handler 对推理循环的决策。

| 字段 | 说明 |
| --- | --- |
| `block` | 阻止当前动作。调用方通常会把 `error_message` 或 `reason` 写回模型。 |
| `skip_step` | 跳过当前 step 或当前处理单元。具体语义由触发点决定。 |
| `action` | 终止性 `TaskAction`。用于 finish、fail、dispatch、cancel、preempt 等控制流。 |
| `reason` | 面向内部或模型的简短原因。 |
| `error_message` | 更明确的错误文本。 |
| `modified` | 表示 handler 修改了上下文或运行状态。 |

### 钩子点列表

当前支持以下 hook point：

| hook point | 触发时机 | 典型用途 |
| --- | --- | --- |
| `before_prompt_build` | 初始 messages 组装完成后，工具定义注入前。 | 注入 skill、memory 或其他提示词材料。 |
| `before_step` | 每次推理循环顶部，调用 LLM 之前。 | 检查取消、处理 preempt、执行上下文压缩。 |
| `before_tool_call` | 工具调用处理前。当前既有整轮工具调用检查，也有单个真实工具执行前检查。 | finish 并列调用保护、审批、工具策略检查。 |
| `after_tool_call` | 真实工具返回后，工具结果写回模型前。 | 收集附件、记录工具副作用。 |
| `before_response` | 模型没有产生工具调用，准备处理纯文本响应前。 | 混合输出隐式 finish、tool-only 模式重试或失败。 |
| `after_llm_call` | provider 返回 `ProviderResponse` 后。 | 统计 token 用量、记录模型响应信息。 |
| `on_task_end` | 正常 finish 返回前。 | 保存上下文快照、补充完成元数据。 |
| `on_task_error` | 错误结束路径。当前已覆盖达到最大步数等路径。 | 保存错误现场、补充失败元数据。 |

## 内置 handler

内置 handler 统一放在 `engine/builtin/`。AI 节点和 supervisor 启动时通过 `auto_discover_and_register(hook_registry)` 扫描 `PLUGIN_META` 并注册。注册是幂等的，因为 `HookRegistry` 会按 handler 名称替换旧实例。

| handler 类 | name | hook point | priority | 说明 |
| --- | --- | --- | ---: | --- |
| `PreemptChecker` | `preempt_checker` | `before_step` | 100 | 检查取消请求和软打断状态。需要注入新用户消息时，会移除旧动态上下文并重建动态 skill、memory 和附件消息。 |
| `CompactChecker` | `compact_checker` | `before_step` | 50 | 在循环顶部执行 microcompact、闲置后的 proactive snip，并在上下文超过阈值时触发系统压缩节点。 |
| `KnowledgeInjector` | `knowledge_inject` | `before_prompt_build` | 50 | 统一调用 skill runtime 和 memory runtime 构建静态、动态知识消息，并在需要时重建 prompt 布局。 |
| `FinishGuardHandler` | `finish_guard` | `before_tool_call` | 100 | 拒绝 `finish()` 与其他非 `reply()` 工具在同一轮同时调用，避免任务终止后遗漏其他工具结果。 |
| `ApprovalHandler` | `approval` | `before_tool_call` | 90 | 在真实工具执行前调用 `RunContext` 上可用的审批接口，并把审批结果归一化为 `HookResult`。 |
| `AttachmentCollector` | `attachment_collector` | `after_tool_call` | 0 | 从真实工具结果中收集附件，写入局部附件列表和 loop state，供最终输出选择。 |
| `UsageTracker` | `usage_tracker` | `after_llm_call` | 0 | 读取 `ProviderResponse.usage`，累加到 `RunContext.total_usage`。 |
| `PlaintextRetryHandler` | `plaintext_retry` | `before_response` | 0 | 处理没有工具调用的纯文本响应。hybrid 模式下生成隐式 finish；tool-only 模式下追加重试提示，超过次数后失败。 |
| `ContextSnapshotSaver` | `context_snapshot_saver` | `on_task_end`、`on_task_error` | 0 | 保存循环上下文快照，并把 `context_ref` 和 `snapshot_saved` 写入 `ctx.extra`。 |

## 外部插件协议

外部 hook 插件放在工作区的 `plugins/` 目录。AI 节点启动时会扫描该目录，并加载启用的 Python 文件。

启用条件如下：

1. 必须是普通文件。
2. 文件名必须以 `.py` 结尾。
3. 文件名不能以 `_` 开头。
4. 文件名不能以 `.disabled` 结尾。

插件模块必须暴露一个可调用的 `register(hook_registry)` 函数。插件可以选择暴露 `PLUGIN_META` 字典，用于展示插件元数据。

`PLUGIN_META` 的文档字段如下：

| 字段 | 说明 |
| --- | --- |
| `name` | 插件名称。为空时使用文件名。 |
| `version` | 插件版本。为空时使用 `unknown`。 |
| `description` | 插件说明。 |
| `author` | 作者。 |
| `hooks` | 插件注册的 hook point 列表。 |

插件加载失败只会记录日志，不会阻止引擎启动。没有 `register()` 函数的插件会被跳过。重复加载时，handler 按名称替换，插件元数据也按插件名称替换。

最小插件示例：

```python
from engine.hooks import Handler, HookContext, HookRegistry, HookResult

# 目的：让插件列表能展示人类可读信息。
# 做法：在模块级声明 PLUGIN_META，加载器会读取并补齐缺省字段。
# 原因：register() 只负责注册逻辑，不适合承载展示元数据。
PLUGIN_META = {
    "name": "my-hook",
    "version": "1.0.0",
    "description": "示例外部 hook 插件。",
    "author": "local",
    "hooks": ["before_step"],
}


class MyHandler(Handler):
    # 目的：让 HookRegistry 能按名称去重。
    # 做法：使用稳定、唯一的 handler name。
    # 原因：AI 节点会重复扫描 plugins/，同名替换能保持加载幂等。
    name = "my_handler"
    priority = 10

    async def handle(self, ctx: HookContext) -> HookResult | None:
        # 目的：示例插件不改变运行时行为。
        # 做法：返回 None，表示不介入当前 hook 链。
        # 原因：开发者可以在这里读取或修改 ctx.messages、ctx.extra 等字段。
        return None


def register(hook_registry: HookRegistry) -> None:
    # 目的：把 handler 安装到指定 hook point。
    # 做法：调用 HookRegistry.register。
    # 原因：外部插件和内置 handler 使用同一个注册协议。
    hook_registry.register("before_step", MyHandler())
```

## 开发指南：新增 Provider

新增 provider 时，优先把文件放入 `providers/`，并让类继承 `BaseProvider`。当前外部插件加载器只加载 hook 插件，不会从 `plugins/` 中发现 provider。

建议步骤如下：

1. 新建 `providers/my_provider.py`。
2. 定义继承 `BaseProvider` 的类。
3. 设置非空、全小写、稳定的 `provider_name`。
4. 实现 `chat()` 和 `chat_stream()`。
5. 在节点 YAML 中设置 `provider: my-provider`。
6. 为新 provider 编写最小单元测试，验证 `provider_registry.list()` 能看到名称，并验证正常响应和错误响应都会返回 `ProviderResponse`。

示例骨架：

```python
from __future__ import annotations

from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse


class MyProvider(BaseProvider):
    # 目的：让 ProviderRegistry 自动发现并注册这个 provider。
    # 做法：声明稳定的 provider_name，节点配置会使用同一个字符串。
    # 原因：推理循环不再维护具体 provider 的 if-elif 分支。
    provider_name = "my-provider"

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        # 目的：保持实例 name 与 registry key 一致。
        # 做法：把 provider_name 传给 BaseProvider。
        # 原因：下游可能读取 provider.name 判断 provider 特性。
        super().__init__(model=model, name=self.provider_name)
        self._http = http
        self._api_key = api_key
        self._base_url = (base_url or "https://example.com").rstrip("/")
        self._options = provider_options or {}

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ProviderResponse:
        # 目的：把供应商响应转换为统一的 ProviderResponse。
        # 做法：在这里完成请求、错误归一化和工具调用解析。
        # 原因：引擎只依赖 BaseProvider 合约，不理解供应商私有格式。
        return ProviderResponse(ok=False, error="not implemented")

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        # 目的：提供与非流式接口一致的最终结果。
        # 做法：流式片段通过回调送出，结束时仍返回 ProviderResponse。
        # 原因：推理循环需要统一读取文本、工具调用、reasoning、usage 和附件。
        return await self.chat(messages=messages, tools=tools)
```

## 开发指南：新增 Hook Handler

新增 hook handler 时，先确定要插入的 hook point，再判断是否需要外部插件形式。如果逻辑属于内置核心行为，可放在 `engine/builtin/`，并在文件内声明 `PLUGIN_META`；如果逻辑属于本地定制，应放入 `plugins/`。

建议步骤如下：

1. 选择一个 hook point。
2. 定义继承 `Handler` 的类。
3. 设置稳定且唯一的 `name`。
4. 设置合适的 `priority`。
5. 在 `handle(ctx)` 中只读取当前 hook point 明确提供的数据。
6. 修改 `ctx.messages` 或 `ctx.extra` 后返回 `HookResult(modified=True)`。
7. 需要阻止工具或步骤时返回 `HookResult(block=True, reason="...")` 或 `HookResult(skip_step=True)`。
8. 需要结束或转交任务时返回带 `action` 的 `HookResult`。
9. 为 handler 编写测试，覆盖不介入、修改、阻止和异常容错等路径。

示例 handler：

```python
from engine.hooks import Handler, HookContext, HookResult


class RequireSafeToolName(Handler):
    # 目的：让注册表能幂等替换这个 handler。
    # 做法：声明唯一 name，并使用较高 priority 在工具执行前优先运行。
    # 原因：安全策略应早于真实工具执行生效。
    name = "require_safe_tool_name"
    priority = 80

    async def handle(self, ctx: HookContext) -> HookResult | None:
        # 目的：只处理单个真实工具调用场景。
        # 做法：没有 ctx.tool_call 时直接返回 None。
        # 原因：before_tool_call 也会用于整轮工具调用检查。
        if ctx.tool_call is None:
            return None

        tool_name = getattr(ctx.tool_call, "name", "")
        if tool_name.startswith("danger_"):
            # 目的：阻止不允许的工具执行。
            # 做法：返回 block=True，并提供可写回模型的错误信息。
            # 原因：调用方会保持工具调用配对完整，再让模型修正下一步行为。
            return HookResult(
                block=True,
                reason="unsafe_tool_name",
                error_message=f"Tool {tool_name} is not allowed.",
            )

        return None
```

注册到外部插件时，在 `plugins/my_policy.py` 中提供 `register()`：

```python
from engine.hooks import HookRegistry


def register(hook_registry: HookRegistry) -> None:
    # 目的：把本地策略接入工具调用前检查。
    # 做法：注册到 before_tool_call。
    # 原因：真实工具执行前是阻止危险工具的合适位置。
    hook_registry.register("before_tool_call", RequireSafeToolName())
```
