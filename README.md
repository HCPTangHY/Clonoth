<div align="center">
  <img src="public/logo.jpg" alt="Clonoth Logo" width="200" />
  <h1>Clonoth</h1>
  <p>一个自进化的 Agent 基座</p>
</div>

## 0. 架构

Clonoth 采用节点图架构，由以下部分组成：

- **Supervisor**：控制面。负责 HTTP API、事件日志、审批流程、安全策略、进程管理。
- **Engine**：执行引擎。加载节点图（workflow），按连接关系调度 AI 节点和工具节点，支持子链调用（handoff）。
- **Toolbox**：工具层。包含 14 个内置工具和 `tools/` 下的脚本工具。
- **Shell CLI**：终端适配层。负责用户交互、流式输出、审批确认。

节点图中的所有角色——入口节点、执行节点、审核节点、规划节点——都是 AI 节点的不同配置。它们的区别在于提示词、模型路由和工具权限，而不是代码层级。

## 1. 快速开始

### 1.1 安装依赖

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate
# Linux/Mac
# source .venv/bin/activate

pip install -r requirements.txt
```

### 1.2 配置模型

编辑 `data/config.yaml`（可参考 `config.example.yaml`）：

```yaml
version: 1
provider: openai
openai:
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"
```

### 1.3 启动

**单入口启动（推荐）**

```bash
python main.py
```

Supervisor 会自动拉起 Engine 和 Shell CLI。

**分开启动**

```bash
# 启动控制面
python -m supervisor.main --no-workers

# 启动执行引擎
python -m engine --supervisor http://127.0.0.1:8765

# 启动终端 CLI
python -m shell.cli --supervisor http://127.0.0.1:8765
```

## 2. 节点系统

### 2.1 节点定义

`config/nodes/*.yaml` 定义节点。每个节点声明自己的类型、提示词、模型路由和工具权限。

当前默认节点：

| 节点 | 用途 |
|---|---|
| `bootstrap.shell_orchestrator` | 入口节点。判断直接回复还是移交下游 |
| `bootstrap.executor` | 执行节点。多步推理、调用工具 |
| `bootstrap.cmd_reviewer` | 命令审核节点。对 shell 命令做语义审核 |
| `bootstrap.planner` | 规划节点 |
| `bootstrap.reviewer` | 复核节点 |

### 2.2 工作流

`config/workflows/*.yaml` 定义节点之间的连接关系。

当前默认工作流：

- `bootstrap.default_chat`：入口 → 执行 → 命令审核（handoff）→ 回复
- `bootstrap.plan_execute_review`：规划 → 执行 → 复核

### 2.3 提示词

`config/prompt_packs/` 存放提示词片段和组装规则。按 `manifest.yaml` 中的 assembly 定义拼装最终 system prompt。

### 2.4 模型路由

`config/model_routing.yaml` 为不同节点指定模型。支持按节点、按角色分配不同的 provider 和 model。

## 3. 工具系统

### 3.1 内置工具

14 个内置工具：`list_dir`、`read_file`、`write_file`、`execute_command`、`search_in_files`、`create_or_update_skill`、`list_skills`、`delete_skill`、`create_or_update_mcp_client`、`list_mcp_clients`、`delete_mcp_client`、`create_or_update_tool`、`reload_tools`、`request_restart`。

### 3.2 脚本工具

`tools/*.py` 下的脚本工具以独立子进程运行。通过 stdin/stdout 的 JSON 协议通信。AI 可以通过 `create_or_update_tool` 创建新工具。

### 3.3 MCP 工具

MCP 客户端定义在 `data/mcp_clients.yaml`。Engine 启动时自动加载。

## 4. 安全策略

安全策略定义在 `data/policy.yaml`（参考 `policy.example.yaml`）。

### 4.1 双层审核

命令执行采用双层机制：

1. `cmd_reviewer` 节点做 AI 语义审核
2. Supervisor Policy 做硬规则和人类审批

AI 审核不能替代人类审批。

### 4.2 文件保护

| 路径 | 策略 |
|---|---|
| `engine/**`、`supervisor/**`、`toolbox/**`、`providers/**`、`shell/**` | 需要审批 |
| `config/nodes/**`、`config/workflows/**`、`config/prompt_packs/**` | 需要审批 |
| `tools/**` | 需要审批 |
| `clonoth_runtime.py`、`main.py` | 需要审批 |
| `data/policy.yaml` | 硬拒绝 |
| `data/events.jsonl` | 硬拒绝 |
| `.env` | 硬拒绝 |
| `config/runtime.yaml` | 自动允许 |

### 4.3 命令硬拒绝

以下命令无论如何不会通过：`rm -rf /`、`rm -rf ~`、`rm -rf *`、`format`、`mkfs`、`fdisk`、`dd if=/dev/zero`、`shutdown`、`reboot`。

## 5. 自进化

AI 可以在人类审批下修改自身的节点定义、工作流、提示词、模型路由和工具。修改后通过 `request_restart` 重启 Engine 使改动生效。

唯一不可修改的是安全策略（`data/policy.yaml`）和事件日志（`data/events.jsonl`）。

## 6. 目录结构

```text
Clonoth/
├── supervisor/     # 控制面
├── engine/         # 执行引擎
├── shell/          # CLI 适配层
├── toolbox/        # 工具层（内置工具 + 脚本工具运行器 + MCP）
├── providers/      # 模型适配层
├── config/         # nodes / workflows / prompt_packs / model_routing / runtime
├── tools/          # 脚本工具
├── skills/         # Skill 文件
├── data/           # 事件日志、配置、运行时数据
└── docs/           # 文档
```

## 7. 流式输出

`config/runtime.yaml` 中 `engine.streaming: true` 开启后，入口节点的 LLM 响应会逐块推送到 CLI。CLI 同时显示思维链（灰色）和文本内容（逐字输出）。
