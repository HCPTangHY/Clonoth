# 会话审批规则

会话审批规则由 Supervisor 保存和执行，不依赖浏览器在线。它不是对 `data/policy.yaml` 的覆盖：

1. policy 返回 `deny`：直接拒绝，会话规则不能放行。
2. policy 返回 `auto`：直接执行，会话规则不追加审批。
3. policy 返回 `approval_required`：沿用系统维护、定时任务的现有处理；普通任务再匹配会话规则。
4. 会话规则返回 `allow`：直接执行，不创建待审批。
5. 会话规则返回 `ask`，或没有会话规则：主任务创建待审批；普通子代理直接拒绝该操作，不创建待审批。

已创建的待审批不会因为切换规则而自动通过。修改规则只影响后续新的请求。关闭页面不会改变权限，也不会自动批准尚需人工决定的操作。

## 子代理不等待人工审批

普通子代理只执行 policy 或会话规则已允许的操作。仍需人工授权时，Supervisor 返回明确的工具错误，要求子代理使用已授权的方法或向主任务报告限制，不要重复提交同一操作。此行为固定，不增加配置，也不由主代理代批。

同步、异步、嵌套子代理以及它们派生的工具任务均适用。用户主任务的入口分支不是子代理，仍可创建审批。系统维护、定时任务的既有特殊处理保持不变。升级前已经创建的 pending 不追溯取消，仍需人工处理。

此行为适用于所有适配器。Discord/OneBot 原本在收到审批事件后自动批准的子操作，如果没有被 policy 或后端会话规则预先允许，现在会直接拒绝；不会再产生供客户端自动批准的事件。

## 数据格式

```json
{
  "version": 1,
  "default_action": "allow",
  "rules": [
    {"trust_levels": ["trusted", "external"], "action": "ask"},
    {"operation": "restart", "action": "ask"},
    {"tool": "request_restart", "action": "ask"},
    {"tool": "generate_image", "action": "ask"}
  ]
}
```

这份配置表示：对 policy 已要求审批的请求，跨工作区信任边界、重启和指定工具仍等待，其余自动放行。不需要列出所有允许的工具；后续增加的工具也按默认动作处理。

- `default_action`：`ask` 或 `allow`。
- `rules`：按顺序匹配，第一条命中决定动作；没有命中则使用默认动作。
- 同一条规则的多个条件必须同时满足。
- `tool`：外层工具名称，支持 glob 匹配。内部文件/命令检查仍携带外层工具名称。
- `operation`：精确匹配底层操作，如 `tool_call`、`read_file`、`write_file`、`execute_command`、`restart`。
- `trust_levels`：匹配 policy 给出的信任级别；空字符串表示无路径操作。
- `ask` 表示仍需人工授权：主任务等待审批；普通子代理不等待，直接返回权限限制。

规则保存在 `data/sessions.json`。没有本会话规则的子会话沿父会话关系查找，使用当前有效规则；子会话有显式配置时优先使用自身配置。规则不按 Web、Discord 等平台名称判断。

## API

接口沿用 Supervisor 的鉴权要求。

- `GET /v1/sessions/{session_id}/approval_rules`：获取本会话及有效规则。
- `PUT /v1/sessions/{session_id}/approval_rules`：请求体为完整规则对象，整体替换本会话规则。
- `DELETE /v1/sessions/{session_id}/approval_rules`：删除本会话配置，恢复继承；无父级规则时仅使用 policy。

响应：

```json
{
  "rules": null,
  "effective_rules": null,
  "source_session_id": null
}
```

`rules` 是本会话配置，`effective_rules` 是考虑继承后的配置，`source_session_id` 是规则来源。规则修改会产生 `session_approval_rules_changed` 事件。

`POST /v1/inbound` 支持可选 `approval_rules` 字段，在创建任务前保存。调用方不传该字段时，不修改现有规则。Web 仅在首次创建会话时携带默认配置，后续通过会话接口修改。

无浏览器调用方也可以使用这些接口预先设置授权。没有规则并不意味着全权限：仍需审批的主任务请求会等待，普通子代理请求直接拒绝。

## Web 行为

“手动、智能、YOLO”仅是 Web 对通用规则的预设，不是后端枚举。

- 手动：`default_action: ask`，无例外。不增加 policy 已自动放行操作的审批。
- 智能：默认允许；跨界路径、重启及用户关闭自动放行的工具等待。
- YOLO：`default_action: allow`，无例外；仍不能绕过 policy 的拒绝规则。
- 自定义：后端规则不匹配 Web 预设，或旧会话没有会话规则。用户可以主动选择预设替换，读取配置时不自动覆盖。

A 会话选择 YOLO 不会修改 B 会话。现有浏览器本地偏好只作为新会话默认值；已存在会话优先读取服务器配置。浏览器不再逐条自动提交审批，因此手机锁屏或页面被系统关闭后，已授权的操作仍可继续。

Discord、OneBot 的客户端未修改；但 Supervisor 的子代理不等待策略统一适用。
