# 配置说明

本文说明 Clonoth 当前仓库中的主要配置文件：`config/runtime.yaml`、`config/nodes/*.yaml`、`data/policy.yaml`，以及常用环境变量。

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

- `compact`：`threshold_tokens` 为自动压缩阈值，`keep_recent` 为优先保留的最近任务段数。压缩时保留完整工具调用配对，并根据剩余空间调整保留量；不再每轮自动删减历史前部。
  **历史保留提示**：当前等待压缩结果的恢复路径仍包含应急裁剪策略。摘要未通过校验时，可能用失败提示替代旧消息以恢复运行；这不等同于保留全部原文，不能将上下文压缩当作历史备份。
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
- `min_increment`：未提取的入口任务数达到该值时，立即安排后台提取。

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

## 模型解析顺序

模型和 provider 的解析层级从高到低：

1. 会话级 provider override（`data/sessions.json` 中每个 session 的 `provider_override` 字段，通过 Web 前端或 API 设置）
2. 节点配置中的 `model` / `provider` / `api_key` / `base_url` 字段
3. `data/config.yaml` 中的全局 provider 配置
4. 环境变量（`OPENAI_API_KEY`、`OPENAI_BASE_URL` 等）

早期版本中存在的 `config/model_routing.yaml` 已移除，当前不再使用。


## 节点配置

节点配置位于：

```text
config/nodes/*.yaml
```

系统内建节点由所属内置插件通过 PLUGIN_META.nodes 声明（声明文件在 `engine/builtin/nodes/`）；`engine/system_nodes/` 与 `config/nodes/` 为文件来源兜底，优先级低于插件声明。

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
| `provider` | provider registry key，例如 `openai`、`openai-responses`、`anthropic`、`gemini-aistudio`、`gemini-vertex`。明确填写未注册名称会报错。 |
| `provider_options` | 透传给 provider 的参数。 |
| `tool_mode` | 覆盖全局工具模式。支持 `native`、`fake-native`、`json`。 |
| `output_mode` | 输出模式，支持 `tool_only` 或 `hybrid`。 |
| `delegate_targets` | 允许委派的目标节点列表。 |

### OpenAI 与 DeepSeek 参数

渠道配置中的 `options` 与节点的 `provider_options` 合并后传入 Provider。OpenAI Chat Completions 支持：

- `reasoning_effort`：传入接口的思考强度。
- `max_tokens`、`max_completion_tokens`：正整数输出限制；无效值不发送。
- `reasoning_field`：需要回传的原生思考字段名称，默认为空。为空时仍可展示响应中的 `reasoning_content`、`reasoning` 或 `thinking`，但请求保持标准 OpenAI 格式。

例如，兼容端点要求回传 `reasoning_content` 时，在节点中配置：

```yaml
provider: openai
provider_options:
  reasoning_field: reasoning_content
  reasoning_effort: high
  max_completion_tokens: 32768
```

回传使用 Provider 保存的原始字段，不使用公共展示文本。文本工具历史模式不回传该字段。

DeepSeek 复用 OpenAI 的流式、非流式和错误处理。Provider 类的缺省地址为 `https://api.deepseek.com`，缺省模型保留 `deepseek-v4-pro`；引擎或节点明确指定的模型、地址仍优先。其 `reasoning_field` 默认为 `reasoning_content`，`reasoning_effort` 默认为 `high`。设置 `reasoning_effort: none` 关闭思考和回传；旧构造参数 `thinking=False` 仍可用。DeepSeek 只发送 `max_tokens`，未配置时可从 `max_completion_tokens` 映射。

### Gemini 渠道类型

Gemini 只提供两个可选渠道类型：

- `gemini-aistudio`：AI Studio；未指定地址时使用 `https://generativelanguage.googleapis.com`。
- `gemini-vertex`：通过 Gemini 协议兼容端点访问 Vertex 后端，必须指定 `base_url`。当前沿用 `/v1beta/models/{model}:generateContent` 路径，由网关负责上游路由和鉴权，不是直接访问 Vertex 项目/区域路径的 OAuth 客户端。

