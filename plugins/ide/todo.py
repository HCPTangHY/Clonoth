"""Session-owned work plans, not a task scheduler or a completion verifier.

Supervisor is the single writer; tools and the panel use the same validation.
Each real session owns its list; workspace bindings never affect ownership.
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

from fastapi import APIRouter, HTTPException, Request

from engine.hooks.types import HookResult

STATUSES = ("pending", "in_progress", "completed", "blocked", "cancelled")
MODES = ("replace", "append")
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
    _fields(patch, {"expected_revision", "expected_session_id", "mode", "items"})
    expected_sid = _text(patch.get("expected_session_id"), "expected_session_id", 200)
    if expected_sid != current["session_id"]:
        raise HTTPException(409, "待办所属会话不同，请重新读取当前会话清单。")
    revision = patch.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise HTTPException(422, "expected_revision must be a nonnegative integer")
    if revision != current["revision"]:
        raise HTTPException(409, "待办已更新，请重新读取清单后再修改。")
    mode = patch.get("mode")
    if mode is None:
        mode = "replace"
    elif mode not in MODES:
        raise HTTPException(422, "mode must be 'replace' or 'append'")
    raw_items = patch.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise HTTPException(422, "items must be a bounded array")
    existing = {item["id"]: dict(item) for item in current["items"]}
    seen: set[str] = set()
    for entry in raw_items:
        _fields(entry, {"id", "title", "status", "note"})
        raw_id = entry.get("id")
        if raw_id is not None:
            eid = _text(raw_id, "id", 64)
            if eid not in existing:
                raise HTTPException(404, "unknown todo ID")
            if eid in seen:
                raise HTTPException(422, "duplicate todo ID")
            seen.add(eid)
    taken = set(existing) if mode == "append" else set(seen)
    seq = max((int(i) for i in taken if i.isascii() and i.isdigit()), default=0)
    now = datetime.now(timezone.utc).isoformat()
    built: list[dict] = []
    appended: list[dict] = []
    merged = {k: dict(v) for k, v in existing.items()} if mode == "append" else {}
    for entry in raw_items:
        raw_id = entry.get("id")
        is_new = raw_id is None
        if is_new:
            seq += 1
            while str(seq) in taken:
                seq += 1
            item_id = str(seq)
            taken.add(item_id)
            item: dict[str, Any] = {"id": item_id, "status": "pending", "note": "", "created_at": now}
        else:
            item_id = raw_id.strip()
            item = dict(existing[item_id])
        if entry.get("title") is not None:
            item["title"] = _text(entry["title"], "title", 500)
        if "title" not in item:
            raise HTTPException(422, "new items require title")
        if entry.get("status") is not None:
            if entry["status"] not in STATUSES:
                raise HTTPException(422, "invalid status")
            item["status"] = entry["status"]
        if entry.get("note") is not None:
            item["note"] = _text(entry["note"], "note", 2000, empty=True)
        if item["status"] in ("blocked", "cancelled") and not item["note"]:
            raise HTTPException(422, "受阻或取消事项必须填写原因。")
        if is_new or item != existing[item_id]:
            item.update(updated_at=now, updated_by=actor)
        if mode == "replace":
            built.append(item)
        elif is_new:
            appended.append(item)
        else:
            merged[item_id] = item
    result_items = built if mode == "replace" else [merged[i["id"]] for i in current["items"]] + appended
    if len(result_items) > MAX_ITEMS:
        raise HTTPException(422, f"最多保留 {MAX_ITEMS} 项；请改用 mode='replace' 重写清单。")
    result = {**current, "items": result_items}
    if result["items"] != current["items"]:
        result.update(revision=revision + 1, updated_at=now, updated_by=actor)
    return result


def _owner(state: Any, session_id: str) -> str:
    """Only temporary entry branches share a list; ordinary children own theirs."""
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
        return sid
    raise HTTPException(409, "invalid session ancestry")


def _path(root: Path, sid: str) -> Path:
    # Separate v2 files from the fixed legacy directory; never scan growing
    # session lists when looking for an old workspace-scoped list.
    key = hashlib.sha256(sid.encode()).hexdigest()
    return root / "data" / "ide" / "todos" / "sessions" / f"{key}.json"


def _read_json(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized data")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
            raise ValueError("invalid session metadata")
        return data
    except (ValueError, TypeError):
        raise HTTPException(500, "待办数据格式损坏，未覆盖原文件。") from None


def _validate_list(data: dict, sid: str, version: int) -> dict:
    try:
        if (type(data["schema_version"]) is not int or data["schema_version"] != version
                or data["session_id"] != sid
                or type(data["revision"]) is not int or data["revision"] < 0
                or not isinstance(data["items"], list) or len(data["items"]) > MAX_ITEMS):
            raise ValueError("invalid list metadata")
        if version == 1:
            _text(data["workspace"]["path"], "workspace.path", MAX_BYTES)
        elif "workspace" in data:
            raise ValueError("workspace is not part of a session list")
        ids = set()
        for item in data["items"]:
            item_id = _text(item["id"], "id", 64)
            if item_id in ids:
                raise ValueError("duplicate ID")
            ids.add(item_id)
            _text(item["title"], "title", 500)
            note = _text(item["note"], "note", 2000, empty=True)
            if item["status"] not in STATUSES or (item["status"] in ("blocked", "cancelled") and not note):
                raise ValueError("invalid status or missing reason")
    except (ValueError, TypeError, KeyError, HTTPException):
        raise HTTPException(500, "待办数据格式损坏，未覆盖原文件。") from None
    return data


def read_list(root: Path, sid: str) -> dict:
    """Read under the supervisor lock; lazily copy one unambiguous v1 list.

    Legacy files remain unchanged as backups. Ambiguity or corruption fails
    closed instead of guessing, merging, or returning an empty replacement.
    """
    path = _path(root, sid)
    try:
        return _validate_list(_read_json(path), sid, 2)
    except FileNotFoundError:
        pass
    legacy = []
    for old_path in sorted(path.parent.parent.glob("*.json")):
        data = _read_json(old_path)
        if data["session_id"] == sid:
            legacy.append(_validate_list(data, sid, 1))
    if len(legacy) > 1:
        raise HTTPException(409, "同一会话存在多份旧待办，请明确整理后再迁移；原文件未修改。")
    if not legacy:
        return {"schema_version": 2, "session_id": sid, "revision": 0,
                "items": [], "updated_at": None, "updated_by": None}
    migrated = {**legacy[0], "schema_version": 2}
    migrated.pop("workspace")
    save_list(root, migrated)
    return migrated


def save_list(root: Path, data: dict) -> None:
    path = _path(root, data["session_id"])
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


UPDATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["expected_session_id", "expected_revision", "items"],
    "properties": {
        "expected_revision": {"type": "integer", "minimum": 0},
        "expected_session_id": {"type": "string", "description": "当前清单返回的 session_id；仅核对身份，不是跨会话访问入口。"},
        "mode": {"type": "string", "enum": list(MODES),
            "description": "可选，默认 'replace'（整表覆盖，items=[] 即清空）；'append' 为按 id 修改已有事项或在末尾追加新事项。"},
        "items": {"type": "array", "maxItems": MAX_ITEMS,
            "description": "mode='replace' 时为替换后的完整事项列表（传 [] 清空）；mode='append' 时为要追加或按 id 更新的事项。",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "id": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 64}, {"type": "null"}],
                        "description": "可选。新增时不填或填 null；修改/保留已有事项时填真实事项 ID，禁止空字符串。"},
                    "title": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 500}, {"type": "null"}],
                        "description": "新增必须填写非空标题；按 id 修改已有事项时不填或填 null 表示保留原标题。"},
                    "status": {"anyOf": [{"type": "string", "enum": list(STATUSES)}, {"type": "null"}],
                        "description": "可选。新增时默认为 'pending'；按 id 修改时不填或填 null 表示保持原状态。"},
                    "note": {"anyOf": [{"type": "string", "maxLength": 2000}, {"type": "null"}],
                        "description": "可选。新增默认空；按 id 修改时不填或填 null 保留原说明，填 '' 清空。受阻、取消后必须有原因。"}}}}}}


def _can_use(node: Any, tool: str) -> bool:
    from engine.inference.pseudo_tools import is_tool_action_allowed

    access = getattr(node, "tool_access", None)
    return bool(access and (access.mode == "all" or (access.mode == "allowlist" and tool in access.allow))
                and is_tool_action_allowed(node, tool, {}))


def operate(state: Any, session_id: str, patch: dict | None = None, *,
            task_id: str = "", worker_id: str = "", generation: int = 0) -> dict:
    """Bounded, synchronous transaction under the host's existing session lock.

    Only reads host task/session records; does not call any private locked methods
    or modify host state. The lock serializes ownership checks, migration and CAS.
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
            if inp.get("_system_task") or inp.get("task_context", {}).get("is_system_task"):
                raise HTTPException(403, "system tasks cannot access work plans")
            node = load_node(state.workspace_root, task.node_id)
            if not _can_use(node, TOOL_UPDATE if patch is not None else TOOL_READ):
                raise HTTPException(403, "node cannot access todo tools")
            # Synchronous children retain their parent's task.session_id, while
            # async children run in their own session (possibly an entry branch).
            session_id = inp.get("child_session_id") or task.session_id
            sid = _owner(state, session_id)
            task_sid = _owner(state, task.session_id)
            if task.caller_task_id and not inp.get("child_session_id"):
                caller = state.tasks.get(task.caller_task_id)
                if caller is None or sid == _owner(state, caller.input.get("child_session_id") or caller.session_id):
                    raise HTTPException(403, "child task has no independent todo session")
            origin = inp.get("_dispatch_origin")
            if origin and not inp.get("child_session_id"):
                parent_sid = origin.get("parent_session_id") if isinstance(origin, dict) else None
                if not parent_sid or sid == _owner(state, parent_sid):
                    raise HTTPException(403, "dispatched task has no independent todo session")
            # Task generation belongs to task.session_id, not a synchronous
            # child's independently persisted session. Reset owners fail above.
            if generation != state.session_generations.get(task_sid, generation):
                raise HTTPException(409, "session generation changed")
            actor = {"type": "model", "node_id": task.node_id, "task_id": task_id}
        else:
            sid = _owner(state, session_id)
        current = read_list(state.workspace_root, sid)
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
        raise ValueError("todo tools require an active task")
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
             "expected_session_id=" + json.dumps(data["session_id"], ensure_ascii=False),
             "本清单只属于上述 session；父会话、子会话和历史中的其他清单不属于当前清单。",
             "可直接使用本摘要的 expected_session_id、revision 和事项 ID 调用 update_todos，无需重复读取。",
             "update_todos 默认 mode='replace' 整表覆盖（开启新任务时直接传入新步骤从头写，传 items=[] 清空）；"
             "同一任务推进中可用 mode='append' 按 ID 改状态或在末尾追加。少于 3 步的简单任务或纯问答无需建待办。",
             "用 read_todos 读取完整清单或解决版本冲突。同一任务推进中不要漏掉未完成步骤。"]
    active = False
    # Preserve original order within each status; prioritize actionable work.
    for status in ("in_progress", "blocked", "pending"):
        for item in data["items"]:
            if item["status"] != status:
                continue
            active = True
            line = json.dumps({k: item[k] for k in ("id", "title", "status", "note")}, ensure_ascii=False)
            if sum(map(len, lines)) + len(line) > MAX_SUMMARY - 100:
                return "\n".join(lines) + "\n清单摘要已截断，请用工具读取其余事项。"
            lines.append(line)
    if data["items"] and not active:
        lines.append("当前清单无未完成事项；若原任务已结束，开启新任务时直接用默认 mode='replace' 重写，或传 items=[] 清空旧记录。")
    return "\n".join(lines)


