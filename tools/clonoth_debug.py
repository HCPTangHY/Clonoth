from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
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


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def _readable_result(result):
        # [AutoC 2026-05-31] Why: clonoth_debug has many action-specific payloads,
        # but the engine now reads data.result uniformly. How: derive a concise
        # action-aware summary while storing the original payload under data.payload.
        # Purpose: keep debug details structured without losing readable history.
        if isinstance(result, dict):
            if action == "api":
                return f"API result for {args.get('path', '')}"
            if action == "task_events":
                return f"{result.get('count', 0)} task events"
            if action == "active_tasks":
                return f"{result.get('active_count', 0)} active tasks"
            if action == "pending_approvals":
                return f"{result.get('pending_count', 0)} pending approvals"
            if action == "recent_events":
                return f"{result.get('total', 0)} recent events"
            if action == "node_status":
                return f"Node status: {result.get('node_id', '')}"
            if action == "health":
                return "Health checked"
        return json.dumps(result, ensure_ascii=False, default=str)
    def output(result):
        print(json.dumps({"ok": True, "data": {"result": _readable_result(result), "payload": result}}, ensure_ascii=False)); sys.exit(0)
    def fail(error): print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import os, subprocess
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    from urllib.error import HTTPError, URLError
    action = args.get("action", "")
    task_id = args.get("task_id", "")
    node_id = args.get("node_id", "")
    limit = args.get("limit", 20)
    EVENTS_FILE = "data/events.jsonl"
    CLONOTH_URL = "http://127.0.0.1:8765"
    def api_call(path, method="GET", body=None, params=None):
        url = f"{CLONOTH_URL}{path}"
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            try:
                err_body = json.loads(e.read().decode())
            except Exception:
                err_body = e.reason
            return {"error": f"HTTP {e.code}", "detail": err_body}
        except URLError as e:
            return {"error": f"Connection failed: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
    def read_events_tail(n=500):
        try:
            result = subprocess.run(["tail", f"-{n}", EVENTS_FILE],
                                    capture_output=True, text=True, timeout=10)
            lines = result.stdout.strip().split("\n")
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []
    # ── api: 通用 API 调用 ──
    if action == "api":
        path = args.get("path", "")
        if not path:
            fail("api action 需要 path 参数")
        method = args.get("method", "GET")
        body = args.get("body")
        params = args.get("params")
        output(api_call(path, method=method, body=body, params=params))
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
                    "type": ev["type"],
                    "ts": ev.get("ts", "")[:19],
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
            etype = ev["type"]
            ts = ev.get("ts", "")[:19]
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
        api_state = api_call("/v1/admin/state")
        api_counts = api_state.get("tasks", {}) if isinstance(api_state, dict) else {}
        output({"active_count": len(active), "tasks": active, "api_task_counts": api_counts})
    # ── pending_approvals: 从 API 查 ──
    elif action == "pending_approvals":
        data = api_call("/v1/admin/state")
        if isinstance(data, dict) and "pending_approvals" in data:
            pa = data["pending_approvals"]
            output({"pending_count": len(pa), "approvals": pa})
        else:
            # fallback to events
            events = read_events_tail(200)
            requested = {}
            decided = set()
            for ev in events:
                p = ev.get("payload", {})
                if ev["type"] == "approval_requested":
                    aid = p.get("approval_id", "")
                    det = p.get("details", {})
                    desc = det.get("tool_name", "") or det.get("command", "")[:80] or det.get("path", "") or str(det)[:80]
                    requested[aid] = {"ts": ev.get("ts", "")[:19], "desc": desc}
                elif ev["type"] == "approval_decided":
                    decided.add(p.get("approval_id", ""))
            pending = {k: v for k, v in requested.items() if k not in decided}
            result = [{"approval_id": k[:16], **v} for k, v in pending.items()]
            output({"pending_count": len(result), "approvals": result})
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
                "type": ev["type"],
                "ts": ev.get("ts", "")[:19],
                "node_id": nid,
                "task_id": p.get("task_id", "")[:12],
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
                    "type": ev["type"],
                    "ts": ev.get("ts", "")[:19],
                    "task_id": tid[:12],
                    "message": (p.get("message", "") or "")[:100],
                    "status": p.get("status", ""),
                })
                if tid:
                    etype = ev["type"]
                    if etype in ("task_created", "task_started"):
                        node_tasks[tid] = {"status": p.get("status", "running"), "ts": ev.get("ts", "")[:19]}
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
        output(api_call("/v1/health"))
    else:
        fail(f"未知 action: {action}")
