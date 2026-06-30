"""write_file — write a text file (policy + approval guarded)."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    content = str(args.get("content", ""))

    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path,
            "content_preview": content[:200],
            "content_len": len(content),
        },
    )
    if err is not None:
        # [AutoC 2026-05-31] Why: guard failures are also tool results and should
        # expose data.result for readable history. How: wrap the policy error in
        # the unified ok/data/error shape while preserving cancellation metadata.
        # Purpose: keep failed writes understandable in model traces.
        error_text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
        return {"ok": False, "error": error_text, "data": {"result": f"ERROR: {error_text}"}, "cancelled": bool(isinstance(err, dict) and err.get("cancelled"))}

    p = resolve_under_allowed_roots(ctx.workspace_root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    byte_count = len(content.encode("utf-8"))
    # [AutoC 2026-05-31] Why: write_file now follows the shared ok/data/error
    # response contract. How: put path and byte count under data and make data.result
    # the human-readable transcript. Purpose: let result_to_raw consume a uniform
    # readable field while callers still get structured metadata.
    return {"ok": True, "data": {"result": f"File written: {path}", "path": path, "bytes": byte_count}}
