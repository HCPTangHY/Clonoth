"""MCP 工具桥：内置目录插件。

三个进程面按可用 face 分流（参照 retry_api / plugin_manager 的双进程形态）：

- engine 进程（tools face 可用）：PLUGIN_META 声明统一的配置管理工具；
  构造时启动后台桥任务——先做一次全量发现，之后每 10 秒比对
  data/mcp_clients.yaml 的 mtime，变化即重发现。发现结果通过
  register_builtin_tool 注册为一等工具（mcp_{cid}_{name}），写入 reload
  快照，registry.reload() 后天然存活；每个 disposer 同时归档到
  HookRegistry 的 disposal ledger，插件卸载时全部摘除。
- supervisor 进程（routes face 可用）：挂三个配置端点，mount="admin/config"
  保持与旧 admin_api 内联版本完全相同的路径与响应形状，前端零改动。
- 两进程共享：mcp_runtime.py 作为插件私有库（协议客户端运行时）。

CRUD 工具保存/删除配置后立即在本 worker 刷新，并 POST /v1/tools/reload
bump reload-seq：其他 worker 的 MCP 工具经快照存活、经 mtime 监视在 10 秒
内跟进新配置。

首次工具列表构建前可调用 wait_mcp_initial_discovery，最多等待15秒；
失败或未完成的客户端由后续监视周期重试。内置插件的卸载是临时性的（下一个 AI 任务会重新自动
注册），这是全部内置插件的既有语义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import mcp_runtime

logger = logging.getLogger(__name__)

_WATCH_INTERVAL_SEC = 10.0


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


# ---------------------------------------------------------------------------
#  CRUD meta tool（engine 进程，经 PLUGIN_META["tools"] 由 loader 注册）
# ---------------------------------------------------------------------------


def _config_write_path(ctx: Any) -> str:
    """Absolute path of the config file this plugin mutates (audited approval)."""
    return str(Path(getattr(ctx, "workspace_root")) / "data" / "mcp_clients.yaml")


async def _request_config_write(ctx: Any, **parameters: Any) -> dict[str, Any] | None:
    """Ask the policy engine for one guarded write; None means allowed."""
    from toolbox._common import request_guard

    _op, err = await request_guard(
        ctx, "write_file", {"path": _config_write_path(ctx), **parameters}
    )
    if err is None:
        return None
    # Keep approval failures distinguishable from user denial through this wrapper.
    return {**_err(err.get("error", "denied")), **err}


async def _list_mcp_clients(ctx: Any) -> dict[str, Any]:
    """List clients with every secret replaced (this answer reaches the model)."""
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
    except Exception as exc:
        return _err(exc)
    redacted = [mcp_runtime.redact_client_spec(client) for client in clients]
    if not redacted:
        return _ok("0 MCP clients (data/mcp_clients.yaml is empty)", clients=[])
    lines = [f"{len(redacted)} MCP clients (config: data/mcp_clients.yaml):"]
    for client in redacted:
        enabled = "enabled" if client.get("enabled", True) else "disabled"
        line = f"- {client.get('id')} | {client.get('transport')} | {enabled}"
        target = str(client.get("command") or client.get("url") or "")
        if target:
            line += f" | {target}"
        if client.get("transport_error"):
            line += f" | INVALID: {client['transport_error']}"
        if client.get("description"):
            line += f" | {client['description']}"
        lines.append(line)
    return _ok("\n".join(lines), clients=redacted)


async def _upsert_mcp_client(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Validate → approval → write → refresh live tools."""
    try:
        # Validated before the guard: a bad payload must never open an approval
        # prompt, and it must never reach the file.
        record = mcp_runtime.validate_client_spec(args)
    except ValueError as exc:
        return _err(exc)
    client_id = str(record["id"])
    denied = await _request_config_write(ctx, mcp_client_id=client_id)
    if denied is not None:
        return denied
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as exc:
        return _err(exc)
    tool_count = await _refresh_and_notify(ctx)
    return _ok(
        f"MCP client saved: {client_id} ({tool_count} tools live)",
        client=mcp_runtime.redact_client_spec(spec),
        path="data/mcp_clients.yaml",
        tools=tool_count,
    )


