# 外部 Bot 接入指南

本文档面向外部渠道适配器（Telegram / Discord / 任意平台）的开发者。

Clonoth 的外部 Bot 只需做三件事：

1. 把用户输入变成 `POST /v1/inbound`
2. 轮询事件流 `GET /v1/sessions/{session_id}/events`
3. 遇到审批请求时，把 allow/deny 决策写回 `POST /v1/approvals/{approval_id}`

---

## 1. 基本概念

### 1.1 channel / conversation_key / session_id

- `channel`：渠道名。例如 `telegram`、`discord`、`cli`。
- `conversation_key`：外部会话的稳定标识，由 Adapter 自行定义。
  - Telegram 建议 `telegram:{chat_id}`
  - Discord 建议 `discord:{channel_id}`
- `session_id`：Clonoth 内部会话 ID。由 Supervisor 创建并返回。Supervisor 维护 `conversation_key → session_id` 映射。

### 1.2 message_id

`/v1/inbound` 支持传 `message_id`。Supervisor 当前不做去重。如果平台是 at-least-once 投递，Adapter 需自行去重。

### 1.3 task 运行时

系统采用平坦的 task 运行时。每条用户消息到达后，Supervisor 立即创建一个入口节点 task。后续的节点调度、工具调用都以独立 task 的形式在队列中流转，由 Engine worker 逐个领取执行。

同一个 session 中可以同时存在多个 task。新消息不会自动取消旧 task。入口节点的 AI 会根据上下文判断是否需要取消旧任务。

---

## 2. 消息流

```
User → Bot → POST /v1/inbound → Supervisor 创建入口 task
                                      ↓
                              Engine worker 领取 task
                                      ↓
                              AI 节点执行 → 可能产生下游 task（工具调用、handoff）
                                      ↓
                              最终 outbound_message 事件
                                      ↓
User ← Bot ← GET /v1/sessions/{id}/events ← events[]
```

Adapter 不需要关心 Engine 内部的节点调度和 task 流转。只需消费事件流。

---

## 3. POST /v1/inbound

请求：

```json
{
  "channel": "telegram",
  "conversation_key": "telegram:123456",
  "message_id": "123456:7890",
  "text": "帮我看看项目结构"
}
```

响应：

```json
{
  "session_id": "<uuid>",
  "accepted": true
}
```

用户在等待回复期间再次发消息时，直接再次调用此接口即可。Supervisor 会为新消息创建新的入口 task。

---

## 4. GET /v1/sessions/{session_id}/events

```
GET /v1/sessions/{session_id}/events?after_seq=123
```

返回一组事件（按 seq 递增）。Adapter 维护 `after_seq` 指针即可。

### 4.1 需要关注的事件类型

| 类型 | 说明 |
|---|---|
| `outbound_message` | 最终回复。把 `payload.text` 发给用户。 |
| `stream_delta` | 流式文本片段。`payload.type` 为 `text` 或 `thinking`，`payload.content` 为文本块。 |
| `stream_end` | 流式输出结束。 |
| `node_started` | 节点开始执行。可用于显示进度。 |
| `node_completed` | 节点执行完成。 |
| `handoff_progress` | 节点间交接进度。可选展示，建议节流。 |
| `approval_requested` | 需要人类审批。 |
| `cancel_acknowledged` | 任务已取消。Adapter 可以据此停止等待回复。 |

`stream_delta` 和 `stream_end` 是 transient 事件，不落盘。如果 Adapter 重启后错过了，不影响最终结果（`outbound_message` 仍会正常到达）。

---

## 5. 任务取消

### 5.1 用户主动取消

Adapter 可以在用户触发取消操作时调用：

```
POST /v1/sessions/{session_id}/cancel
```

这会递增 session generation，使该 session 内所有活跃 task 失效。Engine 在 0.3 秒内检测到取消并停止执行（包括正在进行的 LLM 调用和子进程命令）。

取消成功后，事件流中会出现 `cancel_acknowledged` 事件。

### 5.2 用户发新消息隐式取消

用户在旧任务执行期间发新消息时，Supervisor 会创建新的入口 task。入口节点的 AI 看到新旧消息的上下文后，会自行决定是否取消旧任务（通过 `cancel_active_tasks` 工具）。

Adapter 不需要做任何额外处理。

### 5.3 查询取消状态

```
GET /v1/sessions/{session_id}/cancelled
```

返回 `{"cancelled": true/false}`。

---

## 6. 审批流

当事件中出现 `approval_requested` 时：

1. 展示审批内容（`payload.operation`、`payload.details`、`payload.fingerprint`）
2. 管理员决策后调用：

```
POST /v1/approvals/{approval_id}
{"decision": "allow", "comment": "approved via telegram"}
```

如果平台不支持交互，可以收到审批后自动 deny。

---

## 7. Adapter 工程建议

一个生产可用的 Adapter 包含：

- **Receiver**：接 webhook 或 poll updates
- **Deduper**：基于 message_id 去重
- **Dispatcher**：调用 `/v1/inbound`
- **Event Poller**：按 session 维护 `after_seq` 拉取事件
- **Sender**：把事件转发回平台

### 7.1 after_seq 持久化

以 `session_id` 为 key，每次处理完 events 后落盘。Adapter 重启后从上次 seq 继续拉取。

### 7.2 progress 节流

`handoff_progress` 在复杂处理中可能很频繁。建议每 N 秒合并一次，或只展示关键步骤。

### 7.3 流式输出

如果平台支持消息编辑（如 Telegram `editMessageText`），可以用 `stream_delta` 实现逐步更新显示。否则忽略流式事件，只用 `outbound_message`。

### 7.4 取消按钮

建议在回复等待期间提供取消按钮（如 Telegram inline keyboard），点击后调用 `POST /v1/sessions/{session_id}/cancel`。

---

## 8. 安全与部署

1. 不要把 Supervisor 直接暴露到公网。`/v1/config/openai/secret` 会返回 api_key。
2. Adapter 尽量与 Supervisor 同机部署。
3. 区分用户 Bot 和管理员审批 Bot。普通用户不应看到 approval details。
