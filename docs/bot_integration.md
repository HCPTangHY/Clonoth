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

---

## 2. 消息流

```
User → Bot → POST /v1/inbound → Supervisor → Engine 执行节点图 → outbound_message
                                                                         ↓
User ← Bot ← GET /v1/sessions/{id}/events ← events[]  ←────────────────┘
```

Adapter 不需要关心 Engine 内部的节点调度。只需消费事件流。

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
| `handoff_progress` | 节点移交进度。可选展示，建议节流。 |
| `approval_requested` | 需要人类审批。 |

`stream_delta` 和 `stream_end` 是 transient 事件，不落盘。如果 Adapter 重启后错过了，不影响最终结果（`outbound_message` 仍会正常到达）。

---

## 5. 审批流

当事件中出现 `approval_requested` 时：

1. 展示审批内容（`payload.operation`、`payload.details`、`payload.fingerprint`）
2. 管理员决策后调用：

```
POST /v1/approvals/{approval_id}
{"decision": "allow", "comment": "approved via telegram"}
```

如果平台不支持交互，可以收到审批后自动 deny。

---

## 6. Adapter 工程建议

一个生产可用的 Adapter 包含：

- **Receiver**：接 webhook 或 poll updates
- **Deduper**：基于 message_id 去重
- **Dispatcher**：调用 `/v1/inbound`
- **Event Poller**：按 session 维护 `after_seq` 拉取事件
- **Sender**：把事件转发回平台

### 6.1 after_seq 持久化

以 `session_id` 为 key，每次处理完 events 后落盘。Adapter 重启后从上次 seq 继续拉取。

### 6.2 progress 节流

`handoff_progress` 在复杂处理中可能很频繁。建议每 N 秒合并一次，或只展示关键步骤。

### 6.3 流式输出

如果平台支持消息编辑（如 Telegram `editMessageText`），可以用 `stream_delta` 实现逐步更新显示。否则忽略流式事件，只用 `outbound_message`。

---

## 7. 安全与部署

1. 不要把 Supervisor 直接暴露到公网。`/v1/config/openai/secret` 会返回 api_key。
2. Adapter 尽量与 Supervisor 同机部署。
3. 区分用户 Bot 和管理员审批 Bot。普通用户不应看到 approval details。
