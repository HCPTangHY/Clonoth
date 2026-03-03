# Clonoth 工具系统与自进化机制（详细）

本文档面向 **Clonoth 开发者 / 运维 / Prompt 迭代者**，解释：

- 工具系统的分层（Meta Tools vs Declarative Tools）
- Policy/Approval 如何成为“Root of Trust”的关键安全边界
- Kernel 的 tool trace / artifacts 如何为“可追溯推理 + 可回放”服务
- 自进化（写工具、改代码、重启、回滚）的完整闭环与限制

> 代码参考：
> - `kernel/meta_tools.py`
> - `kernel/registry.py`
> - `kernel/worker.py`
> - `supervisor/policy.py`, `data/policy.yaml`
> - `supervisor/upgrade.py`, `supervisor/process_manager.py`

---

## 1. 设计原则：可进化系统的“能力分层”

Clonoth 将“执行危险操作”的能力收敛到 **Kernel + Supervisor**：

- **Supervisor**：Root of Trust（策略、审批、事件日志、进程管理、回滚 watchdog）
- **Kernel**：执行引擎（多步推理 + 工具调用）
- **Shell**：
  - Orchestrator：决定「直接回复」还是「创建 task」
  - Responder：对 Kernel 结果做最终总结输出（面向用户）

工具系统的目标不是“无限制执行代码”，而是：

1) 让 AI 能“做事”（读写文件 / 执行命令 / 生成工具）；
2) 同时让人类能**审计**、能**拒绝**、能**回滚**；
3) 保持系统在崩溃、重启、重试时仍然**可恢复**。

---

## 2. 工具的两大类：Meta Tools 与 Declarative Tools

### 2.1 Meta Tools（内置元工具）

Meta Tools 是框架手写、受控、可审计的“底座能力”，主要位于：`kernel/meta_tools.py`。

当前内置工具（由 `kernel/registry.py` 注册）包括：

- **Workspace 只读**
  - `list_dir({path})`
  - `read_file({path, start_line?, end_line?})`
  - `search_in_files({query, path?})`

- **Workspace 写入**
  - `write_file({path, content})`

- **命令执行（高风险）**
  - `execute_command({command, timeout_sec?})`

- **自进化核心**
  - `create_or_update_tool({name, description?, input_schema?, command/commands, timeout_sec?})`
  - `reload_tools({})`
  - `request_restart({target, reason?})`

> 安全要点：
> - Meta Tools 会先调用 `KernelContext.request_op()`，由 Supervisor 做 Policy 判断（auto / approval_required / deny）。
> - `execute_command` 的子进程环境变量会做“敏感变量剥离”（例如 `*_API_KEY`）以减少误泄露风险（`kernel/meta_tools.py::_safe_subprocess_env`）。

### 2.2 Declarative Tools（声明式命令工具，Tool v2）

Declarative Tools 是 AI 调用 `create_or_update_tool` 自动生成的工具，存放于 `tools/*.py`。

**关键安全机制（Tool v2）**：

- Kernel **不会 import/执行** `tools/` 中任何 Python 代码。
- Kernel 只用 `ast.parse + ast.literal_eval` 解析文件中的字面量变量：
  - `SPEC`（工具描述/参数 schema）
  - `COMMANDS` 或 `COMMAND`
  - `TIMEOUT_SEC`（可选）

因此：AI 无法在 tool 文件里塞入 Python 运行时代码来“绕过 Policy”。

#### 2.2.1 Declarative Tool 文件格式

一个最小工具文件示例：

```python
# tools/my_tool.py

SPEC = {
  "name": "my_tool",
  "description": "Run build",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"}
    },
    "required": ["path"]
  }
}

COMMANDS = [
  "npm run build --prefix {path}",
  "npm test --prefix {path}",
]

TIMEOUT_SEC = 60.0
```

#### 2.2.2 命令模板与参数注入

- `COMMANDS` 中的字符串使用 Python `str.format(**args)` 做模板渲染。
- 如果缺少参数，会返回：`{"ok": False, "error": "missing argument ..."}`。

#### 2.2.3 运行与错误语义

Declarative tool 的执行逻辑（见 `kernel/registry.py::_make_command_tool`）：

- 对 `COMMANDS` 逐条调用 Meta Tool `execute_command`
- 任意一步失败会中断并返回：
  - `ok=false`
  - `steps=[{command, result}, ...]`

---

## 3. Policy / Approval：Root of Trust 如何控制工具权限

### 3.1 三档策略

Supervisor 的策略引擎：`supervisor/policy.py`。

对每个 op（read/write/command/restart）给出三档决策：

