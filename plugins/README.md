# 外部插件目录

`plugins/` 放置本地外部插件。engine 与 supervisor 两个进程启动时各自扫描这个目录并加载启用的条目；插件管理器（设置区「插件」页）支持运行时装卸与启停。

## 条目形态与启用规则

一个条目可以是：

- **单文件**：`my_plugin.py`
- **目录包**：`my_plugin/`（含 `__init__.py`，内部可拆分模块）

被加载的条目必须：不以 `_` 开头、不以 `.disabled` 结尾。停用 = 重命名为 `xxx.disabled`；启用 = 去掉后缀。

## 插件协议

模块必须提供 `PLUGIN_META`（推荐，自动发现模式）或 `register(ctx)` 函数。

### 方式一：PLUGIN_META（推荐）

```python
PLUGIN_META = {
    "name": "my-plugin",          # 建议与条目名一致（不一致会打警告）
    "version": "1.0.0",
    "description": "做什么的一句话",
    "author": "you",
    "handler_class": "MyHandler",      # 可选：hook handler 类
    "hook_points": [("before_step", "handle")],  # handler 方法挂到哪个 hook 点
    "priority": 100,                    # 可选，hook 优先级
    "tools": [...],                     # 可选：工具声明
    "client": {...},                    # 可选：前端贡献（见下）
    "processes": ["supervisor"],        # 可选：只在指定进程加载
}


class MyHandler:
    name = "my-plugin"

    def __init__(self, ctx):   # 可选：声明参数即收到 EngineContext
        self.ctx = ctx

    async def handle(self, hook_ctx): ...
```

### 方式二：register(ctx)

```python
def register(ctx) -> None:
    ctx.hooks.register("before_step", my_handler)   # 拦截型
    # ctx.contributions.get("routes") / ("prompt_sections")   # 声明型
```

`ctx` 是 `EngineContext`，统一入口：`ctx.hooks`（拦截型注册表）、`ctx.providers`（渠道型）、`ctx.contributions`（声明型容器）。注意：旧式 `register(hook_registry)` 签名已废弃，加载会失败并给出迁移提示。

## 注册面速查

| 你要做什么 | 入口 | 进程 |
|---|---|---|
| 拦截/修改流程 | `ctx.hooks.register(hook点, handler)` | 两者 |
| 注册工具 | PLUGIN_META `tools` | engine |
| 注入 prompt 内容 | `ctx.contributions.get("prompt_sections").register_section(...)` | engine |
| 注册 HTTP 路由 | `ctx.contributions.get("routes").register(router, ...)` | supervisor |
| 前端面板/槽位/样式 | PLUGIN_META `web` | 任意（前端读 manifest） |

## 前端贡献（web）

```python
PLUGIN_META["web"] = {
    "panels": [{"id": "x", "slot": "settings", "title": "标题",
                "entry": "/v1/plugins/my-plugin/web/"}],
    "slots": [{"slot_id": "my.x", "slot": "input_toolbar_right",
               "priority": 50, "script": {"file": "web/slot.js"}}],
    "styles": {"file": "web/styles.css"},   # 或内联字符串
}
```

`script`/`styles` 支持内联字符串或 `{"file": 相对路径}` 文件引用（加载时内联，推荐文件方式——有编辑器支持）。槽位脚本上下文 `ctx = { el, data, api, state, events }`：`api.call(name, ...)` 调宿主动作（如 `retryMessage`），`api.request(path)` 直连后端 API，`state` 跨重挂载存活，`events.on(type, fn)` 订阅事件流。

## 其他

- `example_hook.py.disabled` 是模板：去掉 `.disabled` 后缀即可启用。
- 卸载是彻底的：注册返回的 disposer 按插件归档，卸载时逆序回放。
- 加载失败不阻断其他插件，原因在插件管理界面可见。
- 完整文档见 `docs/plugin-system.md`。
