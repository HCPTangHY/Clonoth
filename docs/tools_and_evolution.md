# 工具与自进化

## 1. 内置工具

当前 17 个内置工具，分为文件操作、命令执行、搜索、Skill 管理、MCP 管理、工具管理、系统管理和任务管理八类。

### 1.1 文件操作

#### list_dir

列出目录内容。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 否 | 相对路径。为空时列出工作区根目录。 |

返回目录下的文件和子目录列表。

#### read_file

读取文本文件。受 Policy 和审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 相对路径 |
| `start_line` | integer | 否 | 起始行号（从 1 开始） |
| `end_line` | integer | 否 | 结束行号（包含） |

不指定行号时读取整个文件。路径受 Policy 规则约束，`.env` 等文件会被硬拒绝。

#### write_file

写入文本文件。受 Policy 和审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 相对路径 |
| `content` | string | 是 | 文件内容 |

文件不存在时自动创建（含父目录）。写入 `config/nodes/**`、`engine/**` 等受保护路径时需要人类审批。

### 1.2 命令执行

#### execute_command

在工作区根目录下执行 shell 命令。受 Policy 和审批保护。子进程环境会自动剥离敏感变量（API key 等）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `command` | string | 是 | 要执行的命令 |
| `timeout_sec` | number | 否 | 超时秒数。默认由 `config/runtime.yaml` 的 `meta.execute_command.default_timeout_sec` 决定（默认 90 秒）。 |

命令执行前经过双层审核：cmd_reviewer 节点做 AI 语义审核，Supervisor Policy 做硬规则和人类审批。`rm -rf /`、`shutdown` 等命令会被硬拒绝。

返回 `exit_code`、`stdout`、`stderr`。输出超过 `meta.execute_command.max_output_chars`（默认 12000 字符）时截断。

命令执行期间，系统每 0.2 秒检查一次取消状态。如果 task 被取消，子进程会被立即终止。

### 1.3 搜索

#### search_in_files

在文件中搜索文本。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 搜索关键词 |
| `path` | string | 否 | 限定搜索的子目录（相对路径）。为空时搜索整个工作区。 |

返回匹配的文件列表和匹配行内容。受 `meta.search.max_file_size_bytes`（默认 3MB）和 `meta.search.max_matches`（默认 100）限制。

### 1.4 Skill 管理

Skill 是存放在 `skills/<name>/SKILL.md` 中的知识模块，会被注入到 AI 节点的 system prompt 中。

#### create_or_update_skill

创建或更新 Skill。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | Skill 名称。只允许 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`。 |
| `description` | string | 否 | Skill 描述 |
| `content` | string | 否 | 完整的 SKILL.md 内容。为空时自动生成模板。frontmatter 会被自动规范化。 |
| `enabled` | boolean | 否 | 是否启用。默认 true。 |

#### list_skills

列出所有 Skill。无参数。

返回每个 Skill 的 `name`、`description`、`enabled`、`path`。

#### delete_skill

删除 Skill 目录。受审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | Skill 名称 |

### 1.5 MCP 管理

MCP（Model Context Protocol）客户端定义存储在 `data/mcp_clients.yaml` 中。Engine 启动时自动连接已启用的客户端并注册其工具。

#### create_or_update_mcp_client

创建或更新 MCP 客户端配置。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 客户端标识 |
| `transport` | string | 是 | 传输方式。可选 `stdio`、`sse`、`streamable_http`。 |
| `description` | string | 否 | 描述 |
| `enabled` | boolean | 否 | 是否启用。默认 true。 |
| `command` | string | 否 | stdio 模式的可执行命令 |
| `args` | string[] | 否 | 命令参数列表 |
| `env` | object | 否 | 传递给子进程的环境变量 |
| `url` | string | 否 | SSE / HTTP 模式的服务端 URL |
| `headers` | object | 否 | HTTP 请求头 |

stdio 模式需要 `command`；sse 和 streamable_http 模式需要 `url`。

MCP 工具注册后的名称格式为 `mcp_{client_id}_{tool_name}`。

#### list_mcp_clients

列出所有 MCP 客户端配置。无参数。

#### delete_mcp_client

删除 MCP 客户端配置。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 客户端标识 |

### 1.6 工具管理

#### create_or_update_tool

创建或更新脚本工具。写入 `tools/<name>.py`。受审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 工具名称。只允许 `[A-Za-z_][A-Za-z0-9_]{0,63}`。不能与内置工具重名。 |
| `script` | string | 是 | Python 脚本主体。脚本中可以使用 `args`（从 stdin 解析的参数字典）、`output(result)`（输出结果）、`fail(error)`（报告错误）。 |
| `description` | string | 否 | 工具描述 |
| `input_schema` | object | 否 | 输入参数的 JSON Schema |
| `timeout_sec` | number | 否 | 执行超时秒数 |

生成的脚本文件包含输入解析和输出辅助函数。AI 只需编写核心逻辑。

#### reload_tools

重新扫描 `tools/` 目录并加载工具。无参数。

通常在 `create_or_update_tool` 之后调用，使新工具立即生效（无需重启 Engine）。

### 1.7 系统管理

#### request_restart

请求 Supervisor 重启 Engine 或整个系统。受审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target` | string | 是 | 重启目标。`engine` 只重启引擎进程；`all` 重启整个系统。 |
| `reason` | string | 否 | 重启原因 |

