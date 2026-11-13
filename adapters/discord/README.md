# Clonoth Discord 适配器

基于 discord.py 的 Discord Bot 适配器。通过 Clonoth SDK 与 Supervisor 通信，支持多节点委派、审批按钮、附件处理和 Bridge Server。

## 目录结构

```
adapters/discord/
├── app.py           # 入口：配置常量、DiscordRuntime、SDK 初始化、事件注册
├── main.py          # 入口（调用 app.main()）
├── agent.py         # Bridge Server、消息处理主逻辑、审批/取消 View
├── callbacks.py     # EventRouter 回调实现（发送 Discord 消息、编辑、React）
├── context.py       # 群聊上下文构建（历史记录、成员信息、提及解析）
├── messaging.py     # 附件收集、图片/文件 MIME 识别、媒体文本生成
├── extensions/      # 可选命令分发；ide_goal.py 为 IDE Goal 的独立 HTTP 桥
└── __init__.py
```

## 前置依赖

- Python 3.11+
- discord.py 2.x
- aiohttp（Bridge Server）
- PyYAML
- python-dotenv（可选，自动加载 .env）
- clonoth_sdk（项目根目录下的 SDK 包）

```bash
pip install discord.py aiohttp pyyaml python-dotenv
```

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DISCORD_TOKEN` | 是 | — | Discord Bot Token |
| `DISCORD_SUPERUSERS` | 建议 | 空 | 管理员 Discord User ID，逗号分隔 |
| `CLONOTH_URL` | 否 | `http://127.0.0.1:8765` | Supervisor API 地址 |
| `CLONOTH_WORKSPACE` | 否 | 项目根目录 | 工作区路径，用于附件存储和 admin token 读取 |
| `DISCORD_LOG_CHANNEL` | 否 | `0` | 日志频道 ID（0 表示不发送日志） |
| `DISCORD_BRIDGE_PORT` | 否 | `8768` | Bridge Server 监听端口 |
| `DISCORD_ENTRY_NODE` | 否 | `main` | 入口节点 ID |
| `DISCORD_NODE_NAMES` | 否 | `{}` | 节点显示名映射，JSON 格式 |
| `DISCORD_HISTORY_LEN` | 否 | `15` | 每个频道保留的历史消息条数 |
| `DISCORD_KEY_ROLES` | 否 | 空 | 上下文筛选：仅这些身份组名会写进模型可见的上下文身份标注（逗号分隔）。不是访问控制，不限制任何命令或权限。未设置时读 runtime config 的 `supervisor.process_manager.discord_key_roles` |

启动顺序：Bot 先通过 `/v1/health`（公开端点）确定有效 workspace，再读取该 workspace 下的 `data/.admin_token` 与 `data/policy.yaml` 的 `extra_roots`。若未设置 `CLONOTH_WORKSPACE` 且 Supervisor 为旧版本（health 不含 `workspace_root`），回退到项目根目录。

## 部署步骤

### 1. 创建 Discord Application

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用。
2. 在 Bot 页面获取 Token，开启以下 Privileged Gateway Intents：
   - Message Content Intent
   - Server Members Intent
3. 使用 OAuth2 URL Generator 生成邀请链接，勾选 `bot` 和 `applications.commands` scope，权限至少包含：Send Messages、Embed Links、Attach Files、Add Reactions、Read Message History、Use External Emojis。

### 2. 配置环境变量

在项目根目录创建 `.env` 文件（或通过 systemd、docker-compose 注入）：

```bash
DISCORD_TOKEN=your_bot_token_here
DISCORD_SUPERUSERS=123456789012345678,987654321098765432
CLONOTH_URL=http://127.0.0.1:8765
DISCORD_LOG_CHANNEL=1234567890
DISCORD_BRIDGE_PORT=8768
DISCORD_ENTRY_NODE=main
```

### 3. 确保 Supervisor 已启动

Discord Bot 依赖 Supervisor API。启动 Bot 前确认 Supervisor 已在 `CLONOTH_URL` 指定的地址上运行。Bot 启动时会自动重试连接最多 10 次。

### 4. 启动 Bot

```bash
# 方式一：直接运行
cd /path/to/clonoth
python -m adapters.discord.app

# 方式二：通过兼容性入口
python adapters/discord/main.py
```

### 5. systemd 服务（推荐）

