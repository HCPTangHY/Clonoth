from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class ToolContext:
    supervisor_url: str
    session_id: str
    run_id: str
    worker_id: str
    workspace_root: Path
    http: httpx.AsyncClient
    registry: Any
    task_id: str = ""
    session_generation: int = 0
    approval_poll_interval_sec: float = 0.5

    async def emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        await self.http.post(
            f"{self.supervisor_url}/v1/sessions/{self.session_id}/events",
            json={"type": type_, "payload": payload},
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(
            f"{self.supervisor_url}/v1/ops/request",
            json={"session_id": self.session_id, "op": op, "parameters": parameters},
        )
        r.raise_for_status()
        return r.json()

    async def check_cancelled(self) -> bool:
        try:
            if self.task_id:
                r = await self.http.get(f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled")
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                r = await self.http.get(f"{self.supervisor_url}/v1/sessions/{self.session_id}/cancelled")
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        interval = float(poll_interval) if poll_interval is not None else float(self.approval_poll_interval_sec or 0.5)
        if interval <= 0:
            interval = 0.5
        while True:
            if await self.check_cancelled():
                return {"approval_id": approval_id, "status": "cancelled"}
            r = await self.http.get(f"{self.supervisor_url}/v1/approvals/{approval_id}")
            r.raise_for_status()
            approval = r.json()
            if approval.get("status") != "pending":
                return approval
            await asyncio.sleep(interval)
