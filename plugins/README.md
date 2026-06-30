# 外部 Hook 插件目录

`plugins/` 用于放置本地外部 hook 插件。AI 节点启动时会扫描这个目录，并加载启用的 Python 文件。

## 启用规则

会被加载的文件必须同时满足以下条件：

1. 是普通文件。
2. 文件名以 `.py` 结尾。
3. 文件名不以 `_` 开头。
4. 文件名不以 `.disabled` 结尾。

`example_hook.py.disabled` 是模板文件。复制它或去掉 `.disabled` 后缀即可启用示例。

## 插件协议

插件必须提供：

```python
from engine.hooks import HookRegistry


def register(hook_registry: HookRegistry) -> None:
    # 目的：把外部 handler 接入指定 hook point。
    # 做法：调用 hook_registry.register。
    # 原因：外部插件和内置 handler 使用同一套注册协议。
    ...
```

插件可以提供可选的 `PLUGIN_META`：

```python
PLUGIN_META = {
    "name": "example-hook",
    "version": "1.0.0",
    "description": "Example external hook plugin template.",
    "author": "Clonoth",
    "hooks": ["before_step"],
}
```

加载器会补齐缺失的元数据字段。没有 `register()` 的文件会被跳过；加载失败只会记录日志，不会阻止引擎启动。重复扫描时，handler 按名称替换，因此注册应使用稳定的 `handler.name`。

完整开发说明见 `docs/plugin-system.md`。
