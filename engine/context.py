from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class RunContext:
    """一次完整图执行的运行上下文。"""

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

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{self.session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception:
            pass
