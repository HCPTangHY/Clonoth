# 工具系统

本文说明 Clonoth 当前仓库中的工具调用模式、工具来源、自定义工具创建方式和审批机制。本文只说明结构和使用方法，不包含任何实际凭据。

## 工具调用三层模式

工具调用格式由 `engine.tool_mode` 或节点配置中的 `tool_mode` 决定。有效值为 `native`、`fake-native`、`json`。旧写法 `fake_native` 会被归一化为 `fake-native`。节点配置优先于 `config/runtime.yaml` 中的全局配置。

当前仓库的 `config/runtime.yaml` 将全局默认值设为 `json`。如果配置缺失或非法，代码会回退到 `fake-native`，以保护旧节点和旧历史记录。

### native

`native` 是真正的原生工具调用模式。

- 工具定义通过 provider 的 tools 参数传入模型。
- 模型返回结构化 `tool_calls`。
- 工具结果以 `role=tool`、`tool_call_id` 的形式写回历史。
- 适合支持原生 function calling 或 tool calling 的 provider。

### fake-native

`fake-native` 是旧版兼容模式。

- 工具定义仍通过 provider 的 tools 参数传入模型。
- 工具调用从 provider 返回的 `tool_calls` 中解析。
- 历史记录中的工具调用和工具结果会被转换为文本形式，并作为 `role=user` 消息继续提供给模型。
- 适合保留旧会话兼容性，避免旧历史被误解释为真正的原生工具格式。

### json

`json` 是文本内嵌工具调用模式。

- 工具定义会写入 system prompt，不向 provider 传 tools 参数。
- 模型需要输出如下格式：

```text
<<<TOOL_CALL>>>
{"name": "工具名称", "arguments": {}}
<<<END_TOOL_CALL>>>
```

- 引擎从文本中解析工具调用。
- 工具结果仍以文本形式返回给模型。
- 适合不支持原生工具调用的模型，或需要绕过 provider 工具调用限制的场景。

无论使用哪种模式，最终回复都应通过 `finish` 工具提交，中间进度应通过 `reply` 工具发送。

## 工具来源

Clonoth 中的工具主要来自三类来源：内置工具、自定义工具和 MCP 工具。

### 内置工具

内置工具由 `toolbox/builtins/` 提供，并在 `toolbox/registry.py` 中注册。它们包括文件读取、文件写入、搜索、命令执行、Skill 管理、MCP 客户端管理、计划任务、记忆、上下文窗口和任务控制等能力。

这些工具名称是保留名称，自定义工具不能覆盖。例如：

- `list_dir`
- `read_file`
- `write_file`
- `apply_diff`
- `execute_command`
- `search_in_files`
- `create_or_update_skill`
- `create_or_update_mcp_client`
- `create_or_update_tool`
- `save_memory`
- `get_context_window`

### 自定义工具

自定义工具位于 `tools/` 目录。当前仓库中有以下自定义工具文件：

- `clear_context.py`
- `clonoth_debug.py`
- `convert_worldbook.py`
- `discord_manage.py`
- `read_image.py`
- `scan_github_keys.py`

注册器会扫描 `tools/*.py`，跳过 `__init__.py`。每个工具文件需要提供字面量 `SPEC`，可选提供 `TIMEOUT_SEC`。注册器只用 AST 读取 `SPEC`，注册时不会导入或执行工具文件。

工具执行时会以独立 Python 子进程运行：

- 标准输入为 JSON 参数。
- 标准输出为 JSON 结果。
- 常见敏感环境变量会被剥离。
- 超时时间来自工具文件中的 `TIMEOUT_SEC`，或运行时默认配置。

### MCP 工具

MCP 客户端配置存放在 `data/mcp_clients.yaml`。支持三种 transport：

- `stdio`
- `sse`
- `streamable_http`

启用的 MCP 客户端会被扫描，客户端暴露的工具会注册为一等工具，命名格式为：

```text
mcp_<client_id>_<tool_name>
```

MCP 配置中的 `env` 和 `headers` 支持 `${VAR}` 或 `$ENV{VAR}` 环境变量引用。不要把 API key、访问令牌或其他敏感信息明文写入仓库文件。

## 自定义工具创建方式

推荐使用内置工具 `create_or_update_tool` 创建或更新自定义工具。它会生成 `tools/<name>.py`，写入 `SPEC` 和执行包装代码，然后调用 registry reload。

基本参数如下：

```yaml
name: my_tool
description: 工具说明
input_schema:
  type: object
  properties:
    text:
      type: string
      description: 输入文本
  required: [text]
script: |
  result = {"ok": True, "text": args["text"]}
  output(result)
timeout_sec: 60
```

工具名必须匹配：

```text
^[A-Za-z_][A-Za-z0-9_]{0,63}$
```

工具脚本中可用：

- `args`：从标准输入读取的参数字典。
- `output(result)`：输出 JSON 并正常退出。
- `fail(error)`：输出错误 JSON 并以失败状态退出。

也可以手工创建 `tools/<name>.py`，但必须保持同样的协议：文件中有 `SPEC`，运行时从标准输入读取 JSON，并向标准输出写 JSON。

## 工具审批机制

部分工具操作会经过 Supervisor 策略和人工审批。工具层通过 `request_guard` 请求 Supervisor 的 `/v1/ops/request`，Supervisor 返回三种安全等级：

- `auto`：直接允许。
- `approval_required`：创建审批请求并等待用户允许或拒绝。
- `deny`：直接拒绝。

审批请求会写入事件流，状态为 pending。任务会等待审批结果；允许后继续执行，拒绝后返回错误。

默认策略位于 `supervisor/policy.py`，实际运行时可由 `data/policy.yaml` 覆盖。默认规则包括：

- 禁止读取或写入 `.env`。
- 写入 `tools/**`、`config/nodes/**`、`data/config.yaml`、`data/schedules.yaml` 和核心源码目录需要审批。
- `execute_command` 默认需要审批。
- 明确拒绝高风险命令模式，例如 `rm -rf /`、`mkfs`、`shutdown`、`reboot`。
- 工作区外部路径默认需要审批。

`create_or_update_tool` 最终通过 `write_file` 写入 `tools/**`，因此也会触发对应审批规则。`execute_command` 和脚本工具运行子进程时会剥离常见 API key 环境变量，以降低意外泄露风险。