async def _delete_mcp_client(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Delete one client, but only once it is known to exist."""
    client_id = str(args.get("id") or "").strip()
    if not client_id:
        return _err("empty client id")
    try:
        mcp_runtime.get_client_spec(ctx.workspace_root, client_id)
    except ValueError as exc:
        # Missing/invalid client: nothing to approve, nothing to write.
        return _err(exc)
    denied = await _request_config_write(ctx, delete_mcp_client=client_id)
    if denied is not None:
        return denied
    try:
        if not mcp_runtime.delete_client(ctx.workspace_root, client_id):
            return _err(f"client not found: {client_id}")
    except Exception as exc:
        return _err(exc)
    tool_count = await _refresh_and_notify(ctx)
    return _ok(
        f"MCP client deleted: {client_id} ({tool_count} tools live)",
        deleted=True,
        id=client_id,
        tools=tool_count,
    )


async def manage_mcp_client(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Unified MCP client management tool: list, upsert, or delete.

    A missing action is always a listing (it must never write), and action
    aliases are rejected: a policy denying ':delete' must not be bypassable by
    spelling the same intent differently.
    """
    action = str(args.get("action") or "").strip().lower()
    if not action or action == "list":
        return await _list_mcp_clients(ctx)
    if action == "upsert":
        return await _upsert_mcp_client(args, ctx)
    if action == "delete":
        return await _delete_mcp_client(args, ctx)
    return _err(f"invalid action: {action!r}, must be 'list', 'upsert', or 'delete'")


async def _refresh_and_notify(ctx: Any) -> int:
    """配置变更后：本 worker 立即刷新，并 bump reload-seq 通知其他 worker。

    Why: CRUD 之前保存配置后既不刷新也不通知，新客户端的工具要等手动
    reload_tools 或进程重启才出现。How: 先对 ctx.registry 跑一次
    refresh_mcp_tools（持有 mcp_* 工具的 worker 立即可用），再用引擎
    http 客户端（自带 admin token）POST /v1/tools/reload；其他 worker 的
    既有 mcp_* 工具经 reload 快照存活，新配置由各自的 mtime 监视跟进。
    Purpose: 配置变更秒级生效，不再依赖重启。
    """
    count = 0
    registry = getattr(ctx, "registry", None)
    if registry is not None:
        try:
            count = await refresh_mcp_tools(registry)
        except Exception:
            logger.warning("post-CRUD MCP refresh failed", exc_info=True)
    http = getattr(ctx, "http", None)
    supervisor_url = str(getattr(ctx, "supervisor_url", "") or "").rstrip("/")
    if http is not None and supervisor_url:
        try:
            await http.post(f"{supervisor_url}/v1/tools/reload")
        except Exception:
            pass  # 其他 worker 由 mtime 监视兜底
    return count


# ---------------------------------------------------------------------------
#  动态工具发现（engine 进程后台桥）
# ---------------------------------------------------------------------------

# 桥状态由事件循环持有，普通节点重入复用；完整删除并重导入插件包后，
# 新旧 runtime 仍通过 shared_state() 访问同一份状态，完成资源交接。
_BRIDGE_TASK_NAME = "mcp-bridge"
# After asking a watcher to stop cooperatively, how long to wait before
# cancelling it; the watcher never blocks for more than one watch interval.
_BRIDGE_STOP_GRACE_SEC = 2.0
_BRIDGE_STOP_HARD_SEC = 5.0


def _bridge_state() -> dict[str, Any]:
    return mcp_runtime.shared_state()


def _same_loop(task: Any) -> bool:
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except Exception:
        return False


async def _stop_bridge_task(task: Any, stop_event: Any) -> None:
    """Ask one watcher to stop, cancelling it only if it does not comply.

    Why: a cancellation request alone is not a completed ownership hand-off;
    teardown must also wait for the watcher to leave. How: flip an explicit
    stop flag the idle loop is already waiting on, wait for it to exit, and
    cancel only as a backstop.
    Purpose: unload is complete and deterministic, not "probably soon".
    """
    if task is None or task is asyncio.current_task() or not _same_loop(task):
        # A task from another loop cannot be driven from here; its generation
        # check makes it exit on its own next wake-up.
        return
    if not task.done() and stop_event is not None:
        stop_event.set()
        await asyncio.wait({task}, timeout=_BRIDGE_STOP_GRACE_SEC)
    if not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=_BRIDGE_STOP_HARD_SEC)
    if task.done() and not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


