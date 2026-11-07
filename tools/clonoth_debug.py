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
  CLONOTH_WORKSPACE       install root; local event log lives at <root>/data/events.jsonl
"""

SPEC = {
    'name': 'clonoth_debug',
    'description': (
        'Clonoth 内部调试工具。查询任务状态、事件流、活跃任务、pending 审批等。避免每次手写 execute_command + grep。\n'
        '\n'
        'action 可选值：\n'
        '- api: 调用任意 supervisor API 端点（需要 path，可选 method/body/params）\n'
        '- task_events: 查某个 task 的事件流（需要 task_id，支持前缀匹配）\n'
        '- active_tasks: 列出所有活跃任务（running/pending/suspended）\n'
        '- pending_approvals: 列出未决审批\n'
        '- recent_events: 最近 N 条事件（可选 node_id 过滤）\n'
        '- node_status: 查某个节点最近活动（需要 node_id）\n'
        '- health: 查 Clonoth supervisor 健康状态'
    ),
    'input_schema': {
        'properties': {
            'action': {
                'description': '要执行的查询操作',
                'enum': ['api', 'task_events', 'active_tasks', 'pending_approvals',
                         'recent_events', 'node_status', 'health'],
                'type': 'string',
            },
            'path': {
                'description': "api action 用：supervisor API 路径，如 '/v1/admin/state'、'/v1/sessions/{sid}/running_tasks'",
                'type': 'string',
            },
            'method': {
                'description': 'api action 用：HTTP 方法，默认 GET',
                'enum': ['GET', 'POST', 'PUT', 'DELETE'],
                'type': 'string',
            },
            'body': {
                'description': 'api action 用：POST/PUT 请求体（JSON 对象）',
                'type': 'object',
            },
            'params': {
                'description': 'api action 用：URL query 参数（JSON 对象）',
                'type': 'object',
            },
            'task_id': {
                'description': "task_events 用：task ID（支持前缀，如 '53cf8e69'）",
                'type': 'string',
            },
            'node_id': {
                'description': "node_status / recent_events 过滤用：节点 ID（如 'child_coder'）",
                'type': 'string',
            },
            'limit': {
                'default': 20,
                'description': '返回条数限制，默认 20',
                'type': 'integer',
            },
        },
        'required': ['action'],
        'type': 'object',
    },
}

TIMEOUT_SEC = 15.0
DEFAULT_SUPERVISOR_URL = "http://127.0.0.1:8765"
ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE")
MAX_LIMIT = 1000
TAIL_BYTES = 262144  # bounded seek fallback when `tail` is unavailable


def _workspace_root():
    """Install root: CLONOTH_WORKSPACE, else the repo containing this tool.

    Why: the event log path used to be the relative literal "data/events.jsonl",
    which only worked when the subprocess cwd happened to be the install root.
    How: derive it from the injected workspace root with a repo-relative
    fallback. Purpose: the tool stays portable across deployments (no
    production directory is hardcoded).
    """
    import os
    from pathlib import Path

    raw = (os.environ.get("CLONOTH_WORKSPACE") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent


def _events_file():
    return _workspace_root() / "data" / "events.jsonl"


def _supervisor_url():
    import os

    return (os.environ.get("CLONOTH_SUPERVISOR_URL") or "").strip().rstrip("/") or DEFAULT_SUPERVISOR_URL


def _admin_token(root):
    """Bearer token following the existing production convention only.

    [2026-10-31] Why: engine/runner.py resolves CLONOTH_ADMIN_TOKEN ->
    <install>/data/.admin_token. How: same order here, value never printed.
    Purpose: one credential path for engine, bot and tools.
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


def _validated_path(path):
    """Accept only a rooted, whitespace-free supervisor path.

    Why: `path` is caller-controlled and concatenated onto the supervisor base
    url. How: require a single leading '/', no scheme/authority override, no
    control chars or whitespace. Purpose: keep the request on the configured
    supervisor origin.
    """
    raw = str(path if path is not None else "").strip()
    if not raw:
        raise ValueError("api action 需要 path 参数")
    if not raw.startswith("/") or raw.startswith("//"):
        raise ValueError("path must start with a single '/'")
    if "://" in raw or "\\" in raw:
        raise ValueError("path must be a supervisor path, not a URL")
    if any(ch.isspace() for ch in raw) or any(ord(ch) < 0x20 for ch in raw):
        raise ValueError("path must not contain whitespace or control characters")
    return raw


def _validated_method(method):
    raw = str(method if method is not None else "GET").strip().upper() or "GET"
    if raw not in ALLOWED_METHODS:
        raise ValueError(f"method must be one of {', '.join(ALLOWED_METHODS)}")
    return raw


def _validated_limit(limit):
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 20
    return max(1, min(MAX_LIMIT, value))


def _parse_event_lines(text):
    import json

    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _tail_by_seek(path, n):
    """Bounded-memory fallback: read only the last TAIL_BYTES of the log."""
    import os

    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # first line is very likely cut in half
        text = text.split("\n", 1)[1] if "\n" in text else ""
    lines = text.splitlines()[-int(n):]
    return _parse_event_lines("\n".join(lines))


