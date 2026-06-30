# 配置说明

本文说明 Clonoth 当前仓库中的主要配置文件：`config/runtime.yaml`、`config/model_routing.yaml`、`config/nodes/*.yaml`，以及常用环境变量。

## runtime.yaml 结构

`config/runtime.yaml` 是运行时调优配置，可以提交到仓库。敏感信息应放在环境变量或 `data/config.yaml` 中，不应写入此文件。

当前结构如下：

```yaml
version: 1
engine: {}
providers: {}
meta: {}
tools: {}
skills: {}
memory: {}
shell: {}
supervisor: {}
```

### engine

`engine` 控制引擎执行行为。

常用字段：

- `max_steps`：单个 AI 节点最多执行步数。
- `streaming`：是否启用流式输出。
- `history_limit`：加载历史消息数量上限。
- `poll_interval_sec`：轮询间隔。
- `max_workers`：并发 task 处理数。
- `model`：全局默认模型。为空时使用 provider 或配置中的默认值。
- `tool_mode`：全局默认工具模式，支持 `native`、`fake-native`、`json`。节点可以覆盖。

子结构：

- `compact`：上下文压缩配置，例如 `threshold_tokens` 和 `keep_recent`。
- `child_session`：子会话隔离配置，例如 `ttl_hours`、`max_per_parent`、`main_session_enabled`。
- `retry`：LLM 调用失败后的重试配置，例如最大重试次数、初始延迟、最大延迟和退避倍数。
- `http`：引擎 HTTP 客户端超时。
- `supervisor`：引擎访问 Supervisor 的健康检查和等待轮询配置。
- `tool_trace`：工具调用日志和进度参数截断长度。
- `signals`：进程内信号总线配置。

### providers

`providers` 保存 provider 相关运行时参数。目前示例中有：

```yaml
providers:
  openai:
    timeout_sec: 600.0
```

provider 实现由 `providers/` 包自动发现。新增 provider 文件时，类需要继承 `BaseProvider` 并声明 `provider_name`。

### meta

`meta` 控制内置元工具的一些限制：

- `execute_command.default_timeout_sec`：命令默认超时。
- `execute_command.max_output_chars`：命令输出最大内联字符数。
- `git.diff_max_chars`：Git diff 最大字符数。
- `search.max_file_size_bytes`：搜索时单文件大小上限。
- `search.max_matches`：搜索最大匹配数量。

### tools

`tools` 保存工具运行参数。当前配置包含：

```yaml
tools:
  command:
    default_timeout_sec: 60.0
```

脚本式自定义工具还会读取 `tools.script.default_timeout_sec`。如果未配置，代码默认使用 60 秒。

### skills

`skills.max_budget_chars` 控制 Skill 正文最大注入字符数。`0` 表示不限制。

### memory

`memory.max_budget_chars` 控制记忆最大注入字符数。`0` 表示不限制。

`memory.auto_extract` 控制会话结束后的自动记忆提取：

- `enabled`：是否启用。
- `node_id`：记忆提取节点。
- `min_messages`：会话消息数少于该值时跳过。
- `min_increment`：自上次提取后新增消息数达到该值才再次触发。

`memory.dream` 控制定期记忆整理：

- `enabled`：是否启用。
- `cron`：UTC cron 表达式。
- `node_id`：记忆整理节点。
- `min_sessions`：至少有多少不同 session 活动后触发。
- `conversation_key`：dream 使用的会话标识。

### shell

`shell` 是历史命名空间。内置 CLI/TUI shell 已移除，目前仅保留：

- `entry_node_id`：默认入口节点。

### supervisor

`supervisor.process_manager` 控制 Supervisor 拉起和停止子进程：

- `stop_wait_timeout_sec`：停止等待时间。
- `engine_workers`：引擎 worker 数量。

## model_routing.yaml

`config/model_routing.yaml` 定义模型路由表。当前文件结构如下：

```yaml
version: 1

defaults:
  provider: openai
  fallback_model: gpt-4o-mini

routes:
  route_name:
    description: 路由说明
    candidates:
      - provider: openai
        model_runtime_key: engine.model
        fallback_to_provider_config_model: true
        fallback_model: gpt-4o-mini
```

字段含义：

- `defaults.provider`：默认 provider。
- `defaults.fallback_model`：最终兜底模型。
- `routes.<name>.description`：路由用途说明。
- `candidates`：候选模型列表，按顺序尝试或选择。
- `provider`：候选 provider 名称。
- `model_runtime_key`：从 `runtime.yaml` 中读取模型名的路径。
- `fallback_to_provider_config_model`：是否回退到 provider 配置中的模型。
- `fallback_model`：该候选自己的兜底模型。

