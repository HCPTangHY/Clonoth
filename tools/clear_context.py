from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '清空指定 Discord 频道的对话上下文（Bot 端历史 + Engine session 上下文）。⚠️ 仅在用户明确要求清理/重置上下文时才可调用，禁止 AI 自行决定调用。副作用：会断开当前 session 的所有子任务回调链。channel_id 为 Discord 频道 ID。',
 'input_schema': {'properties': {'channel_id': {'description': 'Discord 频道 ID', 'type': 'string'}},
                  'required': ['channel_id'],
                  'type': 'object'},
 'name': 'clear_context'}

TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import httpx
    from pathlib import Path

    channel_id = str(args.get("channel_id", "")).strip()
    if not channel_id:
        fail("channel_id is required")

    # ── Read admin token for supervisor API auth ──
    admin_token = ""
    try:
        token_file = Path(__file__).resolve().parent.parent / "data" / ".admin_token"
        if token_file.exists():
            admin_token = token_file.read_text().strip()
    except Exception:
        pass

    results = {}

    # 1. 清 Bot 端 _channel_history
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{args.get('port', 8768)}/discord",
            json={"code": f"_channel_history[{channel_id}] = []\nreturn {{'cleared': True}}"},
            timeout=10.0,
        )
        bot_result = resp.json()
        results["bot_history"] = "cleared" if bot_result.get("ok") else f"failed: {bot_result}"
    except Exception as e:
        results["bot_history"] = f"error: {e}"

    # 2. 调用 supervisor 的 conversation reset API
    #    这会：移除 conversation_map 映射（下次消息创建新 session）+ 清理 node_contexts
    #    + 级联清理 fork/dispatch 子 session
    conversation_key = f"discord:{channel_id}"
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    try:
        resp = httpx.post(
            "http://127.0.0.1:8765/v1/conversations/reset",
            json={"conversation_key": conversation_key},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            results["session_reset"] = "ok"
            results["old_session_id"] = data.get("old_session_id", "")
            results["context_files_cleaned"] = data.get("context_files_cleaned", 0)
            results["cleared_children"] = data.get("cleared_children", 0)
        elif resp.status_code == 404:
            # 404 = conversation not found (already clean)
            results["session_reset"] = "already clean (no active session)"
        elif resp.status_code == 401:
            fail("supervisor API auth failed (401). admin_token may be missing or invalid.")
        else:
            fail(f"supervisor reset failed: HTTP {resp.status_code}")
    except SystemExit:
        raise
    except Exception as e:
        fail(f"supervisor reset error: {e}")

    results["conversation_key"] = conversation_key
    output({"ok": True, "data": {"result": "Context cleared", **results}})
