"""ClonothClient — 封装与 Clonoth Supervisor 的全部 HTTP 交互。

Phase 1 (2026-04-17): 初始创建，从 bot_adapter.py 中提取所有直接 HTTP 调用。
使用 httpx.AsyncClient 作为底层 HTTP 库。
SDK 是纯协议层，不包含任何平台（Discord / Telegram 等）相关逻辑。

封装的 API 端点及对应原始代码位置：
  POST   /v1/inbound                         ← _submit_inbound()
  GET    /v1/events                           ← _outbound_poller()
  POST   /v1/approvals/{id}                   ← _auto_approve(), ApprovalView
  POST   /v1/tasks/{id}/cancel                ← CancelView.cancel_task
  POST   /v1/sessions/{id}/cancel_active_tasks← CancelView fallback
  POST   /v1/tasks/{id}/preempt               ← _handle_agent_inner() Preempt V2
  GET    /v1/sessions/{id}/running_tasks      ← _handle_agent_inner() preempt 目标查找
  GET    /v1/health                           ← on_ready() workspace_root 获取
  GET    /v1/config/openai                    ← !model show
  POST   /v1/config/openai                    ← !model set/key/url
  POST   /v1/admin/restart                    ← 重启流程
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from .types import Event, HealthInfo, InboundResult, OpenAIConfig, RunningTask


class ClonothClient:
    """Clonoth Supervisor HTTP API 客户端。

    封装所有与 Supervisor 的 HTTP 交互，提供类型安全的高层接口。
    适配器（Bot）通过此类与 Clonoth 后端通信，不需要直接构造 HTTP 请求。

    用法::

        client = ClonothClient("http://127.0.0.1:8765")
        result = await client.submit_inbound(
            channel="discord_guild",
            conversation_key="discord:123456",
            text="你好",
        )
        events = await client.poll_events(after_seq=0)
        await client.close()
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0, admin_token: str = "",
                 admin_token_path: str = ""):
        """初始化客户端。

        Args:
            base_url: Supervisor HTTP API 地址，如 "http://127.0.0.1:8765"
            timeout: 每次 HTTP 请求的超时时间（秒）
            admin_token: Supervisor admin token，用于鉴权受保护的端点
            admin_token_path: admin token 文件路径，每次请求时重新读取
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._admin_token = admin_token.strip()
        self._admin_token_path = admin_token_path
        self._client: httpx.AsyncClient | None = None
        self._last_token: str = self._admin_token

    @property
    def base_url(self) -> str:
        """Supervisor 基础地址（只读）。"""
        return self._base_url

    def _resolve_admin_token(self) -> str:
        """获取当前 admin token：优先从文件读取最新值，fallback 到初始值。"""
        if self._admin_token_path:
            try:
                from pathlib import Path
                p = Path(self._admin_token_path)
                if p.exists():
                    token = p.read_text().strip()
                    if token:
                        return token
            except Exception:
                pass
        return self._admin_token

    def _http(self) -> httpx.AsyncClient:
        """获取或创建共享 httpx 客户端。token 变化时自动重建。"""
        current_token = self._resolve_admin_token()
        # token 变了 → 关旧 client，重建
        if self._client is not None and not self._client.is_closed and current_token != self._last_token:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._client.aclose())
            except RuntimeError:
                pass
            self._client = None
        if self._client is None or self._client.is_closed:
            headers = {}
            if current_token:
                headers["Authorization"] = f"Bearer {current_token}"
            self._client = httpx.AsyncClient(timeout=self._timeout, headers=headers)
            self._last_token = current_token
        return self._client

    # ================================================================
    #  Inbound — 提交用户消息
    # ================================================================

    async def submit_inbound(
        self,
        *,
        channel: str,
        conversation_key: str,
        text: str,
        message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        use_context: bool = True,
        entry_node_id: str | None = None,
    ) -> InboundResult:
        """提交用户消息到 Supervisor。

        对应 POST /v1/inbound。
        提取自 bot_adapter.py _submit_inbound() 中的 HTTP 调用逻辑。

        Args:
            channel: 频道类型标识，如 "discord_guild"、"discord_dm"、"cli"
            conversation_key: 会话键，如 "discord:123456"
            text: 用户消息文本（含历史上下文）
            message_id: 平台消息 ID（可选）
            attachments: 附件列表（可选），每项为 {"path": ..., "name": ...} 格式
            use_context: 是否使用对话上下文，默认 True
            entry_node_id: 入口节点 ID（可选，覆盖默认路由）

        Returns:
            InboundResult 包含 session_id 和 inbound_seq

        Raises:
            httpx.HTTPStatusError: HTTP 请求失败时抛出
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conversation_key,
            "text": text,
            "use_context": use_context,
        }
        if message_id is not None:
            payload["message_id"] = message_id
        if attachments:
            payload["attachments"] = attachments
        if entry_node_id:
            payload["entry_node_id"] = entry_node_id

        resp = await self._http().post(f"{self._base_url}/v1/inbound", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return InboundResult(
            session_id=data.get("session_id", ""),
            inbound_seq=int(data.get("inbound_seq", 0) or 0),
            accepted=bool(data.get("accepted", True)),
        )

    # ================================================================
    #  Events — 事件流轮询
    # ================================================================

    async def poll_events(
        self,
        *,
        after_seq: int = 0,
        types: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """从全局事件流拉取新事件。

        对应 GET /v1/events。
        提取自 bot_adapter.py _outbound_poller() 中的事件轮询逻辑。

        Args:
            after_seq: 只返回 seq 大于此值的事件
            types: 逗号分隔的事件类型过滤器（可选），如
                "inbound_message,outbound_message,approval_requested"

        Returns:
            事件列表，按 seq 升序排列
        """
        params: dict[str, Any] = {"after_seq": after_seq}
        if types:
            params["types"] = types
        if limit:
            params["limit"] = limit

        resp = await self._http().get(f"{self._base_url}/v1/events", params=params)
        resp.raise_for_status()
        return [Event.from_dict(e) for e in resp.json()]

    async def ws_connect(self, last_seq: int = 0) -> AsyncGenerator[dict[str, Any], None]:
        """连接全局 WebSocket 事件流，逐条 yield 解析后的事件字典。

        对应 WS /v1/ws。调用方发送 {"last_seq": N} 作为协议兼容帧，
        Supervisor 不再 replay 历史事件，仅推送连接后的实时事件。
        """
        # [SDK WS 2026-05-19] Why: EventRouter needs lower-latency delivery than
        # HTTP polling while keeping /v1/events as fallback. How: derive /v1/ws
        # from the configured HTTP base URL and send the durable EventLog cursor as
        # the first frame. Purpose: let adapters consume the global event stream
        # without learning websocket URL construction or handshake details.
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - exercised only in lean installs.
            raise RuntimeError("websockets package is required for ClonothClient.ws_connect") from exc

        if self._base_url.startswith("https://"):
            ws_url = "wss://" + self._base_url[len("https://"):]
        elif self._base_url.startswith("http://"):
            ws_url = "ws://" + self._base_url[len("http://"):]
        else:
            ws_url = self._base_url
        ws_url = f"{ws_url}/v1/ws"

        connect_kwargs: dict[str, Any] = {"open_timeout": self._timeout}
        if self._admin_token:
            # Why: deployments may protect every Supervisor endpoint with the same
            # bearer token used by HTTP. How: pass the header through websockets'
            # extra_headers parameter. Purpose: keep WS auth behavior aligned with
            # the shared httpx client.
            connect_kwargs["extra_headers"] = {"Authorization": f"Bearer {self._admin_token}"}

        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            await ws.send(json.dumps({"last_seq": int(last_seq or 0)}))
            async for message in ws:
                data = json.loads(message)
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "ping":
                    # Why: Supervisor sends heartbeat frames when the stream is
                    # idle. How: consume them here instead of yielding pseudo
                    # events. Purpose: EventRouter and adapters only see durable
                    # EventLog rows.
                    continue
                yield data

    # ================================================================
    #  Approval — 审批决策
    # ================================================================

    async def approve(
        self,
        approval_id: str,
        *,
        decision: str,
        comment: str | None = None,
    ) -> bool:
        """提交审批决策。

        对应 POST /v1/approvals/{approval_id}。
        提取自 bot_adapter.py _auto_approve() 和 ApprovalView 按钮回调。

        Args:
            approval_id: 审批请求 ID
            decision: "allow" 或 "deny"
            comment: 附加说明（可选）

        Returns:
            True 表示提交成功（HTTP 状态码 < 400）
        """
        body: dict[str, Any] = {"decision": decision}
        if comment is not None:
            body["comment"] = comment
        resp = await self._http().post(
            f"{self._base_url}/v1/approvals/{approval_id}", json=body,
        )
        return resp.status_code < 400

    # ================================================================
    #  Cancel — 取消任务
    # ================================================================

    async def cancel_task(self, task_id: str) -> bool:
        """取消单个任务及其子任务链。

        对应 POST /v1/tasks/{task_id}/cancel。
        提取自 bot_adapter.py CancelView.cancel_task 精准取消逻辑。

        Returns:
            True 表示取消成功，False 表示任务已不存在或已完成
        """
        resp = await self._http().post(
            f"{self._base_url}/v1/tasks/{task_id}/cancel",
        )
        return resp.status_code < 400

    async def cancel_active_tasks(self, session_id: str) -> bool:
        """取消指定 session 中所有活跃任务。

        对应 POST /v1/sessions/{session_id}/cancel_active_tasks。
        提取自 bot_adapter.py CancelView fallback 逻辑（task_id 未回填时）。

        Returns:
            True 表示请求成功
        """
        resp = await self._http().post(
            f"{self._base_url}/v1/sessions/{session_id}/cancel_active_tasks",
        )
        return resp.status_code < 400

    # ================================================================
    #  Preempt — 打断正在运行的任务（Preempt V2）
    # ================================================================

    async def preempt_task(
        self,
        task_id: str,
        *,
        message: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> bool:
        """向正在运行的任务注入 preempt 请求。

        对应 POST /v1/tasks/{task_id}/preempt。
        提取自 bot_adapter.py _handle_agent_inner() 中的 Preempt V2 逻辑。
        被打断的任务会在当前原子操作完成后保存上下文快照并退出，
        新消息通过 message 参数注入到任务中继续处理。

        Args:
            task_id: 要打断的任务 ID
            message: 注入的新消息文本
            attachments: 注入的附件列表（可选）

        Returns:
            True 表示 preempt 请求已被 Supervisor 接受
        """
        body: dict[str, Any] = {"message": message}
        if attachments:
            body["attachments"] = attachments
        resp = await self._http().post(
            f"{self._base_url}/v1/tasks/{task_id}/preempt", json=body,
        )
        if resp.status_code == 200:
            return bool(resp.json().get("ok"))
        return False

    # ================================================================
    #  Query — 查询状态
    # ================================================================

    async def get_running_tasks(self, session_id: str) -> list[RunningTask]:
        """查询指定 session 的活跃任务列表。

        对应 GET /v1/sessions/{session_id}/running_tasks。
        提取自 bot_adapter.py _handle_agent_inner() 中的 preempt 目标查找逻辑。
        Supervisor 端会自动收割 lease 过期的僵尸任务。

        Returns:
            RunningTask 列表；session 不存在时返回空列表而非抛异常
        """
        resp = await self._http().get(
            f"{self._base_url}/v1/sessions/{session_id}/running_tasks",
        )
        if resp.status_code != 200:
            return []
        tasks_data = resp.json().get("tasks", [])
        return [
            RunningTask(
                task_id=t.get("task_id", ""),
                node_id=t.get("node_id", ""),
                status=t.get("status", ""),
                created_at=t.get("created_at", ""),
                caller_task_id=t.get("caller_task_id", ""),
                is_user_entry=bool(t.get("is_user_entry", False)),
                source_inbound_seq=t.get("source_inbound_seq"),
            )
            for t in tasks_data
        ]

    async def get_health(self) -> HealthInfo:
        """查询 Supervisor 健康状态。

        对应 GET /v1/health。
        提取自 bot_adapter.py on_ready() 中获取 workspace_root 的逻辑。
        启动时可通过此方法动态获取工作区路径。

        Raises:
            httpx.HTTPStatusError: Supervisor 不可达时抛出
        """
        resp = await self._http().get(f"{self._base_url}/v1/health")
        resp.raise_for_status()
        d = resp.json()
        return HealthInfo(
            status=d.get("status", "ok"),
            run_id=d.get("run_id", ""),
            workspace_root=d.get("workspace_root", ""),
            started_at=str(d.get("started_at", "")),
            uptime_seconds=float(d.get("uptime_seconds", 0)),
        )

    async def get_openai_config(self) -> OpenAIConfig:
        """查询当前 OpenAI 兼容 API 配置（公开信息）。

        对应 GET /v1/config/openai。
        提取自 bot_adapter.py !model show 命令。

        Raises:
            httpx.HTTPStatusError: 请求失败时抛出
        """
        resp = await self._http().get(f"{self._base_url}/v1/config/openai")
        resp.raise_for_status()
        d = resp.json()
        return OpenAIConfig(
            base_url=d.get("base_url", ""),
            model=d.get("model", ""),
            api_key_present=bool(d.get("api_key_present", False)),
            api_key=d.get("api_key", ""),
        )

    async def update_openai_config(
        self, **kwargs: Any,
    ) -> dict[str, Any]:
        """更新 OpenAI 兼容 API 配置。

        对应 POST /v1/config/openai。
        提取自 bot_adapter.py !model set/key/url 命令。

        Args:
            **kwargs: 要更新的字段，可选 model, base_url, api_key

        Returns:
            更新后的完整配置 dict（AppConfigPublic 格式）

        Raises:
            httpx.HTTPStatusError: 请求失败时抛出
        """
        resp = await self._http().post(
            f"{self._base_url}/v1/config/openai", json=kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- Session Provider Override ----

    async def get_session_provider_override(self, session_id: str) -> dict[str, Any]:
        """查询 session 的 provider 覆写配置。"""
        resp = await self._http().get(
            f"{self._base_url}/v1/sessions/{session_id}/provider_override",
        )
        resp.raise_for_status()
        return resp.json()

    async def set_session_provider_override(
        self, session_id: str, override: dict[str, Any],
    ) -> dict[str, Any]:
        """设置 session 的 provider 覆写配置。"""
        resp = await self._http().put(
            f"{self._base_url}/v1/sessions/{session_id}/provider_override",
            json=override,
        )
        resp.raise_for_status()
        return resp.json()

    async def clear_session_provider_override(self, session_id: str) -> dict[str, Any]:
        """清除 session 的 provider 覆写配置。"""
        resp = await self._http().delete(
            f"{self._base_url}/v1/sessions/{session_id}/provider_override",
        )
        resp.raise_for_status()
        return resp.json()

    async def restart(
        self,
        target: str,
        *,
        reason: str | None = None,
        approval_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """触发 Engine 或全量重启。

        对应 POST /v1/admin/restart。

        Args:
            target: "engine"（仅重启引擎）或 "all"（全量重启）
            reason: 重启原因（可选）
            approval_id: 关联的审批 ID（可选）
            session_id: 关联的会话 ID（可选，用于重启后向该会话发通知）

        Returns:
            True 表示重启已调度
        """
        body: dict[str, Any] = {"target": target}
        if reason is not None:
            body["reason"] = reason
        if approval_id is not None:
            body["approval_id"] = approval_id
        if session_id is not None:
            body["session_id"] = session_id
        resp = await self._http().post(
            f"{self._base_url}/v1/admin/restart", json=body,
        )
        if resp.status_code < 400:
            return bool(resp.json().get("scheduled", False))
        return False

    # ================================================================
    #  Attachments — 文件上传
    # ================================================================

    async def upload_attachment(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        conversation_key: str = "default",
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """上传附件到 Supervisor，返回服务端路径等信息。

        对应 POST /v1/attachments/upload。

        Args:
            file_bytes: 文件二进制内容
            filename: 文件名（含扩展名）
            conversation_key: 附件归属的对话标识
            content_type: MIME 类型，不传则由服务端推断

        Returns:
            包含 path, name, size, mime_type, type 的字典
        """
        files = {"file": (filename, file_bytes, content_type or "application/octet-stream")}
        resp = await self._http().post(
            f"{self._base_url}/v1/attachments/upload",
            params={"conversation_key": conversation_key},
            files=files,
        )
        resp.raise_for_status()
        return resp.json()

    # ================================================================
    #  Lifecycle — 资源管理
    # ================================================================

    async def close(self) -> None:
        """关闭底层 HTTP 连接。

        应在 Bot 关闭时调用，释放连接池资源。
        重复调用安全。
        """
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