`providers/gemini.py` 只是共同实现，不注册为渠道。旧的 `provider: gemini` 不再支持，需要手动修改全局、节点、会话覆盖和备用渠道中的相关配置；系统不会猜测后端或将其自动映射到其他类型。

环境变量分别使用 `GEMINI_AISTUDIO_API_KEY` / `GEMINI_AISTUDIO_BASE_URL` 和 `GEMINI_VERTEX_API_KEY` / `GEMINI_VERTEX_BASE_URL`，不读取旧的 `GEMINI_*` 变量。也可以在 `data/config.yaml` 的对应渠道配置中明确填写地址、模型和密钥引用。

新消息按明确的渠道名称保存和读取原始响应元数据。旧 `metadata.gemini` 和另一种 Gemini 渠道的原始签名不会被复用；普通正文和工具记录仍按现有转换规则处理。这里仅区分渠道类型，不代表已经实现同类型下的账号或部署级签名隔离。

两个渠道采用生产新增的请求参数处理：

- `max_tokens` 或 `max_output_tokens` 映射为 `maxOutputTokens`，缺省为 `65535`；前者优先。
- `temperature`、`top_p`、`top_k` 分别映射为对应的生成参数。
- `reasoning_effort` 转为大写的 `thinkingLevel`，`none` 表示不发送该级别参数，并不保证模型停止内部思考。
- `thought_summary` 缺省为 `true`，写入 `includeThoughts: true`。显式关闭时不请求摘要。它是请求选项，与公共响应中已取消的同名字段无关。
- 保留生产配置优先级：上述思考选项覆盖低层 `thinking_config` 的对应项，`thinkingBudget` 不再发送。仅设置 `thinking_config.includeThoughts: false` 不会覆盖缺省摘要开关；应同时设置 `thought_summary: false`。
- 未指定 `safety_settings` 时使用生产默认类别和阈值；AI Studio 与 Vertex 的类别列表不同。可以用显式列表替换默认设置，具体支持范围由目标接口决定。

Vertex 兼容网关请求保留末尾空白 `system` 消息；目标接口明确拒绝这一角色时，在本次请求副本中移除并重试一次。签名错误同样保留一次兼容重试，但不会修改保存的原始 parts，或把一个 part 的签名转给另一个 part。

流式思考提取到 `reasoning`，普通回复提取到 `text`；原始 parts 及流式边界另行保存。完整非流式响应不合并；带签名的 part 不与其他 part 合并。usage 包含思考与工具提示词计数；生产中重复提示词计数的经验修正仅用于 Vertex 的 `gemini-4-argon` 模型名称，其余情况按返回计数处理。

工具 Schema 的可空类型和多类型列表会转换为对应类型结构，不再只取第一种类型。原生 Schema 无法完整表示的位置元组约束，转换时保留全部元素类型并记录警告，不声称保留了完整的位置约束。

### Anthropic 与 Responses 参数

Anthropic 支持 `max_tokens`，缺省为 `64000`，也可通过 `max_completion_tokens` 指定。`reasoning_effort` 映射到 `output_config.effort`；`none` 关闭思考。默认采用 adaptive thinking 并请求摘要，`thought_summary: false` 将摘要显示设为 `omitted`。显式 `thinking.type: enabled` 仍保留其 `budget_tokens`，adaptive 模式不发送预算。

OpenAI Responses 将 `max_tokens` 或 `max_completion_tokens` 映射为 `max_output_tokens`。`reasoning_effort` 映射为 `reasoning.effort`；`thought_summary` 支持布尔值和 `auto`、`concise`、`detailed`，缺省不主动请求摘要。仍支持直接填写嵌套 `reasoning`；显式的高层选项覆盖对应子项。`effort: none` 时不发送摘要选项。

两者的思考正文和摘要均通过统一 `reasoning` 展示。原始签名块或 `raw_output` 独立保存。Anthropic 签名错误、Responses 加密校验错误各保留一次请求副本重试；已发出可见流式内容后的 Responses 失败不会自动重新开始，以免重复展示。

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

### 控制类伪工具开关