def read_events_tail(n=500):
    """Tail the local event log without ever loading it whole.

    Why: events.jsonl reaches GB size; reading all lines and slicing the tail
    would allocate the entire log per query. How: keep the production
    `tail -n N <file>` argv call (10s timeout, no shell interpolation) and fall
    back to a bounded seek when the binary is unavailable. Purpose: constant
    memory per debug query on production-sized logs.
    """
    import subprocess

    path = str(_events_file())
    try:
        proc = subprocess.run(
            ["tail", f"-{int(n)}", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return _parse_event_lines(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return _tail_by_seek(path, n)


if __name__ == "__main__":
    import json, os, sys
    import httpx

    try:
        _input = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON input: {e}",
                          "data": {"result": f"ERROR: invalid JSON input: {e}"}}, ensure_ascii=False))
        sys.exit(1)

    def _readable_result(result):
        # [2026-10-31] Why: engine's result_to_raw only forwards data.result, so a
        # concise "N events" summary would hide every debug detail from the model.
        # How: migrate the production representation — the full JSON payload,
        # indented and stringified for non-JSON values. Purpose: data.result is
        # losslessly json.loads()-able back into data.payload.
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    def output(result, ok=True, error=""):
        text = _readable_result(result) if isinstance(result, (dict, list)) else str(result)
        payload = {"ok": ok, "data": {"result": text, "payload": result}}
        if not ok:
            payload["error"] = error or text
        print(json.dumps(payload, ensure_ascii=False)); sys.exit(0 if ok else 1)

    def fail(error): output(f"ERROR: {error}", ok=False, error=str(error))

    args = _input if isinstance(_input, dict) else {}
    action = str(args.get("action", "") or "").strip()
    task_id = str(args.get("task_id", "") or "").strip()
    node_id = str(args.get("node_id", "") or "").strip()
    limit = _validated_limit(args.get("limit", 20))

    root = _workspace_root()
    supervisor_url = _supervisor_url()
    admin_token = _admin_token(root)
    headers = {"User-Agent": "Clonoth"}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"

    def api_call(path, method="GET", body=None, params=None):
        """Call the supervisor; return (transport_error, payload).

        Why: supervisor payloads legitimately contain an "error" key (e.g.
        /v1/admin/state snapshots), so a failed request cannot be detected by
        sniffing the body. How: connection failures and HTTP >= 400 are reported
        out-of-band as transport_error, every other response is a real payload.
        Purpose: a dead supervisor is never rendered as a successful debug result.
        """
        url = f"{supervisor_url}{path}"
        try:
            resp = httpx.request(
                method,
                url,
                params=params or None,
                json=body if body is not None else None,
                headers=headers,
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            return f"Connection failed: {e}", None
        try:
            payload = resp.json()
        except ValueError:
            payload = {"text": resp.text[:2000]}
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code}", payload
        return None, payload

    # ── api: 通用 API 调用（method/body/params 原样透传） ──
    if action == "api":
        try:
            path = _validated_path(args.get("path", ""))
            method = _validated_method(args.get("method", "GET"))
        except ValueError as e:
            fail(str(e))
        body = args.get("body")
        params = args.get("params")
        err, payload = api_call(path, method=method, body=body, params=params)
        if err:
            detail = payload if isinstance(payload, dict) else {"detail": payload}
            output({**detail, "error": err}, ok=False, error=err)
        output(payload)
    # ── task_events: 查事件流（events.jsonl） ──
    elif action == "task_events":
        if not task_id:
            fail("需要 task_id 参数")
        events = read_events_tail(1000)
        matched = []
        for ev in events:
            p = ev.get("payload", {})
            tid = p.get("task_id", "")
            if task_id in tid:
                matched.append({
                    "type": ev.get("type", ""),
                    "ts": str(ev.get("ts", ""))[:19],
                    "node_id": p.get("node_id", ""),
                    "status": p.get("status", ""),
                    "message": (p.get("message", "") or "")[:100],
                })
        output({"task_id_prefix": task_id, "count": len(matched), "events": matched[-limit:]})
    # ── active_tasks: 从事件推算活跃任务 + API 交叉校验 ──
    elif action == "active_tasks":
        events = read_events_tail(2000)
        tasks = {}  # tid -> info
        for ev in events:
            p = ev.get("payload", {})
            tid = p.get("task_id", "")
            if not tid:
                continue
            etype = ev.get("type", "")
            ts = str(ev.get("ts", ""))[:19]
            nid = p.get("node_id", "")
            if etype == "task_created":
                tasks[tid] = {"node_id": nid, "status": "pending", "created": ts, "last_ts": ts}
            elif etype == "task_started":
                if tid in tasks:
                    tasks[tid].update(status="running", last_ts=ts)
                else:
                    tasks[tid] = {"node_id": nid, "status": "running", "created": ts, "last_ts": ts}
            elif etype == "task_completed":
                if tid in tasks:
                    tasks[tid].update(status=p.get("status", "completed"), last_ts=ts)
            elif etype == "task_cancelled":
                if tid in tasks:
                    tasks[tid].update(status="cancelled", last_ts=ts)
            elif etype == "task_suspended":
                if tid in tasks:
                    tasks[tid].update(status="suspended", last_ts=ts)
            elif etype == "task_resumed":
                if tid in tasks:
                    tasks[tid].update(status="pending", last_ts=ts)
        active = []
        for tid, info in tasks.items():
            if info["status"] in ("running", "pending", "suspended"):
                active.append({
                    "task_id": tid[:12],
                    "node_id": info.get("node_id", "?"),
                    "status": info["status"],
                    "created": info.get("created", ""),
                    "last_ts": info.get("last_ts", ""),
                })
        api_err, api_state = api_call("/v1/admin/state")
        api_counts = api_state.get("tasks", {}) if isinstance(api_state, dict) else {}
        result = {"active_count": len(active), "tasks": active, "api_task_counts": api_counts}
        if api_err:
            # 本地事件推算结果仍然有用，但 supervisor 交叉校验失败必须如实标注。
            result["api_error"] = api_err
            output(result, ok=False, error=api_err)
        output(result)
    # ── pending_approvals: 从 API 查 ──
    elif action == "pending_approvals":
        api_err, data = api_call("/v1/admin/state")
        if api_err is None and isinstance(data, dict) and "pending_approvals" in data:
            pa = data["pending_approvals"]
            output({"pending_count": len(pa), "approvals": pa})
        else:
            # fallback to events（降级结果，若 API 传输失败则如实标记 ok=false）
            events = read_events_tail(200)
            requested = {}
            decided = set()
            for ev in events:
                p = ev.get("payload", {})
                if ev.get("type") == "approval_requested":
                    aid = p.get("approval_id", "")
                    det = p.get("details", {}) or {}
                    desc = det.get("tool_name", "") or (det.get("command", "") or "")[:80] or det.get("path", "") or str(det)[:80]
                    requested[aid] = {"ts": str(ev.get("ts", ""))[:19], "desc": desc}
                elif ev.get("type") == "approval_decided":
                    decided.add(p.get("approval_id", ""))
            pending = {k: v for k, v in requested.items() if k not in decided}
            result = [{"approval_id": k[:16], **v} for k, v in pending.items()]
            payload = {"pending_count": len(result), "approvals": result}
            if api_err:
                payload["api_error"] = api_err
                output(payload, ok=False, error=api_err)
            output(payload)
    # ── recent_events: 最近事件 ──
    elif action == "recent_events":
        events = read_events_tail(200)
        filtered = []
        for ev in events:
            p = ev.get("payload", {})
            nid = p.get("node_id", "")
            if node_id and node_id not in nid:
                continue
            filtered.append({
                "type": ev.get("type", ""),
                "ts": str(ev.get("ts", ""))[:19],
                "node_id": nid,
                "task_id": str(p.get("task_id", ""))[:12],
                "message": (p.get("message", "") or "")[:80],
                "status": p.get("status", ""),
            })
        output({"total": len(filtered), "events": filtered[-limit:]})
    # ── node_status: 节点状态 ──
    elif action == "node_status":
        if not node_id:
            fail("需要 node_id 参数")
        events = read_events_tail(500)
        node_events = []
        node_tasks = {}
        for ev in events:
            p = ev.get("payload", {})
            nid = p.get("node_id", "")
            tid = p.get("task_id", "")
            if node_id in nid:
                node_events.append({
                    "type": ev.get("type", ""),
                    "ts": str(ev.get("ts", ""))[:19],
                    "task_id": str(tid)[:12],
                    "message": (p.get("message", "") or "")[:100],
                    "status": p.get("status", ""),
                })
                if tid:
                    etype = ev.get("type", "")
                    if etype in ("task_created", "task_started"):
                        node_tasks[tid] = {"status": p.get("status", "running"), "ts": str(ev.get("ts", ""))[:19]}
                    elif etype in ("task_completed", "task_cancelled"):
                        node_tasks.pop(tid, None)
                    elif etype == "task_suspended":
                        if tid in node_tasks:
                            node_tasks[tid]["status"] = "suspended"
                    elif etype == "task_resumed":
                        if tid in node_tasks:
                            node_tasks[tid]["status"] = "pending"
        active = [{"task_id": tid[:12], "status": info["status"], "last_ts": info["ts"]}
                  for tid, info in node_tasks.items()
                  if info["status"] in ("running", "pending", "suspended")]
        last_event = node_events[-1] if node_events else None
        output({
            "node_id": node_id,
            "last_event": last_event,
            "recent_events": node_events[-limit:],
            "active_tasks": active,
        })
    # ── health ──
    elif action == "health":
        err, payload = api_call("/v1/health")
        if err:
            detail = payload if isinstance(payload, dict) else {"detail": payload}
            output({**detail, "error": err}, ok=False, error=err)
        output(payload)
    else:
        fail(f"未知 action: {action}")
