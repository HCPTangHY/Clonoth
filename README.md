# Clonoth

Clonoth 是一个模块化、可扩展的多平台 AI Agent 框架。

它面向长期运行的 Agent 服务场景，将任务调度、模型推理、平台接入、工具调用、技能注入和记忆管理拆分为相对独立的模块，便于扩展、维护和部署。

## 架构概览

Clonoth 采用 Supervisor + Engine + Bot Adapter 的多进程架构。

- **Supervisor** 负责进程管理、会话管理、任务路由、审批策略、事件记录、计划任务和管理接口。
- **Engine** 负责执行 AI 节点，包括上下文构建、模型调用、工具调用、Hook 执行、记忆处理、上下文压缩和节点切换。
- **Bot Adapter** 负责接入外部平台，例如 Discord 和 QQ，并将平台消息转换为 Clonoth 的会话与任务请求。
- **SDK** 为外部调用方提供客户端、回调、审批和事件路由等封装。

这种拆分方式使平台接入、运行时控制和模型执行可以独立演进，也便于在不同部署环境中按需组合。

## 核心特性

### Provider 插件化

`providers` 包提供统一的模型 Provider 接口。`ProviderRegistry` 负责发现和注册 Provider 实现，新增模型后端时通常只需要新增对应的 `providers/*.py` 文件，并按约定暴露实现类。

当前目录中包含 OpenAI、OpenAI Responses、Anthropic、Gemini 等 Provider 相关实现。

### Hook 系统

`engine/hooks` 提供 Hook 系统。Hook 处理器通过 `Handler`、`HookContext` 和 `HookResult` 与执行循环交互，可以在模型调用、工具调用或步骤执行前后插入检查、改写或拦截逻辑。

Hook 系统将运行时策略从主推理流程中拆出，便于测试和维护。

### 外部插件

`plugins` 目录支持外部 Hook 插件。插件文件通过暴露 `register(hook_registry)` 函数注册处理器，也可以提供可选的 `PLUGIN_META` 元数据。示例文件 `plugins/example_hook.py.disabled` 展示了基本协议，去掉 `.disabled` 后缀即可作为插件模板启用。

### 多平台接入

Clonoth 面向多平台 Bot 场景设计。当前项目包含 Discord 管理工具和 QQ/OneBot 相关数据与适配痕迹，平台接入层可以通过 Bot Adapter 将平台事件统一交给 Supervisor 和 Engine 处理。

### 工具系统

`tools` 和 `toolbox` 提供工具注册、运行和上下文支持。工具可以由 Engine 调用，用于文件操作、科学计算、图片处理、Discord 管理、网页读取、MCP 客户端接入等任务。

### 技能系统

`skills` 目录保存可注入的技能说明。技能可以按关键词或常驻策略注入上下文，用于给 Agent 增加特定领域能力、工具使用规范或平台交互规则。

### 记忆系统

Engine 包含记忆提取、记忆存储、上下文压缩和轮次摘要相关模块。记忆系统用于在长期会话中保存用户偏好、项目事实和可复用背景信息。

## 目录结构简表

| 路径 | 说明 |
| --- | --- |
| `engine/` | AI 节点执行核心，包含推理步骤、上下文、记忆、工具调用、Hook 和系统节点。 |
| `supervisor/` | 任务监督层，包含进程管理、任务路由、会话、审批、计划任务和管理 API。 |
| `providers/` | 模型 Provider 实现与 Provider 注册表。 |
| `clonoth_sdk/` | 外部客户端 SDK，封装请求、回调、审批和事件路由。 |
| `tools/` | 可由 Agent 调用的本地工具。 |
| `toolbox/` | 工具运行时、注册表、MCP 和技能运行支持。 |
| `skills/` | 技能定义，用于向模型注入领域能力和行为规则。 |
| `plugins/` | 外部 Hook 插件目录。 |
| `config/` | 运行时、节点、工作流和模型路由配置。 |
| `docs/` | 架构设计、集成说明和演进文档。 |
| `public/` | 管理界面和 Playground 静态资源。 |
| `tests/` | 自动化测试。 |

## 快速开始

### 1. 准备环境

需要 Python 3.11 或更高版本。如需 Web 前端，还需要 Node.js 18+ 和 npm。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 LLM Provider 的 API Key 和端点：

```bash
cp .env.example .env
```

