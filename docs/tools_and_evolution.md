# 工具系统

本文说明 Clonoth 的工具调用模式、工具来源、审批机制和自修改能力。

## 工具调用模式

工具调用格式由节点配置中的 `tool_mode` 决定，回退到 `config/runtime.yaml` 中的全局配置。

### native

原生工具调用模式。工具定义通过 provider 的 tools 参数传入模型，模型返回结构化 `tool_calls`，结果以 `role=tool` 写回历史。适合支持原生 function calling 的 provider。

### fake-native

兼容模式。工具定义仍通过 tools 参数传入，但历史记录中的工具调用和结果会转换为文本形式。适合保留旧会话兼容性。

### json

文本内嵌模式。工具定义写入 system prompt，模型以 `<<<TOOL_CALL>>>` 标记输出调用。适合不支持原生工具调用的模型。

## 输出模式

节点配置中的 `output_mode` 决定回复方式：

- `hybrid`（默认）：纯文本即为隐式 finish，无需调用 finish 工具。同时保留 finish 工具用于需要附件的场景。
- `tool_only`：必须通过 finish 工具提交回复，纯文本被拒绝并重试。

## 工具来源

### 内置工具

由 `toolbox/builtins/` 提供，在 `toolbox/registry.py` 中注册。当前内置工具：

| 工具 | 说明 |
|------|------|
| `list_dir` | 列出目录内容 |
| `read_file` | 读取文件（支持行号范围） |
| `write_file` | 写入文件 |
| `apply_diff` | 搜索替换式修改文件 |
| `execute_command` | 执行 shell 命令 |
| `search_in_files` | 搜索（和替换）文件内容 |
| `set_workspace` | 切换活跃工作区 |
| `create_or_update_skill` | 创建或更新 Skill |
| `list_skills` / `delete_skill` | 列出 / 删除 Skill |
| `save_memory` / `list_memories` / `delete_memory` | 记忆管理 |
| `create_or_update_mcp_client` | 创建或更新 MCP 客户端 |
| `list_mcp_clients` / `delete_mcp_client` | 列出 / 删除 MCP 客户端 |
| `create_or_update_tool` / `reload_tools` | 创建工具 / 热重载 |
| `request_restart` | 请求重启 Engine 或全系统 |
| `create_schedule` / `list_schedules` / `delete_schedule` | 定时调度管理 |
| `cancel_active_tasks` | 取消当前会话活跃任务 |
| `get_context_window` | 查看当前上下文窗口使用情况 |

### 伪工具（Pseudo Tools）

由 Engine 内部处理，不经过工具注册表：

| 伪工具 | 说明 |
|--------|------|
| `finish` | 提交最终回复 |
| `ask` | 向上游提问 |
| `intermediate_reply` | 发送中间进度消息 |
| `switch_node` | 切换到其他节点 |
| `compact_context` | 触发上下文压缩 |
| `preempt_task` | 抢占下游任务 |

### 自定义工具

位于 `tools/*.py`，以独立子进程运行。协议：stdin 读 JSON 参数，stdout 写 JSON 结果。通过 AST 解析 `SPEC` 字典注册，不 import 脚本代码。

### MCP 工具

MCP 客户端配置在 `data/mcp_clients.yaml`，支持 `stdio`、`sse`、`streamable_http` 三种传输。注册后工具名格式为 `mcp_{client_id}_{tool_name}`。

## 审批机制

工具操作通过 `request_guard` 向 Supervisor 请求策略判定：

| 安全等级 | 行为 |
|----------|------|
| `auto` | 直接执行 |
| `approval_required` | 等待人类审批 |
| `deny` | 拒绝执行 |

### trust_level

路径操作会经过 `classify_path` 分类：

| trust_level | 含义 | 默认行为 |
|-------------|------|----------|
| `workspace` | 活跃工作区或 `data/` 目录 | 按策略规则，通常自动放行 |
| `trusted` | `workspace_root`（安装目录）或 `extra_roots` | 核心源码需审批 |
| `external` | 以上都不是 | 强制审批 |
| 空字符串 | 无路径操作（如 `execute_command`） | 回落到用户偏好 |

### 统一工具级审批

所有通过 `ToolRegistry.execute()` 执行的工具（内置、自定义、MCP）都会经过 Supervisor policy 的 `evaluate_tool` 检查。默认策略对所有工具返回 `auto`，可通过 `data/policy.yaml` 的 `tools` 配置覆盖。

### 文件保护规则

| 路径 | 策略 |
|------|------|
| `.env` | 硬拒绝 |
| `data/policy.yaml`、`data/events.jsonl` | 硬拒绝 |
| `engine/**`、`supervisor/**`、`toolbox/**`、`providers/**` | 需要审批 |
| `config/nodes/**`、`tools/**`、`data/config.yaml` | 需要审批 |
| `config/runtime.yaml` | 自动放行 |
| `data/` 子目录（attachments、memory、conversations） | 自动放行 |

### 命令硬拒绝

`rm -rf /`、`mkfs`、`shutdown`、`reboot` 等高风险命令模式被硬拒绝。

## 工作区

`set_workspace` 工具可以切换活跃工作区。切换后：

- `execute_command` 的 cwd 变为工作区路径
- `read_file`、`write_file` 等工具的路径解析基于工作区
- 工作区内路径的 `trust_level` 为 `workspace`
- 记忆按工作区隔离（`data/memory/{node_id}/@{workspace}/`）
- 工作区绑定到 session，跨 turn 持久化

工作区注册表保存在 `data/workspaces.yaml`，session 级绑定保存在 `data/sessions.json`。

## 自修改能力

AI 可以在人类审批下修改系统自身：

| 操作 | 途径 | 审批 |
|------|------|------|
| 创建工具 | `create_or_update_tool` | 需要 |
| 修改节点定义 | `write_file` → `config/nodes/*.yaml` | 需要 |
| 修改引擎源码 | `write_file` → `engine/**` | 需要 |
| 接入 MCP 服务 | `create_or_update_mcp_client` | 不需要 |
| 调整运行参数 | `write_file` → `config/runtime.yaml` | 不需要 |
| 创建定时任务 | `create_schedule` | 需要 |
| 重启 | `request_restart` | 需要 |

安全策略（`data/policy.yaml`）和事件日志（`data/events.jsonl`）始终硬拒绝写入。
