# Schedule Script Type 设计文档

## 概述

在现有 Schedule 系统基础上新增 `type: script` 调度类型。允许定时执行外部脚本，将 stdout 输出作为 inbound 消息注入 LLM 处理链路。

## 动机

当前 Schedule 系统仅支持 `type: message`（注入固定文本触发 LLM）。对于需要**动态数据采集**的场景（如 X/Twitter 监控、RSS 拉取、API 轮询），缺乏原生支持，只能依赖独立 PM2 进程 + 硬编码 Discord Webhook 的方式，存在以下问题：

- Webhook URL 硬编码在脚本中，换频道需改代码
- 发送记录不进 Clonoth session/signals，不可追溯
- 消息格式各脚本各写一套，无法统一管理
- 多一个 PM2 进程占资源

## 设计

### 配置格式

```yaml
# data/schedules.yaml
- id: whale-sect
  cron: "*/30 * * * *"        # 每 30 分钟
  type: script                  # 新增类型
  command: "python scripts/whale_sect_check.py"  # 执行命令
  timeout: 60                   # 脚本超时（秒），默认 30
  text: "以下是最新推文，请转发到频道："  # 可选前缀，拼在 stdout 前面
  conversation_key: "discord:1490987028077084673"  # 输出目标
  workflow_id: ""               # 可选，指定处理节点
  enabled: true
  silent: true                  # stdout 为空时静默跳过，不触发 LLM
```

### 执行流程

```
cron 触发
  ↓
Scheduler 检测 type=script
  ↓
subprocess.run(command, timeout=timeout, capture_output=True)
  ↓
┌─ stdout 非空 ─→ 拼接 text + stdout → 注入 inbound_message → LLM 处理 → outbound 发频道
│
└─ stdout 为空 ─→ silent=true 时跳过；silent=false 时仍注入 text（如果有）
  ↓
stderr 非空 → 写入 supervisor.log 作为警告
  ↓
返回码非 0 → 写入 supervisor.log 作为错误
```

### 脚本规范

脚本只负责**数据采集**，不负责消息投递：

- **stdout**：必须输出合法 JSON（见下方格式），Scheduler 用 `json.loads()` 解析
- **stderr**：日志/调试信息，不会进入 LLM
- **退出码 0**：正常
- **退出码非 0**：异常，Scheduler 记录错误但不中断后续调度
- **空 stdout**：表示「本次无新数据」，配合 `silent: true` 静默跳过
- **JSON 解析失败**：log error 并跳过，不注入 LLM（视为脚本 bug）

#### stdout JSON 格式

```json
{
  "text": "消息内容（必填）",
  "attachments": ["data/attachments/xxx.png"],
  "metadata": {"source": "whale-sect", "count": 3}
}
```

- `text`：必填，注入 inbound 的消息体
- `attachments`：可选，附件路径列表，复用现有 inbound 附件机制
- `metadata`：可选，LLM 可见的额外上下文信息

### LLM 侧行为

脚本输出注入后，LLM 收到的 inbound 消息格式：

```
[Schedule: whale-sect]
以下是最新推文，请转发到频道：

{stdout 内容}
```

LLM 根据 conversation_key 对应频道的节点配置，自主决定如何格式化和发送。

### 状态管理

脚本自行管理状态（如 seen_ids），Scheduler 不介入：

- 状态文件建议放在 `scripts/data/` 目录
- 脚本每次被调用是独立进程，无内存状态延续
- 幂等性由脚本自身保证

## 实现范围

### 需要修改的文件

- `supervisor/scheduler.py` — 新增 script 类型的调度分支
- `data/schedules.yaml` — 配置 schema 扩展（新增 command, timeout, silent 字段）

### 不需要修改的文件

- `supervisor/api.py` — 复用现有 inbound_message 注入逻辑
- `engine/` — 无变更，LLM 侧对 inbound 来源无感知
- `clonoth_sdk/` — 无变更

## 与现有 whale-sect 的迁移路径

1. 将 `whale_sect_monitor.py` 拆分为 `whale_sect_check.py`（单次检查，stdout 输出新推文）
2. 在 schedules.yaml 中配置 script 类型任务
3. 移除 PM2 中的 `whale-sect` 常驻进程
4. LLM 节点配置中加入推文转发指令（或创建专用 workflow）

## 设计决策（2026.4.28 确认）

1. **不设直通模式**：所有脚本输出必须走 inbound → LLM 处理链路，不允许跳过 LLM 直接发 outbound。保证消息管道统一，可追溯。
2. **防重入**：Scheduler 维护 `running_scripts: set[str]`，同 id 脚本正在执行时跳过本次 cron 触发并记录日志。脚本结束后从集合移除。
3. **附件复用现有机制**：脚本输出的附件路径通过 inbound 消息传递，复用节点现有的附件处理逻辑。保证 schedule 和节点的入口统一为 inbound。
