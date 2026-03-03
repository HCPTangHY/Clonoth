<div align="center">
  <img src="public/logo.jpg" alt="Clonoth Logo" width="200" />
  <h1>Clonoth</h1>
  <p>一个“自进化”的 Agent 基座</p>
</div>

## 0. 理念与架构

Clonoth 的目标是成为一个**自进化**的个人 Agent 基座：我们只完成最小且可靠的框架，其余能力（工具、工作流、适配器、甚至部分运行时逻辑）尽可能由 Agent 自己在运行中生成、热加载与迭代。

Clonoth 采用了多进程的架构：

- **Supervisor**：最小且稳定的核心。负责暴露 Gateway API、统一记录基于 JSONL 的事件日志（Event Sourcing）、执行 Policy 权限策略、管理审批流，以及对 Shell 和 Kernel 进行健康检查和重启。
- **Shell Worker**：面向用户的沟通界面（目前为 CLI）。负责维护对话上下文，判断是直接回复还是将复杂任务结构化下发给 Kernel，并向用户展示 Kernel 的进度。
- **Kernel Worker**：核心执行引擎。运行“思考 → 工具调用 → 观察”循环，动态加载 `tools/` 目录下的工具。当遇到敏感操作时，主动向 Supervisor 发起审批。
- **Tools**：Agent 自行生成和更新的 Python 模块（Tool v2）。采用声明式规范（AST 解析提取），无需重启进程即可热加载。

---

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

### 1.2 配置模型 (Provider)
Clonoth 统一抽象了 OpenAI、Gemini、Anthropic 等模型提供商。默认使用 **YAML** 在本地维护配置信息：`data/config.yaml`（已加入 `.gitignore`）。

建议：**把 base_url / api_key 放在环境变量中**，避免被 Agent 间接泄露。

创建或编辑 `data/config.yaml`：
```yaml
version: 1
provider: openai
openai:
  base_url: "${OPENAI_BASE_URL}" # 可为空，将 fallback 到默认地址
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"       # 可为空，默认 gpt-4o-mini
```

启动前设置环境变量（以 Windows PowerShell 为例）：
```powershell
$env:OPENAI_API_KEY = "YOUR_KEY"
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"  # 可选
$env:OPENAI_MODEL = "gpt-5.2"   # 可选
```
*(也可在运行后通过 API 动态修改，见下方 API 说明)*

### 1.3 启动方式

**方式一：单入口一键启动**
```bash
python main.py
```
> Windows 提示：Supervisor 会在当前终端运行 API 服务；Shell CLI 默认会在**新控制台窗口**中打开（可通过环境变量 `CLONOTH_SHELL_NEW_CONSOLE=0` 关闭）。

**方式二：多终端手动分离启动（推荐开发者使用）**
1. 启动控制面 (Supervisor)：
```bash
python -m supervisor.main --no-workers
```
2. 在其它终端分别启动 Worker 和 CLI：
```bash
# 启动 Kernel 执行引擎
python -m kernel.worker --supervisor http://127.0.0.1:8765

# 启动 Shell 路由与对话管理
python -m shell.worker  --supervisor http://127.0.0.1:8765

# 启动 本地交互式 CLI（仅作为终端界面接入 Gateway API）
python -m shell.cli     --supervisor http://127.0.0.1:8765
```

---

## 2. 核心机制

### 2.1 事件驱动与状态恢复 (Event Sourcing)
Clonoth 不依赖关系型数据库，所有的对话、任务、工具调用状态均基于 `data/events.jsonl` 以追加（Append-only）的方式持久化。
无论是 Kernel 崩溃、Shell 重启还是整个系统重启，Supervisor 都能从事件流中重建状态队列，实现无缝断点续传。

### 2.2 审批策略 (Policy & Approval)
为了防止 Agent 在自进化过程中“破坏世界”，Supervisor 在首次启动时会自动生成 `data/policy.yaml`。
它对内建操作（如 `read_file`, `write_file`, `execute_command`, `restart`）进行了严格控制，划分为：
- **L1 自动执行**：只读操作或低风险写入。
- **L2 需审批**：执行系统命令、修改核心代码、重启系统。请求会推送到终端，需人类确认（可手动干预修改为自动放行）。
- **L3 硬拒绝**：高危指令阻断。

### 2.3 工具系统 (Tools Hot-reload)
`tools/` 目录下的工具为“声明式命令工具”。Kernel 进程**不会去 import 或执行**这些未知的 Python 代码，而是通过 AST（抽象语法树）解析出工具的 `SPEC`（输入规范）和 `COMMANDS`。
这既能实现工具编写后的**即刻热加载**，又能避免恶意代码绕过 Supervisor 的 Policy 拦截。

### 2.4 Runtime 动态调参
`config/runtime.yaml` 提供运行时的非敏感参数调整（如 Kernel 轮询间隔、最大执行步数、工具历史截断长度等）。这些参数可由人类修改，也可授权给 Agent 自身进行优化。

---

## 3. 对外集成与 API

Clonoth 将复杂性封装在后端，对外提供了一套稳定的 Gateway API，非常适合接入 Telegram、Discord 或企业微信等外部平台。

### 收发消息
```http
POST /v1/inbound
```
请求体示例：
```json
{
  "channel": "telegram",
  "conversation_key": "chat_12345",
  "text": "帮我写一个 python 爬虫脚本",
  "use_context": true
}
```
*(提示：若 `use_context` 设为 `false`，则由外部 Bot 平台完全自行管理历史记忆。)*

### 动态修改配置
通过 API 直接更新对应 provider 的配置（以 OpenAI 为例）：
```bash
curl -X POST http://127.0.0.1:8765/v1/config/openai \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_KEY","model":"gpt-4o-mini"}'
```

---

## 4. 目录结构
```text
Clonoth/
├── supervisor/     # 控制面：Gateway API、事件日志(Eventlog)、权限(Policy)、进程管理
├── shell/          # 路由层：对话维护、意图识别、任务下发（含本地 CLI）
├── kernel/         # 执行层：Task 执行循环、请求审批、调用工具
├── providers/      # 大模型适配层 (OpenAI / Gemini / Anthropic)
├── tools/          # 自进化区：Agent 编写的工具（热加载）
├── config/         # 运行时配置、Prompts
├── data/           # events.jsonl, config.yaml, policy.yaml等动态数据
└── public/         # 静态资源（如 logo）
```