async def refresh_context(ctx: Any) -> HookResult | None:
    """Append current-session state to the request copy, never rewrite history.

    The shared before_llm_call path also covers resumed, compacted, forked and
    fallback requests. Old injected summaries are not authority for this session.
    """
    before = len(ctx.messages)
    ctx.messages[:] = [m for m in ctx.messages if not (
        m.get("_ide_todos") or (m.get("_meta") or {}).get("_ide_todos")
    )]
    rctx = ctx.rctx
    if (rctx is None or not getattr(rctx, "task_id", "")
            or not _can_use(ctx.node, TOOL_READ) or getattr(rctx, "is_system_task", False)):
        return HookResult(modified=True) if len(ctx.messages) != before else None
    import httpx

    try:
        data = await _request(rctx)
        text = summary(data)  # Empty lists still supply the identity for safe creation.
    except (ValueError, httpx.HTTPError):
        text = "当前会话待办未能读取。旧摘要不可用于更新，也不要将父会话清单当作自己的清单；请重新读取。"
    ctx.messages.append({
        "role": "user", "content": text, "_dynamic": True, "_ide_todos": True,
        "message_type": "dynamic_context",
        "_meta": {"_dynamic": True, "_ide_todos": True, "message_type": "dynamic_context"},
    })
    return HookResult(modified=True)