def _start_engine_bridge(tools_face: Any, ctx: Any) -> int | None:
    """Reuse this worker's bridge on node entry; replace it only on real reload.

    Record ownership before scheduling discovery so concurrent node starts share
    the same initial future. A new module implementation, registry, stopped or
    failed watcher still takes the normal ownership hand-off path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（非 engine 推理路径）：不启动后台桥。
        return
    state = _bridge_state()
    previous = state.get("task")
    previous_stop = state.get("stop")
    initial = state.get("initial_discovery")
    hooks = getattr(ctx, "hooks", None)
    if (
        previous is not None and _same_loop(previous)
        and not previous.done() and not previous.cancelling()
        and previous_stop is not None and not previous_stop.is_set()
        and initial is not None and initial[1] is tools_face
        and initial[0] == state.get("generation")
        and state.get("owner_hooks") is hooks
        and state.get("implementation") is _bridge_main
    ):
        return int(state["generation"])
    generation = int(state.get("generation") or 0) + 1
    state["generation"] = generation
    if initial is not None and not initial[2].done():
        initial[2].set_result(False)
    state["initial_discovery"] = (generation, tools_face, loop.create_future())
    state["owner_hooks"] = hooks
    state["implementation"] = _bridge_main
    stop_event = asyncio.Event()
    state["stop"] = stop_event
    task = loop.create_task(
        _bridge_main(
            tools_face, hooks, previous, previous_stop,
            generation, stop_event,
        ),
        name=_BRIDGE_TASK_NAME,
    )
    state["task"] = task
    return generation


def _bridge_lifecycle_lock() -> asyncio.Lock:
    """Serialize asynchronous ownership release against a replacement bridge."""
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_clonoth_mcp_bridge_lifecycle_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, "_clonoth_mcp_bridge_lifecycle_lock", lock)
    return lock


async def wait_mcp_initial_discovery(timeout: float = 15.0, *, registry: Any = None) -> bool:
    """Wait for the current generation's first attempt, not all clients' success.

    The 15-second startup budget follows the upstream discovery wait. A timeout
    or caller cancellation never cancels the shared watcher/future. Replacement
    generations share this caller's original deadline, so reload cannot extend it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        state = _bridge_state()
        initial = state.get("initial_discovery")
        if initial is None or (registry is not None and initial[1] is not registry):
            return False
        generation, _, ready = initial
        if ready.get_loop() is not loop:
            return False
        try:
            if ready.done():
                completed = ready.result()
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                completed = await asyncio.wait_for(asyncio.shield(ready), remaining)
        except asyncio.TimeoutError:
            return False
        if state.get("initial_discovery") is initial and state.get("generation") == generation:
            return bool(completed)
        if loop.time() >= deadline:
            return False


async def _bridge_main(
    registry: Any, hooks: Any, previous: Any, previous_stop: Any,
    generation: int, stop_event: asyncio.Event,
) -> None:
    """Release the previous bridge, then watch the config file."""
    initial = _bridge_state().get("initial_discovery")
    try:
        await _stop_bridge_task(previous, previous_stop)
        async with _bridge_lifecycle_lock():
            if _bridge_state().get("generation") != generation or stop_event.is_set():
                return
            await _drop_bridge_ownership()
            state = _bridge_state()
            if state.get("generation") != generation or stop_event.is_set():
                return
            state["registry"] = registry
            state["hooks"] = hooks
            state["workspace_root"] = Path(getattr(registry, "workspace_root"))
        await _bridge_loop(registry, generation, stop_event)
    finally:
        if initial is not None and initial[0] == generation and not initial[2].done():
            initial[2].set_result(False)
        state = _bridge_state()
        if state.get("task") is asyncio.current_task():
            state["task"] = None
        if state.get("stop") is stop_event:
            state["stop"] = None