```ini
[Unit]
Description=Clonoth Discord Bot
After=network.target clonoth-supervisor.service

[Service]
Type=simple
User=www
WorkingDirectory=/path/to/clonoth
EnvironmentFile=/path/to/clonoth/.env
ExecStart=/usr/local/bin/python3.11 -m adapters.discord.app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 可选命令：IDE Goal

发送 `!goal 目标正文`（无需 @Bot），Bot 会发送 **IDE 扩展 · 创建 Goal** 确认卡。
所有非 Bot 用户均可发起；仅原命令发起者可以点击“确认创建并开始”或“取消”。
确认固定 120 秒到期，他人点击不续期；修改原消息不改变卡内冻结的目标。
只支持正文，带附件的命令会被拒绝；长正文会随确认卡提供完整 `goal.txt` 供核对。

- 没有目标：确认后创建并开始；已完成目标：确认后替换，重置预算和 Goal 专属评审/证据记录，保留聊天历史和 Todo。
- 其他状态（包括暂停、受阻）：拒绝覆盖；本命令不提供修改、暂停、恢复或清除。
- 使用 IDE 的 `capabilities.create` 与 `action=create`，不改用 `submit`，也不先清除旧目标。未安装、未启用或旧版 IDE 会明确提示，不转普通聊天。
- 确认期间目标 revision、会话身份变化会拒绝。按钮一次性锁定，创建请求用原频道/消息 ID 幂等；网络或服务故障不自动重试。提交后 5xx 明确提示目标可能已保存，需到 Web 核对，不要盲目重发。
- 创建权限开放不等于批准工具执行；现有审批权限保持不变。服务返回暂停/受阻不会被显示成“已开始”。

实现仅在 `extensions/ide_goal.py`，`app.py` 只在历史记录之前调用通用命令分发。
原命令、提示与确认卡使用现有 transient ID 过滤，避免自动进入历史或被引用为模型上下文（沿用有界、进程内过滤，不保证跨 Bot 重启）。
通过 SDK 的已鉴权 HTTP 客户端访问 Supervisor，不直接导入 IDE Python 模块，不改 SDK/Core。
正文结果仍走现有 SDK 后台任务/频道发送路径，不新增结果投递流程。确认成功后，原 Embed 转为 **IDE 扩展 · Goal** 状态卡，移除确认按钮；显示整体状态、自动续轮数、暂停/受阻原因，以及当前轮的计时、工具与简短进度。只有 IDE 返回 completed 才显示目标完成，本轮正文发送完毕不代表验收通过。

状态卡由 `extensions/ide_goal_status.py` 管理，只读轮询 Goal 状态（活动时 3 秒、暂停/受阻时 10 秒），使用公开 `goal_id` 与 `execution.source_inbound_seq/task_id` 精确选取本轮进度。进度取自通用 `DiscordCallbacks.subscribe_progress()` 的同步通知，或准确 task_id 的只读 child-progress/终态查询（有界保留最近 32 个已结束任务），复用原格式化逻辑的副本，不重复建立工具状态机。非默认入口的根任务结束不等于 output 链完成，卡片会明确显示仍在等待输出，不提前标为本轮结束。HTTP 确认前只缓存进度，不宣称创建成功；失败便解除监听。

普通消息的原回复、附件、计时、取消按钮、工具/正文摘要与日志路径保持不变，Goal 卡不写入普通 trigger 的 status_msg 或 cancel_view。Goal 与普通消息同频道运行时仍按各自 source_inbound_seq 隔离。状态卡共享频道编辑限流，临时 Discord 编辑错误最多连续重试三次；每 60 秒检查卡片是否仍存在，即使暂停内容不变也能回收已删除卡。完成/清除/替换、插件不可用、多次读取或编辑失败时停止跟随，不补发新卡。暂停卡可继续显示后续明确恢复，但卡片绑定是进程内状态，Bot 重启后不自动恢复旧卡。

确认前只做读取和无任务的 `get_or_create`，因此取消后可留下空会话，但不产生 Goal 或模型任务。
新会话没有绑定/没有其他事件时，确认后按 `DISCORD_ENTRY_NODE` 初始化；取消后再次创建仍可初始化。
已有节点绑定保持不变。`active_node` 和最多两条会话事件用于识别未使用会话，切换前再读一次，
但现存 `switch_node` API 没有 CAS，**不保证跨 HTTP 的节点绑定原子性**；本扩展不为此新增 Core 接口。

离线验证：

```bash
mkdir -p data/temp/goal-create-discord/discord
TMPDIR="$PWD/data/temp/goal-create-discord/discord" python3.11 -B -m pytest \
  tests/test_discord_goal.py -q --basetemp=data/temp/goal-create-discord/discord/pytest
```

包含真实 app 消息入口、确认按钮、HTTP 替身、SDK 路由，以及隔离的真实 Supervisor/GoalService ASGI 链路（不访问 Discord Gateway 或生产服务，不调用真实模型）。

## 架构说明

### Bridge Server

Bot 启动后会在 `127.0.0.1:{DISCORD_BRIDGE_PORT}` 开启一个 HTTP 服务，供 Supervisor 回调使用。主要端点：

- `POST /execute` — 执行 discord.py 代码（discord_manage 工具）
- `POST /restart` — 触发 Bot 重启

### 消息流程

```
用户消息 → discord.py on_message
  → 构建上下文（历史、成员、附件）
  → SDK submit_inbound → Supervisor
  → Supervisor 处理并产生事件
  → EventRouter 轮询事件
  → callbacks 发送 Discord 回复
```

### 附件处理

附件通过 `_collect_attachments()` 从 Discord CDN 下载并保存到 `data/attachments/` 目录。如果 SDK client 已初始化且 Supervisor 提供了上传端点，会优先走 API 上传。

## Docker 注意事项

- Bot 本身只需出站网络连接（连 Discord Gateway + Supervisor API），不需要开放入站端口。
- `data/attachments/` 目录需挂载为持久卷，或确保 Bot 和 Supervisor 共享同一个卷。
- Bridge Server 端口需在 Bot 和 Supervisor 之间可达。

## 多实例部署

同一台机器部署多个 Bot 实例时，确保每个实例的 `DISCORD_BRIDGE_PORT` 不同。