重启前会自动执行以下步骤：

1. 生成 git 状态和 diff 摘要
2. 通过 Supervisor Policy 发起 `restart` 审批
3. 审批通过后执行 git checkpoint 和重启

### 1.8 定时调度

#### create_schedule

创建或更新定时调度任务。调度定义存储在 `data/schedules.yaml` 中。到达指定时间时，系统会自动注入一条 inbound 消息。受审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 调度任务标识。只允许 `[A-Za-z_][A-Za-z0-9_-]{0,63}`。 |
| `cron` | string | 是 | 5 字段 cron 表达式：minute hour day month weekday（UTC）。 |
| `text` | string | 是 | 触发时注入的消息文本。 |
| `conversation_key` | string | 否 | 会话标识。默认 `scheduler:{id}`。 |
| `workflow_id` | string | 否 | 指定使用的 workflow。为空时使用默认 workflow。 |
| `enabled` | boolean | 否 | 是否启用。默认 true。 |
| `once` | boolean | 否 | 是否只触发一次。触发后自动删除。默认 false。 |

#### list_schedules

列出所有定时调度任务。无参数。

#### delete_schedule

删除定时调度任务。受审批保护。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 调度任务标识 |

### 1.9 任务管理

#### cancel_active_tasks

取消当前会话中所有活跃的下游 task。无参数。

入口节点在收到新用户消息后，如果判断旧任务不再需要，调用此工具取消。取消时会自动保留当前调用链上的 task，不会把自己取消掉。

---

## 2. 工具执行模型

### 2.1 task 化执行

所有工具调用都以独立的 tool task 形式执行。流程如下：

1. AI 节点发出工具调用请求
2. AI 节点返回 `yield_tool` 结果，保存当前上下文快照到 `data/node_contexts/{session_id}/`
3. Supervisor 为每个工具调用创建独立的 tool task
4. Engine worker 领取 tool task 并执行
5. 同一批次的所有 tool task 完成后，Supervisor 创建恢复 task
6. 恢复 task 加载之前保存的上下文快照，把工具结果注入 AI 对话历史，继续执行

### 2.2 可中断性

所有工具在执行期间每 0.2 秒检查一次取消状态。以下操作均可被中断：

- LLM 调用（流式和非流式）：每 0.3 秒检查
- 子进程命令执行：每 0.2 秒检查，取消时 kill 子进程
- 脚本工具执行：每 0.2 秒检查，取消时 kill 子进程
- 审批等待：每次轮询间隔检查

### 2.3 上下文快照

节点在执行过程中会将对话历史保存为 JSON 快照文件，存储在 `data/node_contexts/{session_id}/` 下。调度线程每 30 分钟清理超过 1 小时的旧快照，活跃 task 引用的快照不会被清理。

---

## 3. 脚本工具

`tools/*.py` 下的脚本工具以独立子进程运行。

### 3.1 协议

- 输入：工具参数作为 JSON 从 stdin 读入
- 输出：结果作为 JSON 写到 stdout
- 环境：敏感变量（API key 等）被自动剥离
- 超时：可在脚本中通过 `TIMEOUT_SEC` 变量配置

### 3.2 注册

Engine 启动时扫描 `tools/` 目录，通过 AST 解析每个 `.py` 文件中的 `SPEC` 字典获取工具声明。不执行、不 import 脚本代码。

脚本文件必须包含以下结构：

```python
SPEC = {
    "name": "my_tool",
    "description": "工具描述",
    "input_schema": {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "参数说明"},
        },
        "required": ["param1"],
    },
}
TIMEOUT_SEC = 60  # 可选

if __name__ == "__main__":
    # 核心逻辑
    pass
```

### 3.3 安全隔离

- 脚本在独立子进程中运行，不在主 Engine 进程内 import 或 eval
- 子进程环境自动剥离 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等敏感变量
- 子进程工作目录为工作区根目录
- 执行期间每 0.2 秒检查取消状态，取消时直接 kill 子进程

---

## 4. 安全模型

安全分为四层：

1. **AI 审核节点**（`cmd_reviewer`）：在节点图中对命令做语义审核
2. **Supervisor Policy**：硬规则拒绝 + 人类审批
3. **环境隔离**：子进程执行时剥离敏感环境变量
4. **路径边界**：文件读写限制在 workspace 和 `extra_roots` 内

AI 审核不能替代人类审批。

### 4.1 SafetyLevel

- `auto`：自动允许
- `approval_required`：需要人类审批
- `deny`：硬拒绝

### 4.2 文件保护

