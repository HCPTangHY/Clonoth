from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped

Env contract (injected by toolbox registry from ToolContext):
  CLONOTH_SUPERVISOR_URL  supervisor base url (empty -> local default)
  CLONOTH_ADMIN_TOKEN     admin bearer token (fallback: <root>/data/.admin_token)
  CLONOTH_TASK_ID         task executing this tool  -> reset exclude_task_id
  CLONOTH_SESSION_ID      its runtime session/branch -> reset exclude_session_id
  CLONOTH_WORKSPACE       install root (workspace_root), used for the token file
"""

SPEC = {
    'description': '清空指定 Discord 频道的对话上下文（Bot 端历史 + Engine session 上下文）。⚠️ 仅在用户明确要求清理/重置上下文时才可调用，禁止 AI 自行决定调用。副作用：会断开当前 session 的所有子任务回调链。channel_id 为 Discord 频道 ID。',
    'input_schema': {
        'properties': {'channel_id': {'description': 'Discord 频道 ID', 'type': 'string'}},
        'required': ['channel_id'],
        'type': 'object',
    },
    'name': 'clear_context',
}

TIMEOUT_SEC = 30.0
DEFAULT_SUPERVISOR_URL = "http://127.0.0.1:8765"
DEFAULT_BRIDGE_PORT = 8768


def _workspace_root():
    """Install root: CLONOTH_WORKSPACE, else the repo containing this tool."""
    import os
    from pathlib import Path

    raw = (os.environ.get("CLONOTH_WORKSPACE") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent


def _supervisor_url():
    import os

    return (os.environ.get("CLONOTH_SUPERVISOR_URL") or "").strip().rstrip("/") or DEFAULT_SUPERVISOR_URL


def _admin_token(root):
    """Bearer token, following the existing production convention only.

    [2026-10-31] Why: engine/runner.py and the previous clear_context both
    resolve credentials as CLONOTH_ADMIN_TOKEN -> <install>/data/.admin_token.
    How: keep exactly that order, never print the value. Purpose: one credential
    path across engine, bot and tools; no extra token aliases to drift apart.
    """
    import os

    token = (os.environ.get("CLONOTH_ADMIN_TOKEN") or "").strip()
    if token:
        return token
    try:
        path = root / "data" / ".admin_token"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _channel_key(channel_id):
    """Validate the channel id at the trust boundary and return its int key.

    Why: the id is embedded in Python source sent to the bot bridge. How: accept
    digits only (Discord snowflakes are numeric) and interpolate the resulting
    int, never the raw string. Purpose: no code injection through channel_id, and
    the int key matches the bot's _channel_history entries.
    """
    raw = str(channel_id if channel_id is not None else "").strip()
    if not raw:
        raise ValueError("channel_id is required")
    if not raw.isdigit():
        raise ValueError("channel_id must be a numeric Discord channel id")
    return int(raw)


def _bridge_port(value):
    """Bridge port as a bounded int; reject anything else instead of guessing."""
    raw = value if value not in (None, "", 0) else DEFAULT_BRIDGE_PORT
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("port must be an integer in 1..65535") from None
    if not 0 < port < 65536:
        raise ValueError("port must be an integer in 1..65535")
    return port


if __name__ == "__main__":
    import json, os, sys
    import httpx

    try:
        _input = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON input: {e}",
                          "data": {"result": f"ERROR: invalid JSON input: {e}"}}, ensure_ascii=False))
        sys.exit(1)

    def output(result, ok=True, error=""):
        payload = {"ok": ok, "data": {"result": result}}
        if not ok:
            payload["error"] = error or result
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    def fail(error):
        output(f"ERROR: {error}", ok=False, error=str(error))

    args = _input if isinstance(_input, dict) else {}
    try:
        ch_key = _channel_key(args.get("channel_id", ""))
        bridge_port = _bridge_port(args.get("port"))
    except ValueError as e:
        fail(str(e))

    channel_id = str(ch_key)
    root = _workspace_root()
    supervisor_url = _supervisor_url()
    admin_token = _admin_token(root)
    headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}

    self_session_id = (os.environ.get("CLONOTH_SESSION_ID") or "").strip()
    self_task_id = (os.environ.get("CLONOTH_TASK_ID") or "").strip()
    conversation_key = f"discord:{channel_id}"

    results = {}
    errors = []

    # 1. 清 Bot 端 _channel_history（注意 int key 转换；只用 int 拼源码）
    try:
        code = "_channel_history[%d] = []\nreturn {'cleared': True}" % ch_key
        resp = httpx.post(
            f"http://127.0.0.1:{bridge_port}/discord",
            json={"code": code},
            timeout=10.0,
        )
        if resp.status_code >= 400:
            results["bot_history"] = f"failed: HTTP {resp.status_code}"
            errors.append(f"bot_history: HTTP {resp.status_code}")
        else:
            bot_result = resp.json()
            if isinstance(bot_result, dict) and bot_result.get("ok"):
                results["bot_history"] = "cleared"
            else:
                detail = bot_result.get("error", "") if isinstance(bot_result, dict) else str(bot_result)[:120]
                results["bot_history"] = f"failed: {detail}"
                errors.append(f"bot_history: {detail}")
    except (httpx.HTTPError, ValueError) as e:
        results["bot_history"] = f"error: {e}"
        errors.append(f"bot_history: {e}")

    # 2. 调用 supervisor 的 conversation reset API
    #    exclude_task_id / exclude_session_id：别把正在执行本工具的任务（及其
    #    branch）一起取消/删除，否则清理动作自身的返回路径被掐断。
    try:
        resp = httpx.post(
            f"{supervisor_url}/v1/conversations/reset",
            json={
                "conversation_key": conversation_key,
                "exclude_session_id": self_session_id,
                "exclude_task_id": self_task_id,
            },
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("ok") is False:
                detail = str(data.get("error", "")) or "reset returned ok=false"
                results["session_reset"] = f"failed: {detail}"
                errors.append(f"session_reset: {detail}")
            else:
                body = data if isinstance(data, dict) else {}
                results["session_reset"] = "ok"
                results["old_session_id"] = body.get("old_session_id", "")
                results["cleared_children"] = body.get("cleared_children", 0)
                results["cancelled_tasks"] = body.get("cancelled_tasks", 0)
                results["context_files_cleaned"] = body.get("context_files_cleaned", 0)
        elif resp.status_code == 404:
            results["session_reset"] = "already clean (no active session)"
        elif resp.status_code in (401, 403):
            results["session_reset"] = f"failed: {resp.status_code} unauthorized (missing or stale admin token)"
            errors.append(
                f"session_reset: {resp.status_code} unauthorized — check CLONOTH_ADMIN_TOKEN or data/.admin_token"
            )
        else:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except (ValueError, AttributeError):
                detail = resp.text[:120]
            results["session_reset"] = f"failed: {resp.status_code} {detail}".strip()
            errors.append(f"session_reset: {resp.status_code} {detail}".strip())
    except (httpx.HTTPError, ValueError) as e:
        results["session_reset"] = f"error: {e}"
        errors.append(f"session_reset: {e}")

    results["conversation_key"] = conversation_key

    # 部分失败必须如实上报：任何一步 HTTP 失败都不能输出 "Context cleared"。
    if errors:
        summary = "Context reset incomplete — " + "; ".join(errors)
        results["errors"] = errors
        print(json.dumps({"ok": False, "error": summary,
                          "data": {"result": summary, **results}}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps({"ok": True, "data": {"result": "Context cleared", **results}}, ensure_ascii=False))
    sys.exit(0)
