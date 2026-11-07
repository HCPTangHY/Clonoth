"""Context-window inspection. Worker control is handled by Engine pseudo-tools."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext


async def get_context_window(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return structured usage and a model-visible summary for the routed session."""
    try:
        route_session_id = ctx.route_session_id()
        response = await ctx.http.get(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/context_window",
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return {"ok": True, "data": {"result": f"Context window retrieved: {payload}", "value": payload}}
        effective = payload.get("effective_tokens")
        if effective is None:
            effective = payload.get("prompt_tokens") or payload.get("estimated_tokens") or 0
        threshold = int(payload.get("compact_threshold") or 0)
        utilization = float(payload.get("utilization") or 0.0) * 100.0
        remaining = max(0, threshold - int(effective)) if threshold else 0
        source = str(payload.get("source") or "estimate")
        messages = int(payload.get("message_count") or 0)
        result = (
            f"Context window usage: {int(effective):,} / {threshold:,} tokens "
            f"({utilization:.1f}%, remaining: {remaining:,} tokens), "
            f"source={source}, messages={messages}."
        )
        return {"ok": True, "data": {**payload, "result": result}}
    except Exception as exc:
        text = str(exc)
        return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}
