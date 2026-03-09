from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class RunContext:
    """单个 task 执行片段的运行上下文。"""

    workspace_root: Path
    supervisor_url: str
    session_id: str
    worker_id: str
    http: httpx.AsyncClient
    llm_http: httpx.AsyncClient
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-4o-mini"
    user_text: str = ""
    task_id: str = ""
    session_generation: int = 0

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{self.session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception:
            pass

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
