from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunc
    timeout_sec: float = 30.0
    stateful: bool = False


@dataclass
class RemoteWorker:
    worker_id: str
    namespace: str
    token: str
    worker_name: str = ""
    max_concurrency: int = 1
    heartbeat_interval_sec: float = 15.0
    capabilities: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, ToolDef] = field(default_factory=dict)

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
        *,
        timeout_sec: float = 30.0,
        stateful: bool = False,
    ) -> Callable[[ToolFunc], ToolFunc]:
        # [AutoC 2026-08-01] Provide a decorator registration API for worker tools.
        # Why: remote workers should be small scripts that declare capabilities next
        # to the implementation. How: store ToolDef metadata on the RemoteWorker
        # instance and return the original function. Purpose: worker authors can use
        # @worker.tool(...) without writing protocol boilerplate.
        def deco(func: ToolFunc) -> ToolFunc:
            self.tools[name] = ToolDef(
                name=name,
                description=description,
                input_schema=input_schema or {"type": "object", "properties": {}, "required": []},
                func=func,
                timeout_sec=timeout_sec,
                stateful=stateful,
            )
            return func
        return deco

    def _hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol_version": 1,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name or self.worker_id,
            "namespace": self.namespace,
            "max_concurrency": self.max_concurrency,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "capabilities": dict(self.capabilities),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "timeout_sec": t.timeout_sec,
                    "stateful": t.stateful,
                }
                for t in self.tools.values()
            ],
        }

    @staticmethod
    def _ws_url(supervisor_url: str) -> str:
        ws_url = supervisor_url.rstrip("/")
        if ws_url.startswith("http://"):
            ws_url = "ws://" + ws_url[len("http://"):]
        elif ws_url.startswith("https://"):
            ws_url = "wss://" + ws_url[len("https://"):]
        return ws_url + "/v1/ws/worker"

    async def run(self, supervisor_url: str) -> None:
        # [AutoC 2026-08-01] Maintain a persistent worker WebSocket with backoff.
        # Why: desktop and edge workers may restart or lose network access. How: send
        # hello after every connection, serve calls until disconnect, then reconnect
        # with exponential delay. Purpose: workers can be long-running processes with
        # no external process manager requirements.
        ws_url = self._ws_url(supervisor_url)
        delay = 1.0
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                async with websockets.connect(ws_url, extra_headers=headers, ping_interval=None) as ws:
                    await ws.send(json.dumps(self._hello(), ensure_ascii=False))
                    ack = json.loads(await ws.recv())
                    if not ack.get("accepted"):
                        raise RuntimeError(ack.get("error") or "worker rejected")
                    logger.info("remote worker connected: worker_id=%s namespace=%s", self.worker_id, self.namespace)
                    delay = 1.0
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("remote worker disconnected: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _serve(self, ws: Any) -> None:
        running: dict[str, asyncio.Task[None]] = {}

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(max(1.0, float(self.heartbeat_interval_sec or 15.0)))
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "worker_id": self.worker_id,
                    "running_calls": len(running),
                    "time": time.time(),
                }, ensure_ascii=False))

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                typ = msg.get("type")
                if typ == "tool_call":
                    call_id = str(msg.get("call_id") or uuid.uuid4())
                    if len(running) >= max(1, int(self.max_concurrency or 1)):
                        await self._send_result(ws, {
                            "type": "tool_result",
                            "call_id": call_id,
                            "ok": False,
                            "error": "worker concurrency limit reached",
                            "data": {"result": "ERROR: worker concurrency limit reached"},
                        })
                        continue
                    task = asyncio.create_task(self._handle_call(ws, msg))
                    running[call_id] = task
                    task.add_done_callback(lambda _t, cid=call_id: running.pop(cid, None))
                elif typ == "cancel_tool_call":
                    call_id = str(msg.get("call_id") or "")
                    task = running.get(call_id)
                    if task:
                        task.cancel()
                        await ws.send(json.dumps({"type": "tool_cancelled", "call_id": call_id, "ok": True}, ensure_ascii=False))
                elif typ == "heartbeat_ack":
                    continue
        finally:
            heartbeat_task.cancel()
            for task in running.values():
                task.cancel()
            await asyncio.gather(*running.values(), heartbeat_task, return_exceptions=True)

    async def _send_result(self, ws: Any, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False, default=str))

    async def _handle_call(self, ws: Any, msg: dict[str, Any]) -> None:
        call_id = str(msg.get("call_id") or "")
        tool_name = str(msg.get("tool_name") or "")
        args = msg.get("arguments") if isinstance(msg.get("arguments"), dict) else {}
        tool = self.tools.get(tool_name)
        if tool is None:
            await self._send_result(ws, {
                "type": "tool_result",
                "call_id": call_id,
                "ok": False,
                "error": f"unknown tool: {tool_name}",
                "data": {"result": f"ERROR: unknown tool: {tool_name}"},
            })
            return

        started = time.monotonic()
        try:
            result = tool.func(args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=tool.timeout_sec)
            if not isinstance(result, dict):
                result = {"ok": True, "data": {"result": str(result), "value": result}}
            result.setdefault("type", "tool_result")
            result.setdefault("call_id", call_id)
            result.setdefault("ok", True)
            result.setdefault("data", {})
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            await self._send_result(ws, result)
        except asyncio.CancelledError:
            await self._send_result(ws, {
                "type": "tool_result",
                "call_id": call_id,
                "ok": False,
                "cancelled": True,
                "error": "cancelled",
                "data": {"result": "ERROR: cancelled"},
            })
        except Exception as exc:
            await self._send_result(ws, {
                "type": "tool_result",
                "call_id": call_id,
                "ok": False,
                "error": str(exc),
                "data": {"result": f"ERROR: {exc}"},
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            })


__all__ = ["RemoteWorker", "ToolDef", "ToolFunc"]