| 路径 | 策略 | 原因 |
|---|---|---|
| `engine/**`、`supervisor/**`、`toolbox/**`、`providers/**`、`shell/**` | 需要审批 | 核心源码 |
| `clonoth_runtime.py`、`main.py` | 需要审批 | 入口和运行时库 |
| `config/nodes/**`、`config/workflows/**`、`config/prompt_packs/**` | 需要审批 | 节点图配置 |
| `config/model_routing.yaml` | 需要审批 | 模型路由 |
| `tools/**` | 需要审批 | 脚本工具 |
| `data/config.yaml` | 需要审批 | 运行时配置 |
| `data/policy.yaml` | 硬拒绝 | 安全策略 |
| `data/events.jsonl` | 硬拒绝 | 事件日志 |
| `.env`、`**/.env` | 硬拒绝 | 密钥文件 |
| `config/runtime.yaml` | 自动允许 | 调优参数 |

### 4.3 extra_roots

在 `data/policy.yaml` 中声明额外允许访问的路径：

```yaml
extra_roots:
  - "/www/wwwroot"
```

对 `extra_roots` 下的路径，默认策略收紧为 `approval_required`。

### 4.4 命令硬拒绝

以下命令模式被硬拒绝：`rm -rf /`、`rm -rf ~`、`rm -rf *`、`format`、`mkfs`、`fdisk`、`dd if=/dev/zero`、`shutdown`、`reboot`。

### 4.5 审批守卫

所有需要 Policy 判定的工具内部统一使用 `_request_guard` 函数。流程如下：

1. 向 Supervisor 发送 `POST /v1/ops/request`，获取 Policy 判定
2. 如果判定为 `deny`，立即返回错误
3. 如果判定为 `approval_required`，轮询等待人类审批结果
4. 如果判定为 `auto`，直接执行

等待审批期间也会检查取消状态。如果 task 被取消，审批等待会立即终止。

---

## 5. 自进化

AI 可以在人类审批下修改系统自身。

### 5.1 能做的事

| 操作 | 途径 | 审批 |
|---|---|---|
| 创建新工具 | `create_or_update_tool` | 需要 |
| 修改节点定义 | `write_file` → `config/nodes/*.yaml` | 需要 |
| 修改工作流 | `write_file` → `config/workflows/*.yaml` | 需要 |
| 修改提示词 | `write_file` → `config/prompt_packs/**` | 需要 |
| 修改模型路由 | `write_file` → `config/model_routing.yaml` | 需要 |
| 修改引擎源码 | `write_file` → `engine/**` 等 | 需要 |
| 接入 MCP 服务 | `create_or_update_mcp_client` | 不需要 |
| 调整运行参数 | `write_file` → `config/runtime.yaml` | 不需要 |
| 创建定时任务 | `create_schedule` | 需要 |
| 取消下游任务 | `cancel_active_tasks` | 不需要 |
| 重启使改动生效 | `request_restart` | 需要 |

### 5.2 不能做的事

- 修改安全策略（`data/policy.yaml`）：硬拒绝
- 修改事件日志（`data/events.jsonl`）：硬拒绝
- 读写密钥文件（`.env`）：硬拒绝

### 5.3 典型流程：创建新工具

1. AI 调用 `create_or_update_tool`，生成 `tools/xxx.py`
2. Policy 拦截，弹出审批
3. 人类审查脚本内容，同意
4. AI 调用 `reload_tools` 或 `request_restart` 使工具生效
5. AI 后续可以直接调用新工具

### 5.4 典型流程：修改节点行为

1. AI 调用 `write_file` 修改 `config/nodes/bootstrap.executor.yaml`
2. Policy 拦截，弹出审批
3. 人类审查 YAML 内容，同意
4. AI 调用 `request_restart` 重启 Engine
5. Policy 拦截，弹出审批
6. 人类同意重启
7. Engine 重启后按新定义执行

---

## 6. Tool Trace 与 Artifacts

工具调用完成后，系统保留：

- 摘要信息（注入 AI 上下文）
- 完整输出（超出 `engine.tool_trace.max_inline_chars` 阈值时写入 artifact 文件）
- 标准化的 tool trace 文本块

artifact 文件保存在 `data/artifacts/{run_id}/` 下，可供人工复盘。

---

## 7. runtime.yaml 中的相关配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `engine.max_steps` | 32 | AI 节点单次执行的最大步数 |
| `engine.streaming` | true | 入口节点是否使用流式输出 |
| `engine.poll_interval_sec` | 1.0 | worker 轮询 task 队列的间隔 |
| `meta.execute_command.default_timeout_sec` | 90 | 命令执行默认超时 |
| `meta.execute_command.max_output_chars` | 12000 | 命令输出最大字符数 |
| `meta.git.diff_max_chars` | 600000 | restart 时 git diff 最大字符数 |
| `meta.search.max_file_size_bytes` | 3000000 | 搜索时跳过的文件大小上限 |
| `meta.search.max_matches` | 100 | 搜索最大匹配数 |
| `engine.tool_trace.max_inline_chars` | 8000 | 工具输出内联显示的最大字符数 |
| `tools.command.default_timeout_sec` | 60 | 脚本工具默认超时 |
| `supervisor.process_manager.engine_workers` | 2 | Engine worker 进程数 |
