from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class KernelContext:
    supervisor_url: str
    session_id: str
    task_id: str
    worker_id: str
    workspace_root: Path
    http: httpx.AsyncClient
    registry: Any  # ToolRegistry (避免循环导入)

    # Runtime tuning knobs (loaded from config/runtime.yaml by kernel.worker)
    approval_poll_interval_sec: float = 0.5

    async def emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        await self.http.post(
            f"{self.supervisor_url}/v1/tasks/{self.task_id}/events",
            json={"type": type_, "payload": payload},
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(
            f"{self.supervisor_url}/v1/ops/request",
            json={"session_id": self.session_id, "op": op, "parameters": parameters},
        )
        r.raise_for_status()
        return r.json()

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        interval = float(poll_interval) if poll_interval is not None else float(self.approval_poll_interval_sec or 0.5)
        if interval <= 0:
            interval = 0.5
        while True:
            r = await self.http.get(f"{self.supervisor_url}/v1/approvals/{approval_id}")
            r.raise_for_status()
            approval = r.json()
            if approval.get("status") != "pending":
                return approval
            await asyncio.sleep(interval)
