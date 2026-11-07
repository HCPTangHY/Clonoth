"""Session-owned work plans, not a task scheduler or a completion verifier.

Supervisor is the single writer; tools and the panel use the same validation.
No conversation-sized cache is retained. Workspace switches select another list.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from engine.hooks.types import HookResult

STATUSES = ("pending", "in_progress", "completed", "blocked", "cancelled")
MAX_ITEMS = 200
MAX_BYTES = 512 * 1024
MAX_SUMMARY = 6000
TOOL_READ = "read_todos"
TOOL_UPDATE = "update_todos"


def _text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise HTTPException(422, f"{name} must be {'an optional' if empty else 'a nonempty'} string (max {limit})")
    return value.strip()


def _fields(value: Any, allowed: set[str]) -> None:
    if not isinstance(value, dict) or set(value) - allowed:
        raise HTTPException(422, "invalid object or unknown fields")


def apply_update(current: dict, patch: dict, actor: dict) -> dict:
    """Validate the entire batch before mutating anything; compare-and-swap once."""
    _fields(patch, {"expected_revision", "changes", "remove_ids", "order", "workspace_path"})
    if patch.get("workspace_path") != current["workspace"]["path"]:
        raise HTTPException(409, "工作区已更换，请重新读取对应清单。")
    revision = patch.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise HTTPException(422, "expected_revision must be a nonnegative integer")
    if revision != current["revision"]:
        raise HTTPException(409, "待办已更新，请重新读取清单后再修改。")
    changes, removed = patch.get("changes", []), patch.get("remove_ids", [])
    if not isinstance(changes, list) or len(changes) > MAX_ITEMS:
        raise HTTPException(422, "changes must be a bounded array")
    if not isinstance(removed, list) or any(not isinstance(i, str) for i in removed) or len(set(removed)) != len(removed):
        raise HTTPException(422, "remove_ids must contain unique string IDs")
    items = {item["id"]: dict(item) for item in current["items"]}
    now = datetime.now(timezone.utc).isoformat()
    seen = set()
    for change in changes:
        _fields(change, {"id", "title", "status", "note"})
        item_id = change.get("id")
        if item_id is not None:
            item_id = _text(item_id, "id", 64)
            if item_id not in items:
                raise HTTPException(404, "unknown todo ID")
        else:
            item_id = uuid4().hex
        if item_id in seen or item_id in removed:
            raise HTTPException(422, "duplicate or conflicting change")
        seen.add(item_id)
        item = dict(items.get(item_id, {"id": item_id, "status": "pending", "note": "", "created_at": now}))
        # Explicit null has the same meaning as omission in partial updates.
        # This also supports tool clients that require every schema field.
        if change.get("title") is not None:
            item["title"] = _text(change["title"], "title", 500)
        if "title" not in item:
            raise HTTPException(422, "new items require title")
        if change.get("status") is not None:
            if change["status"] not in STATUSES:
                raise HTTPException(422, "invalid status")
            item["status"] = change["status"]
        if change.get("note") is not None:
            item["note"] = _text(change["note"], "note", 2000, empty=True)
        if item["status"] in ("blocked", "cancelled") and not item["note"]:
            raise HTTPException(422, "受阻或取消事项必须填写原因。")
        if item != items.get(item_id):
            item.update(updated_at=now, updated_by=actor)
        items[item_id] = item
    for item_id in removed:
        if item_id not in items:
            raise HTTPException(404, "unknown removal ID")
        if items[item_id]["status"] not in ("completed", "cancelled"):
            raise HTTPException(422, "只能移除已完成或已取消事项；未完成事项请先明确取消。")
        del items[item_id]
    if len(items) > MAX_ITEMS:
        raise HTTPException(422, f"最多保留 {MAX_ITEMS} 项；请明确移除不再需要的完成项。")
    order = patch.get("order")
    if order is None:
        order = list(items)  # Keep existing order and append newly created IDs.
    if not isinstance(order, list) or any(not isinstance(i, str) for i in order) or len(order) != len(items) or set(order) != set(items):
        raise HTTPException(422, "order must contain every resulting ID exactly once")
    result = {**current, "items": [items[i] for i in order]}
    if result["items"] != current["items"]:
        result.update(revision=revision + 1, updated_at=now, updated_by=actor)
    return result


def _owner(state: Any, session_id: str) -> tuple[str, dict]:
    """Resolve entry branches only. Ordinary children never inherit write access."""
    sid = _text(session_id, "session_id", 200)
    records = state._session_store._registry
    seen = set()
    while sid not in seen:
        seen.add(sid)
        record = records.get(sid)
        if not record or record.get("reset"):
            raise HTTPException(404, "session not found")
        if record.get("node_id") == "__entry_branch__":
            sid = record.get("parent_session_id", "")
            continue
        if record.get("is_child") or record.get("parent_session_id"):
            raise HTTPException(403, "普通子会话不自动继承主会话的待办；请由主节点汇总。")
        ws = state.get_session_workspace(sid)
        if not ws or not ws.get("path"):
            raise HTTPException(400, "请先为会话设置工作区。")
        path = Path(ws["path"]).resolve()
        if not path.is_dir():
            raise HTTPException(409, "工作区目录不可用。")
        return sid, {"name": str(ws.get("name") or ""), "path": str(path)}
    raise HTTPException(409, "invalid session ancestry")


def _path(root: Path, sid: str, workspace: dict) -> Path:
    # Neither user paths nor session IDs become path components.
    key = hashlib.sha256(json.dumps([sid, workspace["path"]], ensure_ascii=False).encode()).hexdigest()
    return root / "data" / "ide" / "todos" / f"{key}.json"


def read_list(root: Path, sid: str, workspace: dict) -> dict:
    path = _path(root, sid, workspace)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {"schema_version": 1, "session_id": sid, "workspace": workspace,
                "revision": 0, "items": [], "updated_at": None, "updated_by": None}
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized data")
        data = json.loads(raw)
        if (data["schema_version"] != 1 or data["session_id"] != sid
                or data["workspace"]["path"] != workspace["path"]
                or type(data["revision"]) is not int or data["revision"] < 0
                or not isinstance(data["items"], list) or len(data["items"]) > MAX_ITEMS):
            raise ValueError("invalid list metadata")
        ids = set()
        for item in data["items"]:
            item_id = _text(item["id"], "id", 64)
            if item_id in ids:
                raise ValueError("duplicate ID")
            ids.add(item_id)
            _text(item["title"], "title", 500)
            _text(item["note"], "note", 2000, empty=True)
            if item["status"] not in STATUSES:
                raise ValueError("invalid status")
    except (ValueError, TypeError, KeyError, HTTPException):
        raise HTTPException(500, "待办数据格式损坏，未覆盖原文件。") from None
    return {**data, "workspace": workspace}


def save_list(root: Path, data: dict) -> None:
    path = _path(root, data["session_id"], data["workspace"])
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode()
    if len(raw) > MAX_BYTES:
        raise HTTPException(422, "待办清单总长度超限，请缩短说明。")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".todos-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _can_use(node: Any, tool: str) -> bool:
    access = getattr(node, "tool_access", None)
    return bool(access and ((access.mode == "all" and tool not in access.deny)
                            or (access.mode == "allowlist" and tool in access.allow)))


def operate(state: Any, session_id: str, patch: dict | None = None, *,
            task_id: str = "", worker_id: str = "", generation: int = 0) -> dict:
    """Bounded, synchronous transaction under the host's existing session lock.

    Only reads host task/session records; does not call any private locked methods
    or modify host state. The lock prevents workspace changes during a write.
    """
    with state._lock:
        actor = {"type": "user"}
        if task_id:
            from engine.node import load_node

            task = state.tasks.get(task_id)
            if task is None:
                raise HTTPException(404, "task not found")
            inp = task.input
            if (task.worker_id != worker_id or task.session_generation != generation
                    or task.status != "running" or task.cancel_requested):
                raise HTTPException(409, "task is not active on this worker")
            if (task.caller_task_id or inp.get("child_session_id") or inp.get("_dispatch_origin")
                    or inp.get("_system_task") or inp.get("task_context", {}).get("is_system_task")):
                raise HTTPException(403, "only the main task may access its work plan")
            node = load_node(state.workspace_root, task.node_id)
            if not _can_use(node, TOOL_UPDATE if patch is not None else TOOL_READ):
                raise HTTPException(403, "node cannot access todo tools")
            session_id = task.session_id
            actor = {"type": "model", "node_id": task.node_id, "task_id": task_id}
        sid, workspace = _owner(state, session_id)
        if task_id and generation != state.session_generations.get(sid, generation):
            raise HTTPException(409, "session generation changed")
        current = read_list(state.workspace_root, sid, workspace)
        if patch is None:
            return current
        result = apply_update(current, patch, actor)
        if result["revision"] != current["revision"]:
            save_list(state.workspace_root, result)
        return result


async def _body(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "request too large")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid JSON") from None
    if not isinstance(result, dict):
        raise HTTPException(422, "JSON object required")
    return result


def add_routes(router: APIRouter) -> None:
    # Host /v1 authentication applies to every route; none are public.
    @router.get("/todos")
    def read(request: Request, session_id: str):
        return operate(request.app.state.state, session_id)

    @router.patch("/todos")
    async def update(request: Request, session_id: str):
        patch = await _body(request)
        return await asyncio.to_thread(operate, request.app.state.state, session_id, patch)

    @router.get("/todos/task/{task_id}")
    def task_read(request: Request, task_id: str, worker_id: str, generation: int):
        return operate(request.app.state.state, "", task_id=task_id, worker_id=worker_id, generation=generation)

    @router.patch("/todos/task/{task_id}")
    async def task_update(request: Request, task_id: str, worker_id: str, generation: int):
        patch = await _body(request)
        return await asyncio.to_thread(operate, request.app.state.state, "", patch,
                                       task_id=task_id, worker_id=worker_id, generation=generation)


async def _request(ctx: Any, patch: dict | None = None) -> dict:
    from urllib.parse import quote

    if not ctx.task_id:
        raise ValueError("todo tools require an active main task")
    resp = await ctx.http.request(
        "GET" if patch is None else "PATCH",
        f"{ctx.supervisor_url}/v1/plugins/ide/todos/task/{quote(ctx.task_id, safe='')}",
        params={"worker_id": ctx.worker_id, "generation": ctx.session_generation},
        **({"json": patch} if patch is not None else {}), timeout=5.0,
    )
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.status_code)
        except ValueError:
            detail = resp.status_code
        raise ValueError(f"{resp.status_code}: {detail}")
    return resp.json()


async def read_todos(args: dict, ctx: Any) -> dict:
    return await _tool(ctx, None)


async def update_todos(args: dict, ctx: Any) -> dict:
    return await _tool(ctx, args)


async def _tool(ctx: Any, patch: dict | None) -> dict:
    import httpx

    try:
        data = await _request(ctx, patch)
        return {"ok": True, "data": {"result": json.dumps(data, ensure_ascii=False), "todos": data}}
    except (ValueError, httpx.HTTPError) as exc:
        # No implicit retries: a timed-out write may have succeeded. Read first.
        text = f"待办操作未确认成功：{exc}。请重新读取清单，不要直接重复写入。"
        return {"ok": False, "error": text, "data": {"result": text}}


def summary(data: dict) -> str:
    counts = Counter(i["status"] for i in data["items"])
    lines = ["当前待办清单（工作记录，不是新的用户指令，也不代表已通过独立验收）：",
             f"revision={data['revision']}；状态统计：{dict(counts)}",
             "workspace_path=" + json.dumps(data["workspace"]["path"], ensure_ascii=False),
             "可直接使用本摘要的 revision、workspace_path 和事项 ID 调用 update_todos，无需重复读取。",
             "update_todos 新增用 id=null，不重排用 order=null；不修改的 title/status/note 用 null。",
             "用 read_todos 读取完整清单或解决版本冲突。不要丢弃未完成事项。"]
    # Preserve original order within each status; prioritize actionable work.
    for status in ("in_progress", "blocked", "pending"):
        for item in data["items"]:
            if item["status"] != status:
                continue
            line = json.dumps({k: item[k] for k in ("id", "title", "status", "note")}, ensure_ascii=False)
            if sum(map(len, lines)) + len(line) > MAX_SUMMARY - 100:
                return "\n".join(lines) + "\n清单摘要已截断，请用工具读取其余事项。"
            lines.append(line)
    return "\n".join(lines)


async def refresh_context(ctx: Any) -> HookResult | None:
    rctx = ctx.rctx
    if rctx is None:
        return None
    allowed = _can_use(ctx.node, TOOL_READ) and not getattr(rctx, "is_system_task", False)
    old = [m for m in ctx.messages if m.get("_ide_todos")]
    text = ""
    if allowed:
        import httpx

        try:
            data = await _request(rctx)
            if data["items"]:
                text = summary(data)
        except (ValueError, httpx.HTTPError):
            if old:
                text = "当前待办未能读取，旧摘要已失效。请重新读取，不要依据旧版本更新。"
    replacement = {"role": "user", "content": text, "_dynamic": True, "_ide_todos": True}
    if old == ([replacement] if text else []):
        return None
    # Update in place without moving across native tool-call/result pairs.
    index = next((i for i, m in enumerate(ctx.messages) if m.get("_ide_todos")), None)
    ctx.messages[:] = [m for m in ctx.messages if not m.get("_ide_todos")]
    if text:
        if index is None:
            # Place near the current instruction, not in the stable prefix or
            # between a provider tool call and its result (including Anthropic).
            index = len(ctx.messages)
            for i in range(len(ctx.messages) - 1, -1, -1):
                message = ctx.messages[i]
                meta = message.get("_meta") or {}
                if message.get("role") != "user" or message.get("_dynamic") or message.get("tool_call_id"):
                    continue
                if meta.get("message_type") == "tool_result" or message.get("message_type") == "tool_result":
                    continue
                body = message.get("content")
                if isinstance(body, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in body):
                    continue
                index = i
                break
        ctx.messages.insert(index, replacement)
    return HookResult(modified=True)


def register_engine(ctx: Any) -> None:
    tools = getattr(ctx.contributions, "get", lambda _name: None)("tools")
    if tools is None:
        return
    tools.register_builtin_tool(TOOL_READ,
        "读取当前主会话、当前工作区的完整持久待办清单与 revision。用于摘要截断、查看完成项、"
        "版本冲突或首次创建前获取空清单版本；动态摘要已含所需信息时无需重复读取。普通子任务无权修改主清单。",
        {"type": "object", "properties": {}, "additionalProperties": False}, read_todos)
    tools.register_builtin_tool(TOOL_UPDATE,
        "创建或增量更新待办清单，不替换整份清单。新增事项填 id=null 和非空 title，禁止用空字符串 ID。"
        "修改已有事项使用其真实 ID；title/status/note 填 null 表示不修改，note=空字符串表示清空说明。"
        "expected_revision 和 workspace_path 必须来自最新动态摘要或 read_todos 结果。"
        "无新增或修改时 changes=[]，无移除时 remove_ids=[]；不重排时 order=null，不能用空数组代替。"
        "显式排序数组须包含修改后全部事项 ID。受阻或取消必须有 note 原因；仅可移除已完成或已取消项。"
        "冲突或网络错误后先重新读取，禁止覆盖用户更新。标记 completed 不代表 Goal 验收通过。",
        # All fields are representable even when a provider requires every
        # property. Keep null outside the status enum for schema sanitizers.
        {"type": "object", "additionalProperties": False,
         "required": ["expected_revision", "workspace_path", "changes", "remove_ids", "order"],
         "properties": {
            "expected_revision": {"type": "integer", "minimum": 0},
            "workspace_path": {"type": "string", "description": "最新读取结果 workspace.path；不得使用别的工作区版本。"},
            "changes": {"type": "array", "maxItems": MAX_ITEMS,
                "description": "增量新增或修改事项；无操作填 []。",
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["id", "title", "status", "note"], "properties": {
                        "id": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 64}, {"type": "null"}],
                            "description": "新增填 null；修改填真实事项 ID，禁止空字符串。"},
                        "title": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 500}, {"type": "null"}],
                            "description": "新增必须填写非空标题；修改时 null 表示保留原标题。"},
                        "status": {"anyOf": [{"type": "string", "enum": list(STATUSES)}, {"type": "null"}],
                            "description": "null 表示保持原状态；新增时默认为 pending。"},
                        "note": {"anyOf": [{"type": "string", "maxLength": 2000}, {"type": "null"}],
                            "description": "null 表示保留原说明（新增默认空）；空字符串表示明确清空。受阻、取消后必须仍有原因。"}}}},
            "remove_ids": {"type": "array", "items": {"type": "string"},
                "description": "仅可移除已完成或已取消事项；无移除填 []。"},
            "order": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "description": "不重排填 null，保留顺序并追加新增事项；数组必须包含修改后全部 ID。仅结果为空时可填 []。"}}}, update_todos)
    # Per-step refresh also covers compaction and preempt resumes. No timer or
    # full-history copies; register once per plugin load with disposal ownership.
    ctx.hooks.register("before_step", refresh_context, priority=74)