async def _drop_bridge_ownership(*, close_actors: bool = True) -> None:
    """Dispose every tool this bridge registered and close its sessions."""
    state = _bridge_state()
    registry = state.get("registry")
    for client_id in list(state.get("disposers") or {}):
        _discard_client_tools(registry, client_id)
    state["registry"] = None
    state["hooks"] = None
    state["workspace_root"] = None
    state["failed_clients"] = set()
    state["retry_pending"] = True
    if close_actors:
        try:
            await mcp_runtime.close_stdio_actors()
        except Exception:
            logger.warning("MCP session teardown failed", exc_info=True)


async def shutdown_bridge(*, expected_generation: int | None = None) -> None:
    """Stop the watcher, dispose every MCP tool and close the stdio actors.

    Public entry used by McpPlugin.teardown() and by tests: after it returns,
    this process holds no MCP watcher task, no mcp_* registration from this
    plugin and no stdio child.
    """
    state = _bridge_state()
    if expected_generation is not None and state.get("generation") != expected_generation:
        return  # An old plugin instance must never shut down its replacement.
    generation = int(state.get("generation") or 0) + 1
    state["generation"] = generation
    initial = state.pop("initial_discovery", None)
    if initial is not None and not initial[2].done():
        initial[2].set_result(False)
    task = state.get("task")
    stop_event = state.get("stop")
    state["task"] = None
    state["stop"] = None
    await _stop_bridge_task(task, stop_event)
    async with _bridge_lifecycle_lock():
        if state.get("generation") == generation:
            await _drop_bridge_ownership()


async def _bridge_loop(registry: Any, generation: int, stop_event: asyncio.Event) -> None:
    state = _bridge_state()
    ws = Path(getattr(registry, "workspace_root"))
    last_mtime: int | None | object = object()
    initial = state.get("initial_discovery")
    while True:
        if int(state.get("generation") or 0) != generation or stop_event.is_set():
            return  # superseded or asked to stop: never leave a second watcher
        try:
            mtime = _config_mtime(ws)
            changed = mtime != last_mtime
            if changed or state.get("retry_pending", True):
                await refresh_mcp_tools(registry, retry_only=not changed)
                # Use the pre-discovery timestamp: edits made while discovery
                # was waiting on a server must trigger another refresh.
                last_mtime = mtime
        except asyncio.CancelledError:
            raise
        except Exception:
            state["retry_pending"] = True
            logger.warning("mcp bridge watch iteration failed", exc_info=True)
        if (initial is not None and initial[0] == generation
                and state.get("generation") == generation and not stop_event.is_set()
                and not initial[2].done()):
            initial[2].set_result(True)
        # Idle wait is an explicit stop handshake, not a bare sleep: teardown
        # sets the flag and the watcher exits on the same loop iteration.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_WATCH_INTERVAL_SEC)
        except asyncio.TimeoutError:
            continue
        return


def _config_mtime(workspace_root: Path) -> int | None:
    try:
        return (Path(workspace_root) / "data" / "mcp_clients.yaml").stat().st_mtime_ns
    except OSError:
        return None


# Regex constructs that are valid ECMA-262 but rejected by Python's re.
# A pattern using them must be preserved verbatim, not dropped as invalid.
_JS_ONLY_PATTERN = re.compile(r"[\\][pP]\{|[\\]k<[A-Za-z_$]|[\\]u\{|\(\?<[A-Za-z_$]")