需要注意：按当前代码搜索结果，Python 代码中没有直接引用 `model_routing.yaml`。当前实际模型解析主要由节点配置、`data/config.yaml` 和 `engine.model` 参与。也就是说，该文件目前更像预留或设计中的声明式路由表；如果要让它生效，需要在模型解析流程中接入。

## 节点配置

节点配置位于：

```text
config/nodes/*.yaml
```

系统内建节点优先从 `engine/system_nodes/` 读取，找不到时再读取 `config/nodes/`。

一个典型节点如下：

```yaml
id: bot_adapter
type: ai
name: MyMainNode
description: 通用节点
tool_access:
  mode: all
skills:
  mode: all
delegate_targets:
  - other_node
prompt: |
  这里是 system prompt。
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 节点 ID。通常与文件名一致。 |
| `type` | 节点类型，支持 `ai` 或 `tool`。 |
| `name` | 显示名称。 |
| `description` | 节点说明。 |
| `prompt` | system prompt。支持字符串或 block 列表。 |
| `model` | 节点专用模型。为空时使用全局默认。支持 `${VAR}` 和 `$ENV{VAR}`。 |
| `api_key` | 节点专用 API key。支持环境变量引用。 |
| `base_url` | 节点专用 base URL。支持环境变量引用。 |
| `provider` | provider registry key，例如 `openai`、`openai-responses`、`anthropic`、`gemini`。 |
| `provider_options` | 透传给 provider 的参数。 |
| `tool_mode` | 覆盖全局工具模式。支持 `native`、`fake-native`、`json`。 |
| `output_mode` | 输出模式，支持 `tool_only` 或 `hybrid`。 |
| `delegate_targets` | 允许委派的目标节点列表。 |

### 工具访问控制

```yaml
tool_access:
  mode: all
  allow: []
  deny: []
```

支持模式：

- `none`：不允许工具。
- `all`：允许所有工具，可配合 `deny` 排除部分工具。
- `allowlist`：只允许 `allow` 列表中的工具。

### Skill 访问控制

```yaml
skills:
  mode: all
  allow: []
```

支持模式：

- `all`：允许所有启用的 Skill。
- `allowlist`：只允许 `allow` 列表中的 Skill。
- `none`：不注入 Skill。

### 记忆访问控制

```yaml
memories:
  mode: all
  allow: []
```

支持模式：

- `all`
- `allowlist`
- `none`

## 环境变量说明

Supervisor 启动时会调用 `load_dotenv()`，因此可以通过 `.env` 提供环境变量。但 `.env` 不应提交到仓库。

配置值支持两种环境变量引用写法：

```yaml
api_key: "${OPENAI_API_KEY}"
base_url: "$ENV{OPENAI_BASE_URL}"
```

常用环境变量如下。

### LLM 和 provider

- `OPENAI_API_KEY`：OpenAI 或兼容接口 API key。
- `OPENAI_BASE_URL`：OpenAI 兼容接口 base URL。
- `OPENAI_MODEL`：默认模型名，可在 `data/config.yaml` 中引用。
- `<PROVIDER>_API_KEY`：非 OpenAI provider 的 API key，例如 `ANTHROPIC_API_KEY`、`GEMINI_API_KEY`。
- `<PROVIDER>_BASE_URL`：非 OpenAI provider 的 base URL，例如 `ANTHROPIC_BASE_URL`、`GEMINI_BASE_URL`。

### Supervisor 和 Shell

- `CLONOTH_HOST`：Supervisor 监听地址，默认 `127.0.0.1`。
- `CLONOTH_PORT`：Supervisor 监听端口，默认 `8765`。
- `CLONOTH_LOG_LEVEL`：日志级别，默认 `info`。
- `CLONOTH_ACCESS_LOG`：是否启用 uvicorn access log。
- `CLONOTH_SUPERVISOR_URL`：Engine、CLI、TUI 访问 Supervisor 的地址。
- `CLONOTH_WORKER_ID`：Engine worker ID。未设置时自动生成。
- `CLONOTH_CONVERSATION_KEY`：Shell 默认会话键。
- `CLONOTH_ADMIN_TOKEN`：Admin API 访问令牌。
- `CLONOTH_SHELL_NEW_CONSOLE`：是否为 shell 使用新控制台。

### 自定义工具和外部服务

- `GEMINI_API_KEY`：`read_image.py` 可使用。
- `GITHUB_TOKEN`：`scan_github_keys.py` 可使用。
- `DISCORD_BRIDGE_HOST`：`discord_manage.py` 连接 Discord Bridge 时使用，默认 `127.0.0.1`。

### MCP

MCP 客户端配置中的 `env` 和 `headers` 可以引用环境变量。例如：

```yaml
clients:
  github:
    transport: stdio
    enabled: true
    command: npx
    args:
      - -y
      - "@modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}"
```

不要把真实 API key、GitHub token 或其他访问凭据明文写入 `data/mcp_clients.yaml`、`config/*.yaml` 或文档。