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

### 会话待办（IDE 插件）

`plugins/ide/` 注册 `read_todos` 和 `update_todos`。Todo 是持久工作记录，不负责调度任务，也不代表 Goal 已通过验收。

- 清单只绑定 session，不要求工作区名称或文件目录；切换工作区不切换清单。
- 主会话的临时入口分支共用所属主会话清单。普通子代理和 worker 使用自己的子会话清单；恢复或 accumulate 复用同一清单，新的 fresh/fork 子会话不复制父清单。
- 服务端根据任务记录解析归属，同步子任务优先使用 `input.child_session_id`，不能直接使用仍指向父会话的 `task.session_id`。
- `read_todos` 返回 `schema_version: 2`、`session_id`、`revision` 和 `items`。`update_todos` 使用 `expected_session_id`、`expected_revision`、`changes`、`remove_ids`、`order`，不再接受 `workspace_path`。`expected_session_id` 只核对当前清单身份，不允许选择其他会话；这也防止 fork 子任务误用父清单版本。
- 摘要在 `before_llm_call` 的请求副本末尾注入，读取当前任务所属清单；旧 Todo 摘要仅从请求副本中移除，不修改原始历史或 JSONL。空清单也提供创建所需身份和 revision。读取失败提示重新读取，不按空清单处理。
- Web 聊天顶部面板仍为只读，主、子会话均可显示自己的清单。

新清单位于 `data/ide/todos/sessions/<sha256(session_id)>.json`，Supervisor 在会话锁内执行身份校验、revision 比较和原子写入。首次读取无 v2 文件的 session 时，扫描旧目录根部，将唯一匹配的 v1 清单复制为 v2；事项 ID、状态、说明、revision 和时间记录保持不变，旧文件保留。多个旧清单对应同一 session 时返回 409，不自动合并。旧目录存在无法解析、无法确认归属的文件时，未迁移清单的读取会失败，需要管理员检查；已有 v2 清单不受影响。

重新启用不会放宽节点权限或工具审批。节点使用 allowlist 时需要包含对应工具；真实工具调用继续遵守部署中的 `data/policy.yaml` 和会话审批规则。默认未匹配的工具需要审批：主任务等待审批，未预授权的子任务会直接拒绝。因此部署前需要确认父会话可继承的审批规则是否已允许这两个工具；必要时由管理员仅为这两个工具配置允许，不改变其他工具的规则，也不通过插件绕过。部署需同步 IDE 插件和 `engine/attachments.py`，让 Supervisor 和 Engine 重新加载，并刷新 Web 页面；旧版本仅更新 Engine 或仅更新前端均不足以兼容新协议。

离线验证：

```bash
TMPDIR="$PWD/data/temp" python3.11 -B -m pytest tests/test_plugins_migration.py tests/test_todo_session_store.py tests/test_todo_engine.py
node --experimental-vm-modules tests/test_todo_frontend.cjs
```

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
