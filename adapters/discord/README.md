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
python -m platform.discord.app

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
ExecStart=/usr/local/bin/python3.11 -m platform.discord.app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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
