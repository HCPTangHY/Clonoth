# Hook System

`engine/hooks/` 是 Clonoth 的推理循环钩子系统。它提供 `HookRegistry`、`Handler`、`HookContext`、`HookResult` 和外部插件加载器。

## 主要文件

| 文件 | 说明 |
| --- | --- |
| `types.py` | 定义 `Handler`、`HookContext`、`HookResult`。 |
| `registry.py` | 定义 `HookRegistry`，负责注册、排序、触发 handler。 |
| `loader.py` | 扫描并加载工作区 `plugins/` 下的外部 hook 插件。 |
| `../builtin/` | 保存内置 handler，并由 `auto_discover_and_register()` 根据 `PLUGIN_META` 自动注册。 |

## 当前 hook point

- `before_prompt_build`
- `before_step`
- `before_tool_call`
- `after_tool_call`
- `before_response`
- `after_llm_call`
- `on_task_end`
- `on_task_error`

## 执行规则

同一个 hook point 下的 handler 按 `priority` 从高到低运行。注册时按 `handler.name` 去重，同名 handler 会被替换，因此重复注册是安全的。

handler 返回 `None` 表示不介入；返回 `HookResult(modified=True)` 表示修改了上下文但继续执行；返回 `block`、`skip_step` 或 `action` 会停止当前 hook 链。

更多说明见 `docs/plugin-system.md`。
