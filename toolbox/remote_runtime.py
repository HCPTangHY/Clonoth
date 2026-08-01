from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

# Module-level httpx client for connection pooling across remote tool calls.
_http_client: httpx.AsyncClient | None = None


def _get_http_client(timeout: float = 360.0) -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=timeout, trust_env=False, headers={"User-Agent": "Clonoth"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
    return _http_client


def _result(ok: bool, result_text: str, *, error: str | None = None, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-08-01] Keep remote-runtime generated failures in the same tool
    # result shape as local tools. Why: broker errors, cancellation, and malformed
    # responses bypass worker code. How: mirror the human-readable message into
    # data.result and attach optional metadata at the top level. Purpose: engine
    # result formatting can treat remote tools like builtin, script, and MCP tools.
    data: dict[str, Any] = {"result": result_text}
    response: dict[str, Any] = {"ok": ok, "data": data, "attachments": []}
    if error:
        response["error"] = error
    response.update(fields)
    return response


def _normalize_tool_result(payload: Any) -> dict[str, Any]:
    # [AutoC 2026-08-01] Normalize broker/worker payloads at the engine boundary.
    # Why: Supervisor returns RemoteCallResultOut while workers may include extra
    # fields. How: preserve ok, data, error, attachments, cancelled, status, call_id,
    # and worker_id, adding data.result if missing. Purpose: ToolRegistry receives a
    # stable {ok,data,error,attachments} result without losing routing metadata.
    if not isinstance(payload, dict):
        return _result(True, str(payload), value=payload)

    ok = bool(payload.get("ok", True))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data = dict(data)
    error = payload.get("error")
    if not isinstance(data.get("result"), str):
        if ok:
            data["result"] = str(payload.get("status") or "remote tool completed")
        else:
            data["result"] = f"ERROR: {error or payload.get('status') or 'remote tool failed'}"

    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    response: dict[str, Any] = {
        "ok": ok,
        "data": data,
        "attachments": attachments,
    }
    if error is not None:
        response["error"] = str(error)
    for key in ("cancelled", "status", "call_id", "worker_id", "elapsed_ms"):
        if key in payload:
            response[key] = payload[key]
    return response


async def list_remote_tools(supervisor_url: str) -> list[dict[str, Any]]:
    # [AutoC 2026-08-01] Discover remote-worker tools through Supervisor HTTP.
    # Why: engine workers should not connect directly to NAT-side worker machines.
    # How: call the Phase 1 broker endpoint with a short-lived httpx AsyncClient and
    # unwrap the tools array. Purpose: ToolRegistry can register remote tools as
    # first-class model-visible tools during startup and hot reload.
    base_url = supervisor_url.rstrip("/")
    try:
        http = _get_http_client()
        resp = await http.get(f"{base_url}/v1/remote/tools", timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []

    tools = payload.get("tools") if isinstance(payload, dict) else []
    return [dict(item) for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []


def _context_payload(ctx: Any) -> dict[str, Any]:
    # [AutoC 2026-08-01] Pass routing metadata to the remote broker. Why: Supervisor
    # uses session/conversation identity for stateful worker affinity and logs remote
    # calls beside the originating task. How: read the same attributes carried by
    # ToolContext. Purpose: computer-use tools keep screenshot/click affinity per
    # visible conversation.
    route_session_id = ""
    route_method = getattr(ctx, "route_session_id", None)
    if callable(route_method):
        try:
            route_session_id = str(route_method() or "").strip()
        except Exception:
            route_session_id = ""
    return {
        "session_id": route_session_id or str(getattr(ctx, "session_id", "") or ""),
        "task_id": str(getattr(ctx, "task_id", "") or getattr(ctx, "run_id", "") or ""),
        "tool_call_id": str(getattr(ctx, "tool_call_id", "") or ""),
        "node_id": str(getattr(ctx, "node_id", "") or getattr(ctx, "_node_id", "") or ""),
        "conversation_key": str(getattr(ctx, "conversation_key", "") or ""),
        "worker_id": str(getattr(ctx, "worker_id", "") or ""),
    }


async def _cancel_remote_call(http: httpx.AsyncClient, base_url: str, call_id: str, reason: str) -> None:
    try:
        await http.post(f"{base_url}/v1/remote/calls/{call_id}/cancel", json={"reason": reason}, timeout=5.0)
    except Exception:
        pass


async def call_remote_tool(ctx: Any, registered_name: str, args: dict[str, Any], timeout: float, supervisor_url: str) -> dict[str, Any]:
    # [AutoC 2026-08-01] Execute one remote-worker tool through the Supervisor broker.
    # Why: remote workers are connected to Supervisor over WebSocket, while engine
    # code should keep the same ToolRegistry execution shape as local tools. How:
    # create a broker call, poll the result endpoint with wait_sec=2, and cancel the
    # broker call if ToolContext reports cancellation. Purpose: remote tools can be
    # used by AI/tool tasks with normal cancellation and result formatting behavior.
    base_url = supervisor_url.rstrip("/")
    timeout_sec = max(0.1, float(timeout or 60.0))
    deadline = time.monotonic() + timeout_sec
    call_id = ""

    http = _get_http_client()
    try:
        create_resp = await http.post(
            f"{base_url}/v1/remote/calls",
            json={
                "registered_name": registered_name,
                "arguments": args or {},
                "timeout_sec": timeout_sec,
                "context": _context_payload(ctx),
            },
        )
        create_resp.raise_for_status()
        created = create_resp.json()
        if not isinstance(created, dict) or not created.get("ok", True):
            return _result(False, "ERROR: remote call creation failed", error=str(created.get("error") if isinstance(created, dict) else "remote call creation failed"))
        call_id = str(created.get("call_id") or "")
        if not call_id:
            return _result(False, "ERROR: remote call missing call_id", error="remote call missing call_id")
    except httpx.HTTPStatusError as exc:
        text = exc.response.text[:500] if exc.response is not None else str(exc)
        return _result(False, f"ERROR: remote call creation failed: {text}", error=f"remote call creation failed: {text}")
    except Exception as exc:
        return _result(False, f"ERROR: remote call creation failed: {exc}", error=f"remote call creation failed: {exc}")

    while True:
        try:
            if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                await _cancel_remote_call(http, base_url, call_id, "task cancelled")
                return _result(False, "ERROR: remote tool cancelled", error="remote tool cancelled", cancelled=True, call_id=call_id)
        except Exception:
            pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _cancel_remote_call(http, base_url, call_id, "remote tool timeout")
            return _result(False, f"ERROR: remote tool timeout after {timeout_sec}s", error=f"remote tool timeout after {timeout_sec}s", call_id=call_id)

        wait_sec = max(0.1, min(2.0, remaining))
        try:
            result_resp = await http.get(
                f"{base_url}/v1/remote/calls/{call_id}/result",
                params={"wait_sec": wait_sec},
                timeout=wait_sec + 5.0,
            )
            if result_resp.status_code == 202:
                await asyncio.sleep(0)
                continue
            result_resp.raise_for_status()
            return _normalize_tool_result(result_resp.json())
        except httpx.HTTPStatusError as exc:
            text = exc.response.text[:500] if exc.response is not None else str(exc)
            return _result(False, f"ERROR: remote call result failed: {text}", error=f"remote call result failed: {text}", call_id=call_id)
        except Exception as exc:
            return _result(False, f"ERROR: remote call result failed: {exc}", error=f"remote call result failed: {exc}", call_id=call_id)