- `auto`：自动允许
- `approval_required`：需要人类审批
- `deny`：硬拒绝

默认策略文件：`data/policy.yaml`（首次启动自动生成，可由人类修改；注意该文件默认被 deny 写入，防止 AI 修改规则本身）。

### 3.2 文件读写规则（glob）

`read_file` / `write_file` 使用 workspace 相对路径的 glob 匹配：

- `.env` / `**/.env` 默认 deny（防止读写 dotenv secrets）
- `data/events.jsonl` 默认 deny（事件日志 append-only）
- `data/policy.yaml` 默认 deny（策略文件人类专管）
- `tools/**` 默认 approval_required（写工具必须审批）
- `config/prompts/**` 默认 approval_required（prompt 被视为高敏）

### 3.3 execute_command 规则（regex allow/deny）

- 默认 `approval_required`
- `auto_patterns`：例如 `git status` / `python -m compileall` 等可自动放行
- `deny_patterns`：例如 `rm -rf`、`curl/wget`、读取环境变量等高风险命令直接拒绝

---

## 4. Tool Trace & Artifacts：让推理“可追溯、可复盘”

### 4.1 tool_result 事件（摘要 + ref）

Kernel 执行每次工具调用后会：

1) 把完整 raw 结果写入：`data/artifacts/{task_id}/...`（用于调试/复盘）
2) 向 Supervisor 发送 `tool_result` 事件：
   - `summary`（简短）
   - `ref`（artifact 相对路径）

### 4.2 [CLONOTH_TOOL_TRACE] 块（喂给模型、也可用于摘要）

Supervisor 在 `session_messages()` 中，会把 `tool_result` 事件转换成一段标准化的 trace 文本（role=assistant）：

- 以 `[CLONOTH_TOOL_TRACE v1] ... [/CLONOTH_TOOL_TRACE]` 包裹
- 包含 `TASK: <task_id>`、工具调用名、参数摘要、结果摘要、artifact ref

用途：
- Kernel 的后续推理可以把这些 trace 当“可读观察”继续使用
- Shell Orchestrator 会剥离这些 trace，避免干扰路由判断
- Shell Responder 会收集这些 trace 来生成最终用户摘要（你现在要求的“task 结束必须过 chat AI”）

---

## 5. 自进化闭环：从改代码到重启回滚

### 5.1 自进化的两条路径

1) **生成/更新工具**（无须重启）
   - Kernel 调用 `create_or_update_tool` 写入 `tools/*.py`
   - 然后 `reload_tools` 生效

2) **修改核心代码**（通常需要重启）
   - Kernel 调用 `write_file` 修改 `shell/`, `kernel/`, `supervisor/` 等
   - 通过 `request_restart(target=kernel/shell/all)` 触发重启

### 5.2 request_restart 的安全流程

`request_restart`（见 `kernel/meta_tools.py`）会：

1) 生成 git diff 摘要（若 workspace 是 git repo）
2) 通过 Supervisor Policy 发起 `restart` 审批（通常 approval_required）
3) 审批通过后：
   - 尝试 `git commit` 做 checkpoint（便于回滚）
   - 调用 `POST /v1/admin/restart`

> 建议：务必让 workspace 处于 git 管理之下（`git init && git commit`），否则 checkpoint 与 watchdog 回滚能力会降级。

### 5.3 UpgradeWatchdog：失败自动回滚

Supervisor 后台线程 `UpgradeWatchdog`（`supervisor/upgrade.py`）通过 marker 文件实现“候选版本探活 + 失败回滚”：

- `/v1/admin/restart` 在自进化场景（带 approval_id）会写入 `data/upgrade_pending.json`
- watchdog 轮询探活：
  - kernel：心跳 worker_id 是否变化、进程是否存活
  - shell：pid 是否变化
- 超时失败：执行 `git reset --hard <rollback_head>` 并重启目标

限制：
- 如果 **Supervisor 本体** 在新版本启动阶段就直接崩溃（语法错误导致 main 无法起），watchdog 也无法运行，需人类手工回滚。

---

## 6. 推荐实践（给要长期跑的自进化 bot）

1) **启用 git**：让 checkpoint 与 rollback 真正可用。
2) **收紧 tools/**：默认建议对 `tools/**` 的写入保持 `approval_required`。
3) **把 secrets 留在 env**：不要让 AI 能 `read_file` 读到 `.env`。
4) **让最终输出走 Shell Responder**：Kernel 只做执行与草稿，减少“执行者”直接对用户输出导致的幻觉与噪音。

---

## 7. 相关文档

- 外部 Bot 接入：`docs/bot_integration.md`