最小可运行配置只需填写三个变量：

```env
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-your-key-here
OPENAI_MODEL=gpt-4o-mini
```

支持任意 OpenAI 兼容端点（本地 Ollama、代理服务等）。其他 Provider（Anthropic、DeepSeek、Gemini）的配置见 `.env.example` 中的注释。

### 3. 准备运行配置

```bash
mkdir -p data
cp config.example.yaml data/config.yaml
cp policy.example.yaml data/policy.yaml
```

三份配置文件的职责：

- **`.env`**：环境变量，存放 API Key 等敏感信息。不提交到仓库。
- **`data/config.yaml`**：Provider 选择、模型、base URL。支持 `${ENV_VAR}` 引用环境变量。不提交到仓库。
- **`data/policy.yaml`**：安全策略（文件读写权限、命令执行审批规则）。
- **`config/runtime.yaml`**：Engine 运行时参数（步数限制、上下文压缩阈值、worker 数量等）。可提交到仓库。

`data/config.yaml` 默认通过 `${OPENAI_BASE_URL}` 等引用 `.env` 中的变量，通常无需额外编辑。

### 4. 构建 Web 前端（可选）

```bash
cd platform/web/frontend
npm ci
npm run build
cd ../../..
```

Supervisor 启动后会自动挂载 `platform/web/frontend/dist/` 到 `/web/` 路径。如果不需要 Web 界面，可跳过此步。

### 5. 启动服务

```bash
python main.py
```

Supervisor 默认监听 `127.0.0.1:8765`。可通过环境变量修改：

```env
CLONOTH_HOST=0.0.0.0
CLONOTH_PORT=8765
```

启动后访问 `http://localhost:8765/web/` 进入 Web 界面。

生产环境建议使用 systemd、PM2 等进程管理器。如果使用 PM2，可参考根目录中的 `ecosystem.config.js`。

### 6. 一步部署（快捷命令）

以上步骤合并为一条命令：

```bash
python3.11 -m venv .venv && \
. .venv/bin/activate && \
python -m pip install -U pip && \
python -m pip install -r requirements.txt && \
cp .env.example .env && \
mkdir -p data && \
( test -f data/config.yaml || cp config.example.yaml data/config.yaml ) && \
( test -f data/policy.yaml || cp policy.example.yaml data/policy.yaml ) && \
( cd platform/web/frontend && npm ci && npm run build ) && \
echo "编辑 .env 填入 API Key，然后运行 python main.py"
```

### 7. 运行测试

```bash
pytest
```

### 环境变量参考

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | 全局 LLM API 端点 | `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | 全局 LLM API Key | 无（必填） |
| `OPENAI_MODEL` | 全局默认模型 | `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` | Anthropic Provider Key | fallback 到全局 |
| `DEEPSEEK_API_KEY` | DeepSeek Provider Key | fallback 到全局 |
| `GEMINI_API_KEY` | Gemini Provider Key | fallback 到全局 |
| `CLONOTH_HOST` | Supervisor 监听地址 | `127.0.0.1` |
| `CLONOTH_PORT` | Supervisor 监听端口 | `8765` |
| `CLONOTH_LOG_LEVEL` | 日志级别 | `info` |
| `CLONOTH_ADMIN_TOKEN` | Admin API 认证 token | 自动生成 |

## 详细文档

更多设计和集成说明见 `docs/` 目录：

- [Bot 集成说明](docs/bot_integration.md)
- [任务与会话架构 ADR](docs/ADR-task-session-architecture.md)
- [Dream 与 Compact 重构 RFC](docs/RFC-dream-and-compact-redesign.md)
- [工具系统与演进说明](docs/tools_and_evolution.md)
- [设计文档目录](docs/design/)

## 扩展入口

- 新增模型后端：在 `providers/` 中新增 Provider 实现，并接入 `ProviderRegistry` 约定。
- 新增 Hook：实现 `Handler`，通过 Hook 注册表注册到对应 Hook 点。
- 新增外部插件：在 `plugins/` 中创建插件文件，暴露 `register(hook_registry)` 函数。
- 新增工具：在 `tools/` 或 `toolbox/` 的约定位置实现工具，并完成注册。
- 新增技能：在 `skills/` 中添加技能说明文件，并配置触发策略。

## 许可

请以仓库中实际提供的许可文件为准。