def _sanitize_schema_patterns(schema: dict[str, Any]) -> None:
    # [2026-09-29] Why: MCP servers may publish regex `pattern` values with
    # escapes that are invalid in ECMA-262 (observed: "^agent\\_run\\_" from
    # the exa server — markdown escaping leaking into a machine field).
    # OpenAI-compatible providers such as DeepSeek compile patterns and reject
    # the whole request on failure, while Anthropic treats schemas as opaque,
    # How: walk the schema, rewrite `\_` to `_` (identical literal match), and
    # drop only patterns that are invalid in ECMA-262 too (Python-only
    # compile failures such as \p{L} or (?<name>x) are preserved verbatim).
    # Purpose: one upstream defect must not break an entire channel, and a
    # valid JavaScript constraint must not be dropped just because Python's re
    # rejects the syntax. Dropping is transport-safe (pattern is advisory).
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "pattern" and isinstance(value, str):
                    fixed = value.replace("\\_", "_")
                    if fixed != value:
                        logger.info(
                            "MCP tool schema pattern sanitized: %r -> %r", value, fixed
                        )
                        node[key] = fixed
                        value = fixed
                    if _JS_ONLY_PATTERN.search(value):
                        # Valid ECMA-262 that Python's re cannot compile: keep it
                        # verbatim instead of dropping a working constraint.
                        continue
                    try:
                        re.compile(value)
                    except re.error:
                        logger.warning(
                            "MCP tool schema pattern dropped (invalid in every dialect): %r", value
                        )
                        del node[key]
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))


def _discard_client_tools(registry: Any, client_id: str) -> None:
    """Remove one client's registrations without disturbing other clients."""
    state = _bridge_state()
    forget = getattr(state.get("hooks"), "remove_plugin_disposer", None)
    for name, dispose in reversed(state["disposers"].pop(client_id, [])):
        try:
            dispose()
        except Exception:
            logger.warning("MCP tool disposer failed: %s", name, exc_info=True)
        if callable(forget):
            try:
                forget(dispose)
            except Exception:
                logger.warning("MCP disposer cleanup failed: %s", name, exc_info=True)
        # Defensive cleanup scoped to the exact tool names we registered.
        for attr in ("_tool_specs", "_tool_funcs", "_builtin_specs", "_builtin_funcs"):
            mapping = getattr(registry, attr, None)
            if isinstance(mapping, dict):
                mapping.pop(name, None)
    state["specs"].pop(client_id, None)
    state.get("registration_signatures", {}).pop(client_id, None)


def _refresh_lock() -> asyncio.Lock:
    """Per-loop refresh lock (one global lock would bind to the first loop)."""
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_clonoth_mcp_refresh_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, "_clonoth_mcp_refresh_lock", lock)
    return lock


def _tool_is_pure(tool: dict[str, Any]) -> bool:
    """Purity comes only from the server's own annotations.

    Why: the previous heuristic guessed from tool names and client ids, so a
    tool named ``search_*`` that writes to a database ran in parallel with its
    siblings. How: readOnlyHint with no destructiveHint, exactly as published.
    Purpose: parallel scheduling only where the server says it is safe.
    """
    annotations = tool.get("annotations")
    if not isinstance(annotations, dict):
        return False
    return annotations.get("readOnlyHint") is True and not annotations.get("destructiveHint")


def _purge_stale_mcp_tools(registry: Any) -> None:
    """Drop mcp_* entries left behind by a previous incarnation of this bridge."""
    for attr in ("_tool_specs", "_tool_funcs", "_builtin_specs", "_builtin_funcs"):
        mapping = getattr(registry, attr, None)
        if isinstance(mapping, dict):
            for name in list(mapping):
                if isinstance(name, str) and name.startswith("mcp_"):
                    mapping.pop(name, None)