`tool_access` 支持操作级规则，例如 `allow: ["manage_memory:list"]` 只允许读取，`deny: ["manage_memory:!list"]` 禁止读取以外的操作。也可以写成 `allow: [{manage_memory: [list]}]`。规则同时影响模型可见的操作枚举和实际执行检查。同一工具的多条 `allow` 规则取并集，`deny` 仍优先；`allow: ["manage_memory:!delete"]` 表示允许删除以外的操作。外部工具的动作须与其枚举值完全一致。

新统一的 `manage_memory`、`manage_skill`、`manage_mcp_client` 未指定动作时只执行 `list`。既有 `manage_tool`、`manage_schedule` 保留原有参数推断方式，权限检查按它们实际会执行的动作进行。

控制类伪工具（`finish`、`ask`、`intermediate_reply`、`compact_context`、`manage_workers`、`switch_node`）由 `control_tools` 字段单独控制：

```yaml
control_tools: all            # 默认，全部注入（旧行为）
control_tools: none           # 全部不注入
control_tools: [finish, intermediate_reply]  # 只注入列出的
```

注意：`finish` 仅在 `output_mode: tool_only` 下注入；`switch_node` 对系统任务不注入。声明一个只有命令执行能力的最小节点：

```yaml
tool_access:
  mode: allowlist
  allow: [execute_command]
skills: {mode: none}
memories: {mode: none}
control_tools: [finish, intermediate_reply]
```

`control_tools: none` 搭配 `output_mode: tool_only` 时节点无法正常 finish，需谨慎。

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

- `all`：按常驻及关键词规则注入。
- `allowlist`：只允许指定记忆书；空列表不允许任何记忆书。
- `constant`：只注入常驻记忆。
- `none`：不注入记忆。

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
- `<PROVIDER>_API_KEY`：非 OpenAI provider 的 API key，例如 `ANTHROPIC_API_KEY`、`GEMINI_AISTUDIO_API_KEY`、`GEMINI_VERTEX_API_KEY`。
- `<PROVIDER>_BASE_URL`：非 OpenAI provider 的 base URL，例如 `ANTHROPIC_BASE_URL`、`GEMINI_AISTUDIO_BASE_URL`、`GEMINI_VERTEX_BASE_URL`。

### Supervisor

- `CLONOTH_HOST`：Supervisor 监听地址，默认 `127.0.0.1`。
- `CLONOTH_PORT`：Supervisor 监听端口，默认 `8765`。
- `CLONOTH_LOG_LEVEL`：日志级别，默认 `info`。
- `CLONOTH_ACCESS_LOG`：是否启用 uvicorn access log。
- `CLONOTH_SUPERVISOR_URL`：Engine 访问 Supervisor 的地址。
- `CLONOTH_WORKER_ID`：Engine worker ID。未设置时自动生成。
- `CLONOTH_ADMIN_TOKEN`：Admin API 访问令牌。

### 自定义工具和外部服务

- `GEMINI_API_KEY`：`read_image.py` 可使用。
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

## 适配器展示和权限

- `response_info` 保存实际响应的 Provider、模型、用量和回复说明。它是独立的展示信息，不拼入回复正文，不进入下一次模型请求。备用模型成功接替时，Web 和 Discord 可据此显示说明；没有说明的旧消息仍可正常读取。
- Web 的默认审批模式仍为 `smart`。主动切换为 `yolo` 后，客户端会自动同意需要审批的操作，包括工作区外的操作。此模式不覆盖服务端的拒绝规则；不需要这一行为时，请使用 `smart` 或手动模式。
- 插件面板的静态资源使用短期、HttpOnly、SameSite=Strict Cookie。申请和续期需要已有的 Bearer 身份；Cookie 只授权对应插件的已登记静态路由，不授权普通 API。退出登录会撤销面板会话。插件 API 仍沿用原有认证，不应把静态面板 Cookie 当作通用访问令牌。
- 子会话完成后会从侧栏隐藏，但隐藏不等于删除历史。可从子任务记录查看尚未过期的会话；后台按原有清理期限回收临时会话。
- Discord 的身份组白名单和上下文记录方式见 [Discord 适配器说明](../adapters/discord/README.md)。仓库不包含生产实例的 Bot Token、用户或频道 ID，也不包含实例专用启动入口。
