from __future__ import annotations

"""Save LLM request payload on provider error for post-mortem debugging.

Why: when a provider returns a non-OK response (400, 422, etc.), the actual
messages sent are lost after the request, making it impossible to diagnose
protocol mismatches (orphan tool_result, role alternation, etc.).
How: hook into after_llm_call, check resp.ok, and dump the full message list
plus error metadata to data/llm_error_snapshots/.
Purpose: provide forensic evidence for intermittent provider errors without
modifying any core code path.
"""

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .result import hook_result

logger = logging.getLogger(__name__)

_MAX_SNAPSHOTS = 30
_SNAP_DIR_NAME = "llm_error_snapshots"

PLUGIN_META = {
    "handler_class": "ErrorSnapshotHandler",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": -10,  # run after usage_tracker (priority 0)
    "name": "error_snapshot",
    "description": "Dump LLM request payload to disk on provider error",
}


class ErrorSnapshotHandler:
    """Save a JSON snapshot when the LLM provider returns a non-OK response."""

    name = "error_snapshot"
    priority = -10

    async def handle(self, ctx: Any) -> Any | None:
        resp = ctx.response
        if resp is None or getattr(resp, "ok", True):
            return None

        # Build snapshot
        try:
            rctx = ctx.rctx
            workspace = Path(getattr(rctx, "workspace_root", "."))
            snap_dir = workspace / "data" / _SNAP_DIR_NAME
            snap_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now(timezone.utc)
            sid = str(getattr(rctx, "session_id", "") or "")[:8]
            step = getattr(ctx, "step", 0)
            filename = f"{ts.strftime('%Y%m%d_%H%M%S')}_{sid}_s{step}.json"

            # Retain internal history separately from the reconstructed API payload.
            messages = deepcopy(ctx.messages or [])
            converted_payload = None
            converted_payload_reconstructed = False
            try:
                ls = ctx.extra.get("loop_state")
                request_history = getattr(ls, "request_history", None)
                formatter = getattr(ls, "formatter", None)
                tool_mode = getattr(formatter, "mode", None) or getattr(ctx.node, "tool_mode", "native")
                # Reconstruction uses the public provider contract, not captured wire data.
                converted_payload = ctx.provider.build_payload(
                    deepcopy(request_history if request_history is not None else messages),
                    tools=deepcopy(ctx.tools) or None,
                    tool_mode=tool_mode,
                    stream=bool(getattr(ls, "use_stream", False)),
                )
                converted_payload_reconstructed = True
            except Exception as conv_err:
                converted_payload = {"conversion_error": str(conv_err)}

            snapshot = {
                "timestamp": ts.isoformat(),
                "session_id": str(getattr(rctx, "session_id", "") or ""),
                "node_id": str(getattr(ctx.node, "id", "") or ""),
                "task_id": str(getattr(rctx, "task_id", "") or ""),
                "step": step,
                "provider": type(ctx.provider).__name__,
                "model": str(getattr(ctx.provider, "model", "unknown")),
                "status_code": getattr(resp, "status_code", None),
                "error": str(getattr(resp, "error", "") or ""),
                "message_count": len(messages),
                "messages": messages,
                "converted_payload": converted_payload,
                "converted_payload_reconstructed": converted_payload_reconstructed,
            }

            snap_path = snap_dir / filename
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

            # Housekeeping: keep newest N snapshots
            existing = sorted(snap_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in existing[:-_MAX_SNAPSHOTS]:
                try:
                    old.unlink()
                except OSError:
                    pass

            logger.info("error_snapshot: saved %s (%d msgs, error=%s)",
                        filename, len(messages), snapshot["error"][:80])
        except Exception as exc:
            logger.warning("error_snapshot: failed to save: %s", exc)

        return None