async def refresh_mcp_tools(registry: Any, *, retry_only: bool = False) -> int:
    """Discover per client; retain unchanged registrations on transient failure.

    Deleted, disabled and reconfigured clients lose their old registrations
    immediately. Other clients are replaced only when discovered definitions or
    the wrapper implementation change; unchanged wrappers retain their identity.
    Failed clients are retried by the existing ten-second watcher.
    """
    async with _refresh_lock():
        state = _bridge_state()
        state["retry_pending"] = True
        generation = state.get("generation")
        ws = Path(getattr(registry, "workspace_root"))
        try:
            clients = mcp_runtime.list_clients(ws)
        except Exception:
            logger.warning("MCP configuration unreadable; keeping existing registrations", exc_info=True)
            return sum(len(items) for items in state["disposers"].values())

        if state.get("registry") is not registry:
            # First refresh for this registry: adopt it and drop whatever a
            # previous incarnation of the bridge left registered there.
            _purge_stale_mcp_tools(registry)
            state["registry"] = registry
        if state.get("workspace_root") is None:
            state["workspace_root"] = ws

        active = {
            str(client["id"]): client for client in clients
            if client.get("id") and client.get("enabled", True)
            and not client.get("transport_error")
        }
        for cid in list(state["disposers"]):
            if cid not in active or state["specs"].get(cid) != active[cid]:
                _discard_client_tools(registry, cid)
        cleanup_complete = False
        try:
            cleanup_complete = await mcp_runtime.reconcile_stdio_actors(clients)
        except Exception:
            # One session's shutdown must not prevent discovery for other clients.
            logger.warning("MCP session cleanup failed; continuing discovery", exc_info=True)

        if state.get("generation") != generation:
            return 0
        failed: set[str] = set()
        for cid, client in active.items():
            if (
                retry_only and cid not in state["failed_clients"]
                and cid in state["disposers"] and state["specs"].get(cid) == client
            ):
                continue
            replacing = False
            try:
                result = await asyncio.wait_for(
                    mcp_runtime.list_tools(ws, cid), timeout=mcp_runtime.MCP_CALL_TIMEOUT,
                )
                if state.get("generation") != generation:
                    return 0
                if not isinstance(result, dict) or result.get("ok") is False:
                    raise ValueError("MCP tool discovery failed")
                result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
                tools = result_data.get("tools") if isinstance(result_data.get("tools"), list) else result.get("tools")
                if not isinstance(tools, list):
                    raise ValueError("MCP discovery returned no tool list")

                definitions = {}
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    raw_name = str(tool.get("name") or "").strip()
                    if not raw_name:
                        continue
                    reg_name = f"mcp_{cid}_{raw_name}"
                    desc = str(tool.get("description") or "").strip()
                    schema = tool.get("input_schema")
                    if not isinstance(schema, dict):
                        schema = {"type": "object", "properties": {}, "required": []}
                    _sanitize_schema_patterns(schema)
                    definitions[reg_name] = {
                        "raw_name": raw_name,
                        "description": f"[MCP:{cid}] {desc}" if desc else f"[MCP:{cid}] {raw_name}",
                        "input_schema": schema,
                        "pure": _tool_is_pure(tool),
                    }
                # JSON comparison distinguishes booleans/numbers in schemas and
                # ignores object/tool listing order. Keep actual discovery, but
                # do not invalidate unrelated calls waiting for approval.
                signature = (registry, _make_mcp_tool, ws, json.dumps(
                    [client, definitions], sort_keys=True, allow_nan=False,
                ))
                signatures = state.setdefault("registration_signatures", {})
                if cid in state["disposers"] and signatures.get(cid) == signature:
                    continue
                _discard_client_tools(registry, cid)
                replacing = True
                state["disposers"][cid] = []
                for reg_name, definition in definitions.items():
                    raw_name = definition["raw_name"]
                    schema = definition["input_schema"]
                    pure = definition["pure"]
                    try:
                        dispose = registry.register_builtin_tool(
                            reg_name,
                            definition["description"],
                            schema,
                            _make_mcp_tool(ws, cid, raw_name),
                            pure=pure,
                        )
                    except TypeError:
                        dispose = registry.register_builtin_tool(
                            reg_name,
                            definition["description"],
                            schema,
                            _make_mcp_tool(ws, cid, raw_name),
                        )
                    state["disposers"][cid].append((reg_name, dispose))
                    add = getattr(state.get("hooks"), "add_plugin_disposer", None)
                    if callable(add):
                        try:
                            # Keyed by (client, tool) so a refresh replaces the
                            # previous undo closure instead of stacking dead ones.
                            add("mcp", dispose, key=(cid, reg_name))
                        except TypeError:
                            add("mcp", dispose)
                        except Exception:
                            pass
                state["specs"][cid] = client
                signatures[cid] = signature
            except Exception:
                if state.get("generation") != generation:
                    return 0
                if replacing:
                    _discard_client_tools(registry, cid)
                failed.add(cid)
                logger.warning("MCP client '%s' discovery failed; will retry", cid, exc_info=True)
        state["failed_clients"] = failed
        state["retry_pending"] = bool(failed) or not cleanup_complete
        return sum(len(items) for items in state["disposers"].values())


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> Callable:
    """可取消的 MCP 工具调用包装（自 toolbox/registry.py 原样迁入）。"""

    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Why: MCP tool calls previously had no cancel support — the
        # engine loop only checks cancel between tools, not during a tool's await.
        # How: run the MCP call in a background task and poll check_cancelled every
        # 2 seconds. If cancel is detected, cancel the background task and return a
        # cancelled result. Purpose: users can stop stuck MCP calls without restarting.
        try:
            mcp_task = asyncio.ensure_future(
                mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
            )
            while not mcp_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(mcp_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                if mcp_task.done():
                    break
                # Check cancel
                if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                    mcp_task.cancel()
                    try:
                        await mcp_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled by user", "cancelled": True}
            return mcp_task.result()
        except asyncio.CancelledError:
            return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled", "cancelled": True}
        except Exception as e:
            return _err_tool(str(e), mcp_client=client_id, mcp_tool=tool_name)

    return _call


def _err_tool(message: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}", **fields}}


# ---------------------------------------------------------------------------
#  supervisor 进程：admin/config 配置端点
# ---------------------------------------------------------------------------

class RawContent(BaseModel):
    content: str


def _workspace_root(request: Request) -> Path:
    return Path(request.app.state.state.workspace_root)


def list_mcp_clients_endpoint(request: Request) -> list[dict[str, Any]]:
    try:
        data = mcp_runtime._load_raw_config(_workspace_root(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clients = data["clients"]
    res: list[dict[str, Any]] = []
    for cid, spec in sorted(clients.items()):
        if not isinstance(spec, dict):
            continue
        item: dict[str, Any] = {"id": str(cid)}
        item.update(spec)
        res.append(item)
    return res


def get_mcp_clients_raw_endpoint(request: Request) -> dict[str, str]:
    p = _workspace_root(request) / "data" / "mcp_clients.yaml"
    if not p.exists():
        return {"content": "version: 1\nclients: {}\n"}
    return {"content": p.read_text(encoding="utf-8")}


def update_mcp_clients_raw_endpoint(payload: RawContent, request: Request) -> dict[str, Any]:
    try:
        mcp_runtime.save_raw_config_content(_workspace_root(request), payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


def _register_admin_routes(routes: Any) -> None:
    from engine.faces.routes import static_router

    router = APIRouter()
    router.add_api_route("/mcp-clients", list_mcp_clients_endpoint, methods=["GET"])
    router.add_api_route("/mcp-clients/raw", get_mcp_clients_raw_endpoint, methods=["GET"])
    router.add_api_route("/mcp-clients/raw", update_mcp_clients_raw_endpoint, methods=["PUT"])
    routes.register(router, mount="admin/config", key="mcp:config", description="MCP 客户端配置端点")

    # 面板与依赖资源需要鉴权；宿主用 Bearer 换取短期静态资源 Cookie。
    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "web"),
        prefix="/web",
    )
    routes.register(client, key="mcp:panel", description="MCP 设置面板静态资源（需鉴权）")


# ---------------------------------------------------------------------------
#  Handler 与 PLUGIN_META
# ---------------------------------------------------------------------------

class McpPlugin:
    """按可用 face 分流注册：supervisor 挂配置端点，engine 启动工具桥。"""

    name = "mcp"
    priority = 50
    teardown_key = ("mcp", "bridge")

    def __init__(self, ctx: Any = None) -> None:
        self._bridge_generation: int | None = None
        self._bridge_loop: asyncio.AbstractEventLoop | None = None
        contributions = getattr(ctx, "contributions", None)
        get_face = getattr(contributions, "get", lambda _n: None)
        routes = get_face("routes")
        tools = get_face("tools")
        if routes is not None:
            _register_admin_routes(routes)
        if tools is not None:
            self._bridge_generation = _start_engine_bridge(tools, ctx)
            if self._bridge_generation is not None:
                self._bridge_loop = asyncio.get_running_loop()

    def teardown(self) -> Any:
        """Release this plugin's tools, watcher task and stdio children.

        Why: the loader archives teardown() as the plugin's disposer, so an
        unload/reload has to undo the background bridge — a fire-and-forget
        cancel leaves the old watcher, its tool registrations and one child
        process per MCP client behind. How: schedule the async release on the
        running loop and hand the task back. Purpose: unloading is complete
        when the returned task completes, and nothing survives the unload.
        """
        generation = self._bridge_generation
        if generation is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if loop is not self._bridge_loop:
            return None  # Generation numbers are local to their owning loop.
        state = _bridge_state()
        if state.get("generation") != generation:
            return None
        # Synchronous invalidation prevents a new load from reusing a bridge
        # whose asynchronous teardown has been scheduled but not run yet.
        stop = state.get("stop")
        if stop is not None:
            stop.set()
        return loop.create_task(
            shutdown_bridge(expected_generation=generation), name="mcp-bridge-teardown",
        )

PLUGIN_META = {
    "handler_class": "McpPlugin",
    "hook_points": [],
    "priority": 50,
    "wants_context": True,
    "description": "MCP 客户端工具桥：动态工具发现（engine）、配置 CRUD 元工具、admin/config 端点与设置面板（supervisor）",
    "author": "core",
    "web": {
        "panels": [
            {
                "id": "settings",
                "slot": "settings",
                "order": 10,
                "title": "MCP",
                "icon": "cable",
                "entry": "/v1/plugins/mcp/web/",
            }
        ],
    },
    "tools": [
        {
            "name": "manage_mcp_client",
            "description": (
                "Manage MCP client configurations in data/mcp_clients.yaml: "
                "action='list' lists all configured MCP clients; "
                "action='upsert' creates or updates an MCP client (stdio, sse, or streamable_http; tools go live immediately); "
                "action='delete' removes an MCP client by id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "upsert", "delete"], "description": "Operation: 'list' (default), 'upsert' (create/update), or 'delete'."},
                    "id": {"type": "string", "description": "[upsert/delete] MCP client ID."},
                    "description": {"type": "string", "description": "[upsert] Client description."},
                    "enabled": {"type": "boolean", "description": "[upsert] Whether the client is enabled."},
                    "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "streamable-http", "http"], "description": "[upsert] Transport type."},
                    "command": {"type": "string", "description": "[upsert stdio] Executable command."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "[upsert stdio] Command arguments."},
                    "env": {"type": "object", "description": "[upsert stdio] Environment variables."},
                    "stateful": {"type": "boolean", "description": "[upsert stdio] Whether the server holds cross-call state (uses 1800s idle TTL instead of 900s)."},
                    "idle_ttl_sec": {"type": "number", "description": "[upsert stdio] Optional custom idle timeout in seconds before reaping the stdio subprocess."},
                    "url": {"type": "string", "description": "[upsert sse/http] Remote MCP endpoint URL."},
                    "headers": {"type": "object", "description": "[upsert sse/http] Optional HTTP headers."},
                },
                "required": ["action"],
            },
            "func": manage_mcp_client,
        },
    ],
}
