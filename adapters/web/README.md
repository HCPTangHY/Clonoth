# Clonoth Web 前端

基于 React + Vite + Tailwind CSS 的 Web 管理界面。由 Supervisor 以静态文件形式挂载，提供对话、节点管理、模型配置、审批等功能。

## 目录结构

```
adapters/web/
└── frontend/
    ├── src/
    │   ├── components/     # React 组件
    │   │   ├── chat/       # 对话界面
    │   │   └── settings/   # 设置面板（节点、工具、模型、MCP 等）
    │   ├── api/            # Supervisor API 客户端
    │   ├── stores/         # Zustand 状态管理
    │   └── App.tsx         # 路由入口
    ├── dist/               # 构建产物（由 Supervisor 挂载）
    ├── vite.config.ts      # Vite 配置，base 路径为 /web/
    ├── tailwind.config.ts  # Tailwind CSS 配置
    ├── package.json
    └── tsconfig.json
```

## 前置依赖

- Node.js 18+
- npm 或 pnpm

## 构建步骤

### 1. 安装依赖

```bash
cd adapters/web/frontend
npm ci
```

`npm ci` 会严格按照 `package-lock.json` 安装依赖，更适合部署环境。日常开发需要更新依赖时再使用 `npm install`。

### 2. 开发模式

```bash
npm run dev
# 访问 http://localhost:5173/web/
```

开发模式下，API 请求需要 Supervisor 在 `http://127.0.0.1:8765` 运行。可在 `src/api/supervisorClient.ts` 中修改默认地址。

### 3. 生产构建

```bash
npm run build
```

构建产物输出到 `dist/` 目录。

### 4. 测试

```bash
# 运行全部测试
npm test

# 单次运行
npm run test:run
```

## 部署方式

### 方式一：Supervisor 自动挂载（默认）

Supervisor 启动时会自动检测 `adapters/web/frontend/dist/` 目录。如果存在，将其挂载到 `/web/` 路径：

```
http://{supervisor_host}:{supervisor_port}/web/
```

无需额外配置。只需确保 `dist/` 目录存在即可。

### 方式二：独立 HTTP 服务

也可以用 nginx 或其他 Web 服务器单独托管 `dist/` 目录。需要配置反向代理，将 `/v1/` 路径转发到 Supervisor API：

```nginx
server {
    listen 80;
    server_name clonoth.example.com;

    location /web/ {
        alias /path/to/clonoth/adapters/web/frontend/dist/;
        try_files $uri $uri/ /web/index.html;
    }

    location /v1/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## 生产部署注意事项

### 构建产物同步

在多实例部署中，应在 `clonoth_original` 中执行 `npm run build`，然后将整个 `dist/` 目录（包括 `index.html`）复制到各生产实例。不要在生产实例中单独构建，因为每次 Vite 构建会生成不同的文件名哈希，导致 `index.html` 引用的文件名与实际文件不匹配。

```bash
# 正确做法
cd /path/to/clonoth_original/adapters/web/frontend
npm ci
npm run build
rm -rf /path/to/production/adapters/web/frontend/dist
mkdir -p /path/to/production/adapters/web/frontend/dist
cp -a dist/. /path/to/production/adapters/web/frontend/dist/
```

⚠️ 注意：

- `cp -a dist/. 目标目录/` 复制的是 `dist` 目录中的内容。
- 不要写成 `cp -r dist/ 目标目录/dist/`，否则在目标目录已存在时可能得到 `dist/dist/` 这种嵌套目录。
- 多实例部署时，应先在同一个构建目录生成 `dist/`，再同步到各生产实例。

### Admin Token

设置面板中的部分功能（节点编辑、工具管理、模型配置、引擎重启等）需要 Admin Token。Token 存储在 `data/.admin_token` 文件中，首次使用时在前端设置页面输入。

### base 路径

`vite.config.ts` 中 `base` 设置为 `/web/`，与 Supervisor 的挂载路径一致。如需修改挂载路径，需同步修改 `vite.config.ts` 中的 `base` 和 Supervisor 中的 `app.mount()` 路径。

## 技术栈

- React 18
- Vite 6
- Tailwind CSS 4
- Zustand（状态管理）
- js-yaml（配置文件解析）
- react-markdown + remark-gfm（Markdown 渲染）
- Vitest + Testing Library（测试）
