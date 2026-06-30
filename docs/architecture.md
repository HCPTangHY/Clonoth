# Clonoth 架构说明

本文说明 Clonoth 当前的核心架构。系统按职责分为三层：Supervisor、Engine 和 Bot Adapter。三层之间以 HTTP API 和事件流连接，避免把平台接入、任务调度和大模型推理混在同一进程中。

## 1. 三层架构

### Supervisor：任务调度与事件流

Supervisor 是系统的控制面。它接收外部输入，维护 session、任务队列和事件日志，并把任务分配给 Engine 执行。

主要职责包括：

- 接收 `/v1/inbound` 输入，并根据 `channel` 和 `conversation_key` 找到或创建 session。
- 把输入转为待处理工作项，再创建具体 task。
- 维护 task 状态，包括 pending、running、completed、failed、cancelled 等。
- 在 task 创建、启动、完成、取消和恢复等阶段写入事件。
- 通过 `/v1/events` 和 session 级事件接口向 SDK 或适配器暴露事件流。
- 在 task 完成后根据 `result.action` 进行统一路由，例如 finish、dispatch、switch、preempt、compact 等后续动作。

`supervisor/task_router.py` 中的 `TaskRouterMixin` 是完成态任务的统一分发入口。它会先处理批量任务、上下文压缩任务和轮摘要任务，再根据 task result 中的 action 字段进入对应子路由。

### Engine：LLM 推理与工具执行

Engine 是执行面。它从 Supervisor 领取任务，加载节点配置，构造运行上下文，调用模型 provider，并执行模型产生的工具调用。

主要职责包括：

- 通过 Supervisor 的任务接口领取 node task 或 tool task。
- 根据 node yaml 加载节点定义，包括模型、提示词、工具权限、技能权限、记忆权限和输出模式。
- 创建 provider，并调用统一的推理循环 `run_ai_node`。
- 在推理循环中构造消息历史，处理上下文快照、附件、记忆、技能和 hook。
- 处理 LLM 返回的文本、伪工具调用和真实工具调用。
- 执行工具后把结果写回对话历史，并继续下一步推理，直到任务完成或被中断。
- 向 Supervisor 回报 task complete，并写入 `node_started`、`node_completed`、`stream_delta`、`handoff_progress` 等事件。

`engine/inference/ai_step.py` 中的 `run_ai_node` 是 AI 节点的主循环入口。它接收 `RunContext`、provider、工具注册表、节点定义、用户指令、历史消息、恢复数据和附件等参数，返回 `TaskAction`，由 Engine 再提交给 Supervisor。

### Bot Adapter：平台桥接

Bot Adapter 是平台接入层。它不直接执行模型推理，也不直接管理任务队列，而是把 Discord、命令行或其他平台的输入转成 Clonoth 的 inbound 请求，并把事件流转回平台消息。

SDK 中的 `EventRouter` 是 Adapter 的通用协议层。它轮询 Supervisor 事件流，维护触发消息和主任务状态，并通过 `AdapterCallbacks` 通知具体平台适配器执行发送消息、编辑进度、展示审批界面、刷新 typing 状态等操作。

SDK 保留平台无关逻辑，例如协议标记清理、审批去重和事件分发。平台相关逻辑，例如 Discord 的 `[SPLIT]`、`[REACT]`、消息编辑节流和动画展示，由具体 Adapter 自行处理。

## 2. 进程模型

Clonoth 采用多进程模型。Supervisor、Engine 和 Bot Adapter 可以分别作为独立进程运行。

典型通信方式如下：

1. Bot Adapter 收到平台消息后，请求 Supervisor 的 `/v1/inbound`。
2. Supervisor 记录 inbound event，并把输入放入待处理队列。
3. Engine 通过 `/v1/inbound/next` 和 `/v1/tasks/next` 领取工作。
4. Engine 执行节点或工具任务后，通过 `/v1/tasks/{task_id}/complete` 回报结果。
5. Bot Adapter 或 SDK 通过 `/v1/events` 按 `after_seq` 轮询新事件，并把输出呈现到平台。

这种设计让调度状态集中在 Supervisor，推理和工具执行集中在 Engine，平台协议集中在 Adapter。任一层重启时，可以依靠事件日志、任务状态和会话信息恢复运行。

## 3. 事件流模型

事件流路径是：

```text
EventStore → Events API → EventRouter → AdapterCallbacks → 平台操作
```

Supervisor 的 `EventLog` 是 append-only JSONL 日志。每个事件包含 `seq`、`event_id`、`ts`、`run_id`、`session_id`、`component`、`type` 和 `payload`。`seq` 单调递增，调用方可以用 `after_seq` 做增量轮询。

