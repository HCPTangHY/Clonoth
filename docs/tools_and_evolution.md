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

- `hybrid`（默认）：纯文本作为隐式 finish，不要求调用 finish 工具。
- `tool_only`：必须通过 finish 工具提交回复，纯文本被拒绝并重试。

## 工具来源

### 内置工具

由 `toolbox/builtins/` 提供，在 `toolbox/registry.py` 中注册。当前内置工具：

| 工具 | 说明 |
|------|------|
| `list_dir` | 列出目录内容 |
| `read_file` | 读取文件；文本每次最多 500 行、64 KB，超过 10 MB 默认预览前 50 行，也可指定范围读取 |
| `write_file` | 写入文件 |
| `apply_diff` | 精确搜索替换；`start_line` 使用本批修改前的原文件行号，自动补偿前序修改 |
| `execute_command` | 执行 shell 命令，默认超时 90 秒，可通过配置或参数覆盖 |
| `grep` | 搜索或替换文件内容；支持正则、glob 和 content/files_only/count 输出模式 |
| `set_workspace` | 切换活跃工作区 |
| `manage_skill` | `list` 查看、`upsert` 创建或更新、`delete` 删除 Skill |
| `manage_memory` | `list` 查看、`upsert` 保存、`delete` 删除记忆；指定 ID 可读取完整条目 |
| `manage_mcp_client` | `list` 查看、`upsert` 创建或更新、`delete` 删除 MCP 客户端 |
| `manage_tool` | `upsert` 创建或更新外部脚本，`reload` 热重载；可声明 pure 和 async_mode |
| `request_restart` | 请求重启 Engine 或全系统 |
| `manage_schedule` | `list` 查看、`upsert` 创建或局部更新、`delete` 删除定时任务 |
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
| `manage_workers` | `list` 查看、`message` 追加指令、`preempt` 软中断、`cancel` 硬取消当前会话的下游任务 |

`preempt` 不带消息时请求任务停止，带 `message` 时按追加指令继续处理，与生产接口一致。`cancel` 可以指定任务或节点；省略目标时取消当前会话的活跃下游任务，但不取消调用任务本身。指定了不存在或不明确的目标时返回错误，不扩大取消范围。

### 自定义工具

位于 `tools/*.py`，以独立子进程运行。协议：stdin 读 JSON 参数，stdout 写 JSON 结果。通过 AST 解析 `SPEC` 字典注册，不 import 脚本代码。结构化 JSON 在文本截断前解析，避免较大的结果被截成无效 JSON。

脚本环境提供 `CLONOTH_WORKSPACE`（安装根目录）、`CLONOTH_SESSION_ID`、`CLONOTH_TASK_ID` 和 `CLONOTH_SUPERVISOR_URL`。管理脚本通过 `CLONOTH_ADMIN_TOKEN` 或本机 `data/.admin_token` 认证，不得将生产凭据写入脚本。

本次仅迁移 `clear_context.py` 和 `clonoth_debug.py` 两个管理脚本，其余本地脚本保持原状。清理上下文只应在用户明确要求时执行；脚本会排除当前调用任务，仍会终止其他相关任务。

统一管理工具不提供旧名称别名。节点工具名单应使用 `manage_tool`、`manage_schedule`、`manage_workers`、`manage_skill`、`manage_memory`、`manage_mcp_client`。已有部署还需检查自定义节点和策略文件中的旧工具名，不能只替换源码。

真实工具与控制工具按模型声明顺序处理。连续的显式异步工具可以分组启动，普通工具仍按顺序执行。中断时为未执行的原生工具调用补充结果，避免恢复后的请求出现配对错误。

### MCP 工具

MCP 客户端配置在 `data/mcp_clients.yaml`，支持 `stdio`、`sse`、`streamable_http` 三种传输。注册后工具名格式为 `mcp_{client_id}_{tool_name}`。

同一 Worker 事件循环中的节点启动和恢复会复用已有 MCP 桥及首次发现任务，不会重新删除工具或关闭 stdio 会话。配置变化由现有监视器按客户端刷新；显式卸载、代码重载或失效的桥才进行资源交接。桥、stdio 会话池和 schema 缓存由事件循环持有，完整重新导入插件包后仍可完成旧资源清理。首次部署这一状态存储变更需重启 Worker，不能仅热重载旧版本。

刷新仍执行工具发现；客户端配置、规范化工具定义及包装实现未变化时，保留原注册和函数身份，避免修改其他客户端导致待审批调用失效。实际定义或实现变化仍使旧审批失效。插件清理同时核对所属事件循环和 generation，不会因两个循环的 generation 相同而关闭另一个循环的桥。

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

所有通过 `ToolRegistry.execute()` 执行的工具（内置、自定义、MCP）都会经过 Supervisor policy 的 `evaluate_tool` 检查。默认策略为 `approval_required`；已有内部文件或命令审批的工具可通过具体规则免除重复的工具级审批。可通过 `data/policy.yaml` 的 `tools` 配置覆盖。

审批只对应当次调用的工具实现、定义和参数。等待期间工具被移除，返回 `tool_unavailable`；实现或定义被替换，返回 `tool_changed`。这类情况是工具执行错误，不是假定用户拒绝，也不会自动重试或调用已卸载的旧实现。同步与异步预授权共用这项检查，调用结束或授权失败后清理临时授权记录。定义与参数按规范化 JSON 比较，区分 `1`、`1.0`、`true`，忽略对象键顺序；不能序列化的值以及 NaN、Infinity 在授权前返回 `tool_invalid_json`，授权期间出现则按变更拒绝。

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
| 创建工具 | `manage_tool(action="upsert")` | 需要 |
| 修改节点定义 | `write_file` → `config/nodes/*.yaml` | 需要 |
| 修改引擎源码 | `write_file` → `engine/**` | 需要 |
| 接入 MCP 服务 | `manage_mcp_client(action="upsert")` | 按配置文件写权限审批 |
| 调整运行参数 | `write_file` → `config/runtime.yaml` | 不需要 |
| 创建定时任务 | `manage_schedule(action="upsert")` | 需要 |
| 重启 | `request_restart` | 需要 |

安全策略（`data/policy.yaml`）和事件日志（`data/events.jsonl`）始终硬拒绝写入。`request_restart` 的 Git 检查点只提交已跟踪文件，不自动纳入未跟踪文件。