def register_engine(ctx: Any) -> None:
    tools = getattr(ctx.contributions, "get", lambda _name: None)("tools")
    if tools is None:
        return
    tools.register_builtin_tool(TOOL_READ,
        "读取当前执行会话的完整持久待办清单、session_id 与 revision，不要求工作区或文件目录。"
        "主会话和子代理各有独立清单，子代理不会继承父清单。用于摘要截断、查看完成项、版本冲突；"
        "当前摘要已含所需信息时无需重复读取。",
        {"type": "object", "properties": {}, "additionalProperties": False}, read_todos)
    tools.register_builtin_tool(TOOL_UPDATE,
        "创建、整表重写或增量更新当前会话的待办清单（少于 3 步的简单任务或纯问答无需建待办）。"
        "默认 mode='replace' 整表覆盖：传入完整新清单直接替换旧清单（开启新任务时直接从头写，传 items=[] 清空）。"
        "同一任务推进中可用 mode='append' 增量合并：条目带已有 id 修改对应事项（未改字段可省略或填 null），不带 id 在末尾追加新事项。"
        "新增事项只需填非空 title（status 默认 'pending'，note 默认空，自动分配短序号 ID）；受阻或取消必须有 note 原因。"
        "expected_session_id 和 expected_revision 必须来自当前清单的最新摘要或 read_todos 结果，"
        "不能使用父会话的清单身份；expected_session_id 只校验归属，不能选择其他会话。"
        "冲突或网络错误后先重新读取，禁止覆盖用户更新。标记 completed 不代表 Goal 验收通过。",
        UPDATE_SCHEMA, update_todos)
    # The request hook gets an isolated copy. It never mutates a persisted prefix
    # or inserts a user row between a native tool call and its pending result.
    ctx.hooks.register("before_llm_call", refresh_context, priority=40)