Supervisor 暴露两类事件读取方式：

- session 级事件接口：按指定 `session_id` 返回事件。
- 全局 `/v1/events`：按 `after_seq` 返回所有 session 的事件，并支持通过 `types` 参数过滤事件类型。

SDK 的 `EventRouter` 读取这些事件后，会按事件类型分发处理。例如：

- `inbound_message`：记录用户输入触发源。
- `task_created`：把 task id 回填到触发状态。
- `node_started` / `node_completed`：更新节点进度。
- `stream_delta`：把流式文本交给 Adapter 展示。
- `outbound_message` / `intermediate_reply`：产生平台可见文本。
- `approval_requested`：触发审批展示。
- `task_completed` / `task_cancelled`：结束或清理任务状态。

## 4. 会话管理

系统用 `session` 承载一段连续对话状态。外部输入进入 `/v1/inbound` 时，Supervisor 调用 `get_or_create_session(channel, conversation_key)` 查找或创建 session。

`conversation_key` 是平台侧的会话映射键。它用于把同一频道、同一线程、同一用户私聊或其他平台会话稳定映射到同一个 Clonoth session。Supervisor 内部保存 `conversation_map`，把 `conversation_key` 映射到 `session_id`。

session 还关联以下信息：

- inbound 消息游标，用于防止重复处理。
- task 的 `session_generation`，用于上下文清理和取消后的代际隔离。
- 对话历史和上下文引用，用于 Engine 恢复历史消息。
- 子任务和子 session 信息，用于 dispatch、compact、summary 等派生任务。

## 5. 任务生命周期

一次常规用户请求的生命周期如下：

```text
inbound → task_created → node_started → llm_call → tool_call → finish
```

更完整的过程如下：

1. Adapter 收到平台输入，提交到 `/v1/inbound`。
2. Supervisor 写入 `inbound_message` 事件，并记录 inbound seq。
3. Engine 领取 inbound work item，Supervisor 创建 node task，并写入 `task_created`。
4. Engine 领取 task，加载 node yaml，构造 `RunContext`，写入 `node_started`。
5. Engine 调用 `run_ai_node`，进入 LLM 推理循环。
6. LLM 返回文本、伪工具调用或真实工具调用。流式文本会形成 `stream_delta` 事件。
7. 如果模型请求工具，Engine 执行工具，并通过进度事件写入工具调用状态。
8. 工具结果回填到消息历史，推理循环继续。
9. 当模型调用 `finish`，或节点输出被转换为完成动作时，Engine 得到 `TaskAction`。
10. Engine 写入 `node_completed`，并通过 task complete API 把结果交回 Supervisor。
11. Supervisor 在 `TaskRouterMixin` 中按 result action 路由，可能输出 `outbound_message`，也可能创建子任务、切换节点、压缩上下文或恢复父任务。
12. EventRouter 轮询到输出事件后，调用 AdapterCallbacks，由 Bot Adapter 发送或编辑平台消息。

其中 `llm_call` 和 `tool_call` 是生命周期中的逻辑阶段。当前事件流中更常见的具体事件名称包括 `stream_delta`、`context_usage`、`llm_retry`、`handoff_progress`、`intermediate_reply` 和 `node_completed`。

## 6. 节点系统

节点由 yaml 配置定义。用户配置目录是 `config/nodes/`，系统内建节点目录是 `engine/system_nodes/`。加载时优先读取系统节点目录，找不到时再读取用户节点目录。

一个 node yaml 通常描述以下内容：

- `id`：节点标识，对应文件名和任务中的 `node_id`。
- `type`：节点类型，当前主要是 `ai` 或 `tool`。
- `name`、`description`：节点展示名称和说明。
- `model`、`provider`、`api_key`、`base_url`、`provider_options`：模型与 provider 配置。
- `prompt`：系统提示词，支持字符串或 block 列表。
- `tool_access`：工具访问控制，支持 none、all、allowlist 和 deny。
- `skills`：技能访问控制。
- `memories`：记忆访问控制。
- `tool_mode`：工具调用格式，例如 fake-native、native 或 json。
- `output_mode`：输出模式，例如 tool_only 或 hybrid。
- `delegate_targets`：允许当前节点委派的目标节点列表。

节点系统把人格、能力、工具权限和模型路由放入配置层。Engine 只需要根据 `node_id` 加载配置并执行统一流程，因此新增节点通常不需要修改推理主循环代码。
