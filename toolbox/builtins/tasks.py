"""Task management: cancel_active_tasks, list_active_tasks."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: task-management tools return supervisor payloads
    # whose shape can vary. How: wrap them under data with a stable result string.
    # Purpose: keep task tools in the same ok/data/error contract.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: supervisor HTTP errors should expose data.result for
    # readable history. How: normalize the error string once. Purpose: avoid legacy
    # ok=false payloads from task tools.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


async def cancel_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Cancel all active downstream tasks in the current session."""
    try:
        # [Fork/Merge 2026-05-17] Why: this tool is session-scoped, but a caller
        # may be executing inside a temporary entry branch. How: use ToolContext's
        # parent-first route session. Purpose: sibling branch tasks under the same
        # user conversation can be cancelled together.
        route_session_id = ctx.route_session_id()
        # [2026-05-28] 支持可选 node_id 过滤：只取消指定节点的活跃任务。
        # 为什么：有时只需取消某个子节点的任务，而非 session 内全部。
        # 怎么改：如果 args 中有 node_id，作为 query param 传给 supervisor。
        # 目的：更细粒度的任务取消控制。
        params: dict[str, str] = {"exclude_task_id": ctx.task_id}
        _node_id = str(args.get("node_id") or "").strip()
        if _node_id:
            params["node_id"] = _node_id
        r = await ctx.http.post(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
            params=params,
        )
        if r.status_code >= 400:
            return _err(r.text)
        payload = r.json()
        return _ok("Active tasks cancelled", response=payload)
    except Exception as e:
        return _err(e)


async def list_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List active tasks in the current session."""
    note = "活跃任务信息已在系统上下文中注入，无需额外查询。"
    # [AutoC 2026-05-31] Why: list_active_tasks is a lightweight informational
    # tool but must still provide data.result. How: return the note as both result
    # and structured note. Purpose: keep the response schema uniform.
    return _ok(note, note=note)


async def get_context_window(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get current context window token usage for the session."""
    try:
        # [Fork/Merge 2026-05-17] Why: context usage events are emitted to the
        # parent route session, not to branch storage sessions. How: query the
        # same parent-first route used by tool events. Purpose: avoid returning an
        # empty branch estimate while the parent has the real usage record.
        route_session_id = ctx.route_session_id()
        r = await ctx.http.get(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/context_window",
        )
        if r.status_code >= 400:
            return _err(r.text)
        payload = r.json()
        if isinstance(payload, dict):
            return _ok("Context window retrieved", **payload)
        return _ok("Context window retrieved", value=payload)
    except Exception as e:
        return _err(e)
