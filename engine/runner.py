from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from clonoth_runtime import (
    fetch_llm_secret,
    get_bool,
    get_entry_node_id,
    get_float,
    get_int,
    get_str,
    load_runtime_config,
    load_yaml_dict,
    normalize_llm_secret,
)
from providers import registry as provider_registry
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

from .inference import run_ai_node
from .attachments import build_multimodal_content
from .context import RunContext
from .model import resolve_provider
from .context_store import load_context_snapshot
from .inference.tool_format import sanitize_control_tool_history

from .node import Node, load_node
from .protocol import TaskAction
# [2026-04-17] write_artifact 移除：截断机制已废弃
from .tool_step import result_to_raw, summarize_result
# Phase 1 (Session Conversation Store): 导入 ConversationStore 用于影子写入，
# 在每个 node task 执行时实例化并挂载到 RunContext，供 ai_step 影子写入消息。
from .conversation_store import ConversationStore, Message, MessageType
# Phase 0/1: Signal System — 导入信号总线初始化和桥接函数。
# 在 _run_node_task 中初始化 bus 并安装 EventLog 桥接，使 LLM 调用信号
# 自动转发到 data/signals.jsonl 供监控使用。
from engine.signals import get_bus
from engine.signals.bridge import install_event_bridge


def _provider_init_kwargs(
    rp: Any,
    *,
    provider_name: str,
    llm_http: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    provider_options: dict[str, Any],
) -> dict[str, Any]:
    """Build common constructor kwargs for provider classes."""
    # [provider-registry 2026-05-03] 统一构造 provider 参数。
    # 原因：runner 不应再按 provider 类型实例化不同类；做法：先组装一个超集 kwargs，
    # 再由 _instantiate_provider 按构造签名过滤；目的：保留旧 provider 行为，同时支持新插件。
    if provider_name in {"openai", "openai-responses"}:
        resolved_api_key = rp.api_key or api_key
        resolved_base_url = rp.base_url or base_url or None
    else:
        env_prefix = provider_name.upper().replace("-", "_")
        resolved_api_key = rp.api_key or os.environ.get(f"{env_prefix}_API_KEY", "") or api_key
        resolved_base_url = rp.base_url or os.environ.get(f"{env_prefix}_BASE_URL", "") or None
    return {
        "http": llm_http,
        "api_key": resolved_api_key,
        "base_url": resolved_base_url,
        "model": rp.model,
        "provider_options": provider_options,
    }


def _merge_provider_options(global_options: dict[str, Any], node_options: dict[str, Any]) -> dict[str, Any]:
    """Merge global and node provider options, with node values taking priority."""
    # [AutoC 2026-06-01] Why: provider-wide defaults belong in runtime.yaml, but
    # individual nodes still need to override a nested option such as reasoning
    # effort. How: recursively copy dict values from the global layer, then overlay
    # node-level provider_options. Purpose: pass one complete provider_options dict
    # to provider adapters without mutating runtime or node configuration objects.
    merged: dict[str, Any] = dict(global_options or {})
    for key, value in (node_options or {}).items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key] = _merge_provider_options(base_value, value)
        else:
            merged[key] = value
    return merged


def _instantiate_provider(provider_cls: type, init_kwargs: dict[str, Any]) -> Any:
    """Instantiate a provider, passing only kwargs its constructor accepts."""
    # [provider-registry 2026-05-03] Provider 构造函数目前不完全一致。
    # 原因：OpenAI 不接收 provider_options，而其他 provider 需要；做法：用签名过滤 kwargs，
    # 若未来插件声明 **kwargs 则传入完整超集；目的：不强迫 Phase 1 改写所有 provider 构造函数。
    signature = inspect.signature(provider_cls)
    parameters = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return provider_cls(**init_kwargs)
    allowed = {
        name
        for name, param in parameters.items()
        if param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return provider_cls(**{key: value for key, value in init_kwargs.items() if key in allowed})


def _create_provider_from_registry(
    rp: Any,
    *,
    llm_http: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    provider_options: dict[str, Any],
) -> Any:
    """Resolve and instantiate the configured provider through ProviderRegistry."""
    requested_name = (rp.provider_type or "openai").strip().lower()
    provider_cls = provider_registry.get(requested_name)
    provider_name = requested_name
    if provider_cls is None:
        # [provider-registry 2026-05-03] 未注册 provider 保持旧行为：回退到 OpenAI。
        # 原因：过去未知 provider 会落入默认 OpenAI 分支；做法：registry miss 时取 openai；
        # 目的：配置错误不改变历史降级语义。
        provider_cls = provider_registry.get("openai")
        provider_name = "openai"
    if provider_cls is None:
        raise RuntimeError("provider registry did not register the fallback 'openai' provider")
    init_kwargs = _provider_init_kwargs(
        rp,
        provider_name=provider_name,
        llm_http=llm_http,
        api_key=api_key,
        base_url=base_url,
        provider_options=provider_options,
    )
    return _instantiate_provider(provider_cls, init_kwargs)


_NODE_INFO_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def _fallback_node_info(node_id: str) -> dict[str, str]:
    """Build fallback node metadata for absent or invalid node YAML."""
    return {"id": node_id, "name": node_id, "description": node_id}


def _node_info_path_and_mtime(workspace_root: Path, node_id: str) -> tuple[Path | None, int]:
    """Resolve the node YAML path and return its mtime using os.stat."""
    # [2026-05-04] Keep the same lookup order as load_node while avoiding a full
    # load_node parse on every task. Why: delegate metadata is needed for tool
    # descriptions, but parsing all target YAML files at task startup is wasteful.
    # How: stat the system-node path first, then the user-node path, returning the
    # first existing YAML file and its mtime. Purpose: unchanged YAML files can be
    # served from the module cache, while hot edits still take effect immediately.
    candidates = [
        workspace_root / "engine" / "system_nodes" / f"{node_id}.yaml",
        workspace_root / "config" / "nodes" / f"{node_id}.yaml",
    ]
    for path in candidates:
        try:
            stat_result = os.stat(path)
        except OSError:
            continue
        # [2026-05-04] Store nanosecond mtime from os.stat. Why: YAML edits can
        # happen close together during development. How: use st_mtime_ns instead
        # of the rounded float mtime. Purpose: make hot edits invalidate the cache
        # as soon as the filesystem reports a change.
        return path, int(stat_result.st_mtime_ns)
    return None, -1


def _load_cached_node_info(workspace_root: Path, node_id: str) -> dict[str, str]:
    """Load id/name/description for one node YAML with an mtime cache."""
    path, mtime = _node_info_path_and_mtime(workspace_root, node_id)
    if path is None:
        # [2026-05-04] Missing delegate YAML must not prevent tool registration.
        # Why: delegate_targets can contain absent nodes during partial config
        # rollout. How: return a fallback info row with id/name/description equal
        # to the target id. Purpose: ai_step can still register dispatch:target
        # and the runtime will fail gracefully only if the supervisor cannot run it.
        return _fallback_node_info(node_id)

    cache_key = str(path.resolve())
    cached = _NODE_INFO_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        # [2026-05-04] Return the cached metadata dict directly when mtime is
        # unchanged. Why: _collect_node_info is called on every task startup.
        # How: the cache stores (mtime, info_dict) by resolved YAML path. Purpose:
        # avoid yaml.safe_load and Node construction until the YAML file changes.
        return cached[1]

    info = _fallback_node_info(node_id)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if isinstance(data, dict):
        kind = str(data.get("kind") or "node").strip()
        node_type = str(data.get("type") or "ai").strip().lower()
        if kind == "node" and node_type in {"ai", "tool", "script"}:
            name = str(data.get("name") or node_id).strip() or node_id
            description = str(data.get("description") or "").strip() or name
            info = {
                "id": str(data.get("id") or node_id).strip() or node_id,
                "name": name,
                "description": description,
            }
    _NODE_INFO_CACHE[cache_key] = (mtime, info)
    return info


def _collect_node_info(workspace_root: Path, node_ids: list[str]) -> list[dict[str, str]]:
    """收集指定节点的基本信息（id/name/description）。"""
    result: list[dict[str, str]] = []
    for target_id in node_ids:
        tid = str(target_id or "").strip()
        if not tid:
            continue
        result.append(_load_cached_node_info(workspace_root, tid))
    return result


def _discover_switchable_nodes(workspace_root: Path, current_node_id: str) -> list[dict[str, str]]:
    """发现可切换的根节点（不被任何其他节点 delegate_targets 引用的节点），排除当前节点。"""
    # 系统节点目录分离：扫描 engine/system_nodes/ 和 config/nodes/ 两个目录，
    # 合并结果，engine 内建目录优先（同 id 跳过后扫到的）。
    nodes_dirs = [
        workspace_root / "engine" / "system_nodes",
        workspace_root / "config" / "nodes",
    ]
    all_nodes: list[dict[str, Any]] = []
    all_targets: set[str] = set()
    seen_ids: set[str] = set()
    for nodes_dir in nodes_dirs:
        if not nodes_dir.is_dir():
            continue
        for f in sorted(nodes_dir.iterdir()):
            if f.suffix not in (".yaml", ".yml") or f.name.startswith("_"):
                continue
            n = load_node(workspace_root, f.stem)
            if n is None or n.type != "ai" or n.id in seen_ids:
                continue
            seen_ids.add(n.id)
            all_nodes.append({"id": n.id, "name": n.name, "description": n.description or n.name})
            all_targets.update(n.delegate_targets)
    # 根节点 = 不被任何节点 delegate 引用的节点
    roots = [n for n in all_nodes if n["id"] not in all_targets]
    if not roots:
        roots = list(all_nodes)
    # 排除当前节点自己
    return [n for n in roots if n["id"] != current_node_id]


# [2026-05-04] Only currently supported static pseudo tools are listed here.
# Why: removed aggregate dispatch tools must not be treated as active static
# pseudo tools. How: dynamic dispatch history records are still detected by
# prefix below. Purpose: keep restored history cleanup aligned with the active
# pseudo-tool surface.
_PSEUDO_TOOL_NAMES = {"finish", "intermediate_reply", "switch_node"}


def _find_pseudo_history_marker(content: str) -> tuple[str, int]:
    """Find a trailing pseudo-tool history marker, including dispatch:target."""
    # [2026-05-04] Dynamic dispatch names cannot be enumerated in this helper.
    # Why: restored fake-native history can contain records such as
    # dispatch:child_coder. How: first check the fixed static names, then parse
    # the dispatch: prefix until the common "was executed" separator. Purpose:
    # keep history restoration from leaking pseudo-tool call records.
    for name in _PSEUDO_TOOL_NAMES:
        tag = f"[Tool call history record: {name} was executed with args: "
        pos = content.find(tag)
        if pos >= 0:
            return name, pos

    dynamic_prefix = "[Tool call history record: dispatch_to_"
    pos = content.find(dynamic_prefix)
    if pos < 0:
        return "", -1
    name_start = pos + len("[Tool call history record: ")
    tail = content[name_start:]
    separator = " was executed with args: "
    separator_pos = tail.find(separator)
    if separator_pos < 0:
        return "", -1
    return tail[:separator_pos], pos


def _message_to_history_dict(msg: Message, *, current_task_id: str = "") -> dict[str, Any]:
    """将 ConversationStore 的 Message 转为 runner 期望的 history dict 格式。

    Child Session 隔离（Phase B）：从 child session JSONL 加载的 Message 对象
    需要转为与 snapshot messages 相同的 dict 格式，供 ai_step 的消息组装使用。
    """
    # [AutoC 2026-06-18] Why: output_rejected needs msg.id to call
    # ConversationStore.update_message and persist the reject result.
    # How: carry id through the history dict. Purpose: ai_step can
    # locate and update the original finish "ok" row in JSONL.
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.id:
        d["id"] = msg.id
    # [AutoC 2026-06-01] Why: stripping every historical image made later LLM
    # steps in the same task lose the user's image input. How: keep multimodal
    # list content when source_task_id matches the active task, but preserve the
    # old all-strip behavior for callers that do not pass current_task_id. Purpose:
    # task-local history keeps locked user input while cross-task history stays
    # compact and safe against stale file:// attachment references.
    msg_task_id = str(msg.source_task_id or "")
    if isinstance(d["content"], list) and (not current_task_id or msg_task_id != current_task_id):
        d["content"] = _strip_images_from_content(d["content"])
    _meta = dict(msg.meta) if msg.meta else {}
    # P6 Snip Compact: 将 source_task_id 透传到 _meta，供 snip_history 按 task 过滤
    if msg.source_task_id:
        _meta["source_task_id"] = msg.source_task_id
    if _meta:
        d["_meta"] = _meta
    if msg.tool_calls:
        d["tool_calls"] = msg.tool_calls
    # [2026-05-01] 恢复 true native role=tool 消息的配对字段。
    # 怎么改：ConversationStore.Message 新增 tool_call_id/name，这里转回 history dict。
    # 目的：build_llm_messages 在 native 模式下能原样透传 role=tool + tool_call_id。
    if getattr(msg, "tool_call_id", ""):
        d["tool_call_id"] = msg.tool_call_id
    if getattr(msg, "name", ""):
        d["name"] = msg.name
    return d


def _strip_trailing_pseudo_call(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip trailing pseudo-tool call from the last assistant message in history.

    When restoring history from a context snapshot, the last assistant message
    may contain a pseudo-tool record like '[Tool call history record: finish was executed with args: {...}]'.
    For finish: extract the text param and replace with a normal assistant reply.
    For other pseudo tools, including dispatch:{target_id}, drop or trim.
    """
    if not history:
        return history

    last = history[-1]
    role = last.get("role", "")
    if role not in ("assistant", "user"):
        return history

    content = last.get("content", "")
    if not isinstance(content, str):
        return history

    # Look for pseudo-tool record marker
    pseudo_name, marker_pos = _find_pseudo_history_marker(content)

    if marker_pos < 0:
        return history

    pre_text = content[:marker_pos].strip()
    result = list(history)

    if pseudo_name == "finish":
        # Extract the text param and restore as assistant reply
        call_str = content[marker_pos:]
        args_start = call_str.find("args: ")
        if args_start >= 0:
            inner = call_str[args_start + len("args: "):]
            # Strip trailing ']' bracket
            last_bracket = inner.rfind("]")
            if last_bracket >= 0:
                inner = inner[:last_bracket]
                try:
                    args = json.loads(inner)
                    finish_text = str(args.get("text", "")).strip()
                    if finish_text:
                        combined = f"{pre_text}\n\n{finish_text}".strip() if pre_text else finish_text
                        # Always restore as assistant role (the finish text is the AI's final response)
                        result[-1] = {"role": "assistant", "content": combined}
                        return result
                except Exception:
                    pass

    # Other pseudo tools or parse failure: keep pre_text or drop the message.
    if pre_text:
        result[-1] = {"role": role, "content": pre_text}
        return result
    return result[:-1]


def _strip_images_from_content(content: list[dict[str, Any]]) -> str:
    """Strip image_url parts from multimodal content, return plain text with references.

    Historical images should not be re-sent every turn as base64 (expensive and redundant).
    Current turn's images are passed separately via the attachments mechanism.
    """
    text_parts: list[str] = []
    image_refs: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url = ""
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url", ""))
            elif isinstance(image_url, str):
                url = image_url

            # Why: the old generic marker hid the saved attachment path, so later
            # tasks could not reopen historical images. How: keep file:// paths as
            # lightweight text references while replacing non-file payloads with an
            # inline marker. Purpose: avoid re-sending image bytes but preserve a
            # read_file-compatible path when one exists.
            if url.startswith("file://"):
                image_refs.append(f"[图片: {url[7:]}]")
            else:
                image_refs.append("[图片: <inline>]")
        elif part.get("type") == "text":
            t = str(part.get("text", "")).strip()
            if t:
                text_parts.append(t)
    text = "\n".join(text_parts)
    if image_refs:
        refs = "\n".join(image_refs)
        text = f"{text}\n{refs}" if text else refs
    return text


async def _fetch_history(rctx: RunContext, limit: int = 40) -> list[dict[str, Any]]:
    try:
        r = await rctx.http.get(
            f"{rctx.supervisor_url}/v1/sessions/{rctx.session_id}/messages",
            params={"limit": limit},
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                result = []
                for m in data:
                    if not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "system"}:
                        continue
                    content = m.get("content", "")
                    if isinstance(content, list):
                        # Strip image_url parts from historical messages to avoid
                        # re-encoding large base64 images on every turn.
                        # Current turn's images are passed separately via attachments.
                        entry: dict[str, Any] = {"role": str(m.get("role")), "content": _strip_images_from_content(content)}
                    else:
                        entry = {"role": str(m.get("role")), "content": str(content)}
                    # Preserve message_type for downstream keyword scan filtering
                    _mt = m.get("message_type")
                    if _mt:
                        entry["message_type"] = str(_mt)
                    result.append(entry)
                return result
    except Exception:
        pass
    return []


async def _fetch_session_provider_override(
    http: httpx.AsyncClient,
    supervisor_url: str,
    session_id: str,
) -> dict[str, Any]:
    """Fetch the session-scoped provider override from supervisor."""
    sid = str(session_id or "").strip()
    if not sid:
        return {}
    try:
        # [AutoC 2026-06-01] Why: provider selection is now session-scoped in
        # supervisor state, while engine workers only receive task snapshots. How:
        # query the admin-protected provider_override endpoint with the same HTTP
        # client that already carries the supervisor admin token. Purpose: each
        # node task resolves provider settings from the current durable session.
        r = await http.get(f"{supervisor_url.rstrip('/')}/v1/sessions/{sid}/provider_override")
        if r.status_code == 200:
            data = r.json()
            return dict(data) if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


async def _fetch_session_workspace(
    http: httpx.AsyncClient,
    supervisor_url: str,
    session_id: str,
) -> dict[str, Any]:
    """Fetch the session-scoped workspace {name, path} from supervisor."""
    sid = str(session_id or "").strip()
    if not sid:
        return {}
    try:
        r = await http.get(f"{supervisor_url.rstrip('/')}/v1/sessions/{sid}/workspace")
        if r.status_code == 200:
            data = r.json()
            return dict(data) if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


async def _register_engine(
    http: httpx.AsyncClient,
    supervisor_url: str,
    worker_id: str,
    generation_id: str,
) -> dict[str, Any] | None:
    """Register this engine instance with supervisor (Direction 2: Generation ID).

    Triggers cleanup of orphaned tasks from previous engine instances.
    Returns the registration response dict.
    """
    try:
        r = await http.post(
            f"{supervisor_url}/v1/engine/register",
            json={"worker_id": worker_id, "generation_id": generation_id},
        )
        if r.status_code == 200:
            data = r.json()
            orphans = data.get("orphans_cancelled", 0)
            if orphans:
                print(f"[engine] registered generation {generation_id[:8]}, cancelled {orphans} orphan(s)", flush=True)
            else:
                print(f"[engine] registered generation {generation_id[:8]}", flush=True)
            return data
        else:
            print(f"[engine] registration failed: HTTP {r.status_code}", flush=True)
            return None
    except Exception as e:
        print(f"[engine] failed to register with supervisor: {e}", flush=True)
        return None


async def wait_supervisor(
    http: httpx.AsyncClient,
    base_url: str,
    *,
    timeout: float = 2.0,
    interval: float = 0.5,
) -> None:
    print(f"[engine] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await http.get(f"{base_url}/v1/health", timeout=timeout)
            if r.status_code == 200:
                print("[engine] supervisor connected", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(interval)


async def worker_loop(*, supervisor_url: str, workspace_root: Path, worker_id: str = "") -> None:
    wid = worker_id or str(uuid.uuid4())
    generation_id = str(uuid.uuid4())  # Direction 2: unique generation per engine startup
    # [AutoC 2026-08-01] Honor CLONOTH_SUPERVISOR_URL for remote tool discovery.
    # Why: Phase 2 remote tools are discovered through the Supervisor broker, and
    # some deployments pass a canonical broker URL through the environment. How:
    # prefer the env var when present while keeping the worker_loop argument as the
    # fallback. Purpose: engine startup, task polling, and remote tools use one URL.
    supervisor_url = os.environ.get("CLONOTH_SUPERVISOR_URL", "").strip() or supervisor_url or "http://127.0.0.1:8765"
    supervisor_url = supervisor_url.rstrip("/")
    runtime_cfg = load_runtime_config(workspace_root)
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")
    _last_reload_seq = 0

    sup_timeout = get_float(runtime_cfg, "engine.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    poll_sec = get_float(runtime_cfg, "engine.poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)
    max_workers = get_int(runtime_cfg, "engine.max_workers", 4, min_value=1, max_value=32)

    # Resolve admin token: env var > shared file > (removed: register response)
    _admin_token = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip()
    if not _admin_token:
        _token_file = workspace_root / "data" / ".admin_token"
        if _token_file.is_file():
            _admin_token = _token_file.read_text(encoding="utf-8").strip()
            if _admin_token:
                print("[engine] loaded admin token from shared file", flush=True)

    _sup_headers: dict[str, str] = {"User-Agent": "Clonoth"}
    if _admin_token:
        _sup_headers["Authorization"] = f"Bearer {_admin_token}"

    async with (
        httpx.AsyncClient(timeout=sup_timeout, trust_env=False, headers=_sup_headers) as http,
        httpx.AsyncClient(timeout=llm_timeout, trust_env=False, headers={"User-Agent": "Clonoth"}) as llm_http,
    ):
        await wait_supervisor(http, supervisor_url)

        # Direction 2: register generation with supervisor, triggering orphan cleanup
        _reg_result = await _register_engine(http, supervisor_url, wid, generation_id)

        # Token should already be resolved from env or shared file above.
        # Register no longer returns admin_token to avoid API-based token leakage.

        mcp_count = await registry.load_mcp_tools()
        remote_count = await registry.load_remote_tools(supervisor_url)
        if mcp_count or remote_count:
            print(f"[engine] loaded {mcp_count} MCP tools, {remote_count} remote tools", flush=True)

        # Direction 1: graceful shutdown via signal handling
        stop_event = asyncio.Event()
        _loop = asyncio.get_running_loop()

        def _request_stop():
            if not stop_event.is_set():
                print("[engine] received stop signal, initiating graceful shutdown...", flush=True)
                stop_event.set()

        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                _loop.add_signal_handler(_sig, _request_stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows or non-main thread

        print(f"[engine] worker {wid[:8]} ready (max_concurrent={max_workers})", flush=True)
        _active: set[asyncio.Task] = set()

        while not stop_event.is_set():
            try:
                # ---- reap finished tasks ----
                _done = {t for t in _active if t.done()}
                for t in _done:
                    if not t.cancelled():
                        _exc = t.exception()
                        if _exc:
                            print(f"[engine] task error: {_exc}", flush=True)
                _active -= _done

                # ---- tools hot-reload check ----
                try:
                    rr = await http.get(f"{supervisor_url}/v1/tools/reload-seq")
                    if rr.status_code == 200:
                        new_seq = int(rr.json().get("seq", 0))
                        if new_seq > _last_reload_seq:
                            _last_reload_seq = new_seq
                            count = registry.reload()
                            # [AutoC 2026-08-01] Reload MCP and remote tools after script reload.
                            # Why: Supervisor bumps reload-seq when worker tool pools change, and
                            # registry.reload() resets dynamic entries back to builtin/script tools.
                            # How: re-run MCP discovery and remote broker discovery in the same hot
                            # reload branch. Purpose: engine tool tables track live remote workers.
                            mcp_count = await registry.load_mcp_tools()
                            remote_count = await registry.load_remote_tools(supervisor_url)
                            print(f"[engine] tools reloaded ({count} local tools, {mcp_count} MCP tools, {remote_count} remote tools)", flush=True)
                except Exception:
                    pass

                # ---- poll for new task (only if below capacity) ----
                if len(_active) < max_workers:
                    nr = await http.get(f"{supervisor_url}/v1/tasks/next", params={"worker_id": wid})
                    if nr.status_code == 200:
                        item = nr.json()
                        if isinstance(item, dict) and item.get("task_id"):
                            _t = asyncio.create_task(
                                _handle_task(http, llm_http, supervisor_url, workspace_root, registry, item, wid)
                            )
                            _active.add(_t)
                            continue
            except Exception as e:
                print(f"[engine] error: {e}", flush=True)
            # Stop-aware sleep: wake immediately on stop signal
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                pass

        # ---- Direction 1: graceful shutdown — cancel active tasks and write terminal states ----
        if _active:
            print(f"[engine] shutting down, cancelling {len(_active)} active task(s)...", flush=True)
            for t in _active:
                t.cancel()
            # Wait for tasks to handle CancelledError and POST terminal state
            await asyncio.wait(_active, timeout=15.0)
        print(f"[engine] worker {wid[:8]} stopped (generation {generation_id[:8]})", flush=True)


async def _heartbeat(
    http: httpx.AsyncClient,
    sup_url: str,
    task_id: str,
    worker_id: str,
    interval: float = 60.0,
    lease_sec: float = 120.0,
) -> None:
    """Periodically renew task lease to prevent zombie reaping."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/renew_lease",
                    json={"worker_id": worker_id, "lease_sec": lease_sec},
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def _handle_task(
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    item: dict[str, Any],
    worker_id: str,
) -> None:
    task_id = str(item.get("task_id") or "").strip()
    kind = str(item.get("kind") or "").strip()
    session_id = str(item.get("session_id") or "").strip()
    session_generation = int(item.get("session_generation") or 0)

    cfg_raw = await fetch_llm_secret(http, sup_url)
    api_key, base_url, default_model = normalize_llm_secret(cfg_raw)

    _hb = asyncio.create_task(_heartbeat(http, sup_url, task_id, worker_id))
    try:
        if kind == "node":
            result = await _run_node_task(
                http=http, llm_http=llm_http, sup_url=sup_url, ws_root=ws_root,
                registry=registry, task=item, worker_id=worker_id,
                session_id=session_id, session_generation=session_generation,
                api_key=api_key, base_url=base_url,
                default_model=default_model,
            )
        elif kind == "tool":
            result = await _run_tool_task(
                http=http, sup_url=sup_url, ws_root=ws_root,
                registry=registry, task=item, worker_id=worker_id,
                session_id=session_id, session_generation=session_generation,
                task_id=task_id,
            )
        else:
            result = {"action": "fail", "node_id": "", "error": f"未知 task kind: {kind}"}
    except asyncio.CancelledError:
        # Direction 1: engine graceful shutdown — write terminal state before exiting
        print(f"[engine] task {task_id} cancelled (engine shutdown)", flush=True)
        result = {"action": "cancelled", "node_id": str(item.get("node_id") or ""), "error": "engine graceful shutdown"}
    except Exception as exc:
        import traceback as _tb
        print(f"[engine] task {task_id} crashed: {exc}", flush=True)
        _tb.print_exc()
        result = {"action": "fail", "node_id": str(item.get("node_id") or ""), "error": f"引擎内部错误: {exc}"}

    _hb.cancel()
    # 重试 complete_task POST，防止因网络/超时导致 task 永久孤立
    try:
        for _complete_attempt in range(3):
            try:
                _cr = await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/complete",
                    json={"worker_id": worker_id, "result": result},
                )
                if _cr.status_code < 500:
                    break  # 成功或客户端错误（如 404），不再重试
            except asyncio.CancelledError:
                raise  # Propagate to outer handler for final attempt
            except Exception as _ce:
                print(f"[engine] task {task_id} complete POST failed (attempt {_complete_attempt + 1}/3): {_ce}", flush=True)
                if _complete_attempt < 2:
                    await asyncio.sleep(2 ** _complete_attempt)  # 1s, 2s
        else:
            print(f"[engine] task {task_id} ORPHANED - all complete POST attempts failed", flush=True)
    except asyncio.CancelledError:
        # Shutdown interrupted the retry loop; make one final attempt
        try:
            await http.post(
                f"{sup_url}/v1/tasks/{task_id}/complete",
                json={"worker_id": worker_id, "result": result},
            )
        except Exception:
            print(f"[engine] task {task_id} ORPHANED - shutdown interrupted complete POST", flush=True)


_TASK_ACTION_FIELDS = set(TaskAction.__dataclass_fields__)


def _task_action_from_dict(data: dict[str, Any], *, node_id: str) -> TaskAction:
    """Convert a script-produced TaskAction dictionary into the runtime object."""
    # [AutoC 2026-06-09] Why: script_step intentionally returns plain dicts so it
    # can stay independent from runner internals, while the rest of runner records
    # TaskAction objects. How: filter unknown keys, supply node_id when omitted,
    # and mirror result.summary into the top-level summary. Purpose: script nodes
    # share the existing event, transcript, and completion logic without special
    # casing every later use of action.action or action.to_dict().
    raw = data if isinstance(data, dict) else {}
    kwargs = {key: value for key, value in raw.items() if key in _TASK_ACTION_FIELDS}
    kwargs.setdefault("node_id", node_id)
    result = kwargs.get("result")
    if not kwargs.get("summary") and isinstance(result, dict):
        kwargs["summary"] = str(result.get("summary") or "")
    return TaskAction(**kwargs)


def _build_task_context(input_data: dict[str, Any]) -> dict[str, Any]:
    """Build task_context from input_data, bridging supervisor flags.

    [2026-05-09] Supervisor sets '_system_task' in input_data top-level,
    but engine checks 'is_system_task' inside task_context. This function
    bridges that gap so system nodes (turn_summarizer, compactor, etc.)
    correctly suppress switch_node injection.
    """
    ctx = input_data.get("task_context") if isinstance(input_data.get("task_context"), dict) else {}
    if input_data.get("_system_task"):
        ctx["is_system_task"] = True
    return ctx


async def _run_node_task(
    *,
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    api_key: str,
    base_url: str,
    default_model: str,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    node_id = str(task.get("node_id") or "").strip()
    source_inbound_seq = task.get("source_inbound_seq")
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
    # [Fork/Merge 2026-05-12] Read parent_session_id from task input before RunContext creation.
    # Why: supervisor now runs entry tasks on a branch session while the SDK still maps channels
    # through the parent session. How: keep session_id as the runtime ConversationStore session and
    # carry parent_session_id separately for event routing. Purpose: preserve old tasks that do not
    # have this field while allowing branch tasks to report events on the user-facing session.
    parent_session_id = str(input_data.get("parent_session_id") or "").strip()

    node = load_node(ws_root, node_id)
    if node is None:
        return {"action": "fail", "node_id": node_id, "error": f"节点未找到：{node_id}"}
    # [AutoC 2026-08-01] api_key 检查移至 provider 构造之后。
    # 全局 key 为空不代表最终无 key：resolve_provider 会从
    # node.api_key / config.yaml provider section / 环境变量中取。

    rctx = RunContext(
        workspace_root=ws_root, supervisor_url=sup_url,
        session_id=session_id, worker_id=worker_id,
        http=http, llm_http=llm_http,
        api_key=api_key, base_url=base_url, default_model=default_model,
        user_text=str(input_data.get("instruction") or "").strip(),
        task_id=task_id, session_generation=session_generation,
        parent_session_id=parent_session_id,
        source_inbound_seq=int(source_inbound_seq) if source_inbound_seq is not None else None,
        task_context=_build_task_context(input_data),
    )
    # [AutoC 2026-08-10] Why: apply the durable session workspace after RunContext
    # creation. How: fetch {name, path} from supervisor and resolve into
    # workspace/workspace_name, defaulting to workspace_root when path is absent
    # or invalid. Purpose: tools and memory isolation use the same session-scoped
    # workspace each turn without re-calling set_workspace.
    _sw_sid = child_session_id or session_id
    _sw = await _fetch_session_workspace(http, sup_url, _sw_sid)
    if _sw:
        _ws_name = str(_sw.get("name") or "").strip()
        _ws_path = str(_sw.get("path") or "").strip()
        _ws_p = Path(_ws_path).resolve() if _ws_path else None
        if _ws_name:
            rctx.workspace_name = _ws_name
        if _ws_p and _ws_p.is_dir():
            rctx.workspace = _ws_p
    # Phase 1 (Session Conversation Store): 创建 ConversationStore 实例并挂载到 rctx。
    # ai_step 中的 _shadow_write 通过 getattr(ls.rctx, 'conversation_store', None) 访问。
    # RunContext 是非 frozen dataclass，允许动态添加属性。
    # 数据目录: data/conversations/{session_id}.jsonl
    _conv_store = ConversationStore(ws_root / "data" / "conversations")
    rctx.conversation_store = _conv_store  # type: ignore[attr-defined]

    history = []
    resume_data_raw = input_data.get("resume_data") if isinstance(input_data.get("resume_data"), dict) else None
    is_resume = bool(resume_data_raw)

    # [AutoC 2026-06-09] Why: resumed tasks get a fresh RunContext, losing
    # tool_call_log and total_usage from the previous execution fragment.
    # How: restore from task.result._runtime_state saved by the prior claim.
    # Purpose: the full task lifecycle shares one logical RunContext state.
    if is_resume:
        _prev_result = task.get("result") if isinstance(task.get("result"), dict) else {}
        _prev_state = _prev_result.get("_runtime_state") if isinstance(_prev_result.get("_runtime_state"), dict) else {}
        if isinstance(_prev_state.get("tool_call_log"), list):
            rctx.tool_call_log = list(_prev_state["tool_call_log"])
        if isinstance(_prev_state.get("total_usage"), dict):
            rctx.total_usage = dict(_prev_state["total_usage"])

    # ====== Child Session 隔离（Phase B）：优先走 child session 路径 ======
    # child_session_id 由 supervisor 的 task_router 在 dispatch 时设置。
    # 有此字段时，子节点的历史从自己的 JSONL 文件加载，不再使用 snapshot。
    child_session_id = str(input_data.get("child_session_id") or "").strip()
    context_mode = str(input_data.get("context_mode") or "").strip()
    fork_from = str(input_data.get("fork_from_session_id") or "").strip()
    context_ref = str(input_data.get("context_ref") or "").strip()
    use_context = bool(input_data.get("use_context", True))

    # Step 2（2026-04-16）：主节点切 ConversationStore 的 feature flag。
    # flag 开启时，主节点（无 child_session_id）从 data/conversations/{session_id}.jsonl
    # 加载 history，与 child session 同源；_persist_ctx 不写 snapshot。
    # flag 关闭时回退到旧 snapshot 机制（context_ref + load_context_snapshot）。
    _runtime_cfg_main = load_runtime_config(ws_root)
    _main_conv_enabled = bool(
        _runtime_cfg_main.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
    )

    if child_session_id:
        # ---- Child Session 模式（新路径）----
        rctx.child_session_id = child_session_id

        if context_mode == "fork" and fork_from and not is_resume:
            # fork: 先从父 session 复制历史到子 session
            _conv_store.fork(fork_from, child_session_id)

        if not is_resume:
            # 从子 session 的 JSONL 加载历史，过滤 system 消息（子节点重建自己的 system prompt）
            stored = _conv_store.load(child_session_id)
            history = [_message_to_history_dict(m, current_task_id=task_id) for m in stored if m.role != "system"]
            # [2026-05-07] 子会话恢复时先清理 finish 控制流历史。
            # 原因：system.compactor 等 child session 可能已有旧 finish tool_call/tool_result 污染。
            # 做法：在进入提示组装前去掉控制流配对，再保留旧 fake-native 尾部兼容清理。
            # 目的：父会话不会回放子任务 finish 伪工具。
            history = sanitize_control_tool_history(history)
            history = _strip_trailing_pseudo_call(history)
        # child session 模式不使用 context_ref（让 ai_step 重建 system prompt）
        context_ref = ""
    elif _main_conv_enabled:
        # ---- 主节点 ConversationStore 模式（Step 2 新路径）----
        # 主节点入口 task 在 task_store.py 已不再注入 context_ref。
        # 此处从主 session 的 JSONL 加载 history，消息源和 child session 一致。
        # _shadow_write 从 Phase 1 起就已经把 user_input/assistant/tool_result
        # 写入了 data/conversations/{session_id}.jsonl，这里的 load 能拿到完整结构。
        # resume 场景（compact_done / child_result / child_preempted）也走这里：
        # 此时 context_ref 为空，ai_step 会走 assemble_initial_messages，需要 history 不为空。
        if use_context:
            stored = _conv_store.load(session_id)
            history = [_message_to_history_dict(m, current_task_id=task_id) for m in stored if m.role != "system"]
            # 剥离尾部伪工具（finish/dispatch 等），同 child session 路径处理
            # [2026-05-07] 先执行结构化控制流清理，覆盖 true native 中非文本化的 finish 记录。
            # 原因：旧 _strip_trailing_pseudo_call 只能处理 fake-native 文本 marker。
            # 做法：sanitize_control_tool_history 处理 tool_calls/role=tool，再调用旧兼容函数。
            # 目的：主会话与子会话的历史恢复语义一致。
            history = sanitize_control_tool_history(history)
            history = _strip_trailing_pseudo_call(history)
            # 仅新 inbound（非 resume）需要 pop 尾部重复 instruction：
            # shadow_write 会在本轮再次写入 user 消息，若 JSONL 里上一条就是本轮 instruction，
            # 则要 pop 掉，避免 assemble_initial_messages 再次追加造成重复。
            if not is_resume:
                _cur_instr = str(input_data.get("instruction") or "").strip()
                if _cur_instr and history and history[-1].get("role") == "user":
                    _last_content = history[-1].get("content", "")
                    if isinstance(_last_content, str) and _last_content.strip() == _cur_instr:
                        history.pop()
        context_ref = ""  # 走 ai_step 的 assemble_initial_messages 重建系统提示
    elif context_ref and not is_resume:
        # ====== 旧路径（兼容期保留）：从 snapshot 加载 ======
        # 有上一轮 context_ref 但不是 resume：从快照提取非系统消息作为 enriched history，
        # 清空 context_ref 让 ai_step 重建新的系统提示词。
        snapshot = load_context_snapshot(ws_root, context_ref)
        if snapshot and isinstance(snapshot.get("messages"), list):
            history = [m for m in snapshot["messages"] if m.get("role") != "system"]
            # 剥离尾部的伪工具调用，改为提取 finish 的 text 作为正常 assistant 回复。
            # [2026-05-07] snapshot 兼容路径也清理结构化 finish 控制流记录。
            # 原因：旧快照可能保存 true native role=tool finish result。
            # 做法：先清理结构化控制工具，再执行旧文本 marker 清理。
            # 目的：所有历史来源都不向 LLM 回放 finish 伪工具。
            history = sanitize_control_tool_history(history)
            history = _strip_trailing_pseudo_call(history)
        elif use_context:
            # 快照加载失败，降级为 session_messages
            history = await _fetch_history(rctx)
        context_ref = ""  # 让 ai_step 走 else 分支重建系统提示词
    elif not context_ref and use_context:
        history = await _fetch_history(rctx)

    ds_info = _collect_node_info(ws_root, list(node.delegate_targets))

    runtime_cfg = load_runtime_config(ws_root)
    sw_info = _discover_switchable_nodes(ws_root, node.id)
    entry_node_id = get_entry_node_id(runtime_cfg)

    provider = None
    if node.type == "ai":
        # [AutoC 2026-06-09] Why: script nodes do not call an LLM and therefore must
        # not require provider settings or API keys. How: keep provider override,
        # config lookup, resolve_provider, and provider construction inside the AI
        # branch only. Purpose: type=script nodes can run deterministic Python in
        # runner without depending on OpenAI-compatible configuration.
        # [AutoC 2026-06-18] Why: dispatched child tasks have child_session_id
        # (their own isolated session), but task.session_id still points to the
        # parent. Querying provider_override from parent session leaks the user's
        # model choice into child nodes. How: use child_session_id for the lookup
        # when present — child sessions carry no provider_override.
        _override_sid = child_session_id or session_id
        session_provider_override = await _fetch_session_provider_override(http, sup_url, _override_sid)
        # [2026-06-07] Why: resolve_provider needs all provider sections from config.yaml
        # to look up the default model for a non-global provider (e.g. openai-responses).
        # How: load config.yaml and extract provider blocks (dicts with base_url/model/api_key).
        # Purpose: setting provider=X on a node automatically uses X's configured model.
        _config_data = load_yaml_dict(ws_root / "data" / "config.yaml")
        _provider_configs = {
            k: v for k, v in (_config_data if isinstance(_config_data, dict) else {}).items()
            if isinstance(v, dict) and any(pk in v for pk in ("base_url", "api_key", "model"))
        }
        # [AutoC 2026-06-16] Why: config.yaml provider is the global active channel,
        # not only a label in settings. How: pass it into resolve_provider as the
        # default provider for nodes that do not pin node.provider. Purpose: switching
        # ACTIVE to deepseek/openai-responses changes ordinary AI task routing.
        _active_provider = str((_config_data or {}).get("provider") or "openai").strip() if isinstance(_config_data, dict) else "openai"
        rp = resolve_provider(
            ws_root,
            node,
            default_model,
            session_override=session_provider_override,
            provider_configs=_provider_configs,
            default_provider_type=_active_provider,
        )
        provider_cfg = (runtime_cfg.get("providers") or {}).get(rp.provider_type or "openai")
        global_provider_options = provider_cfg.get("options") if isinstance(provider_cfg, dict) else {}
        if not isinstance(global_provider_options, dict):
            global_provider_options = {}
        node_provider_options = getattr(node, "provider_options", {}) or {}
        if not isinstance(node_provider_options, dict):
            node_provider_options = {}
        _po = _merge_provider_options(global_provider_options, node_provider_options)
        # [AutoC 2026-06-01] Why: runtime.yaml now supports provider-wide options, but
        # node YAML must remain the most specific layer. How: read
        # providers.<provider_type>.options and merge it with node.provider_options.
        # Purpose: providers receive global defaults unless a node overrides them.
        #
        # [provider-registry 2026-05-03] Provider 创建改为 registry 查找。
        # 原因：旧 if-elif 每新增 provider 都要改 runner；做法：按 rp.provider_type 找类并传统一 kwargs；
        # 目的：保持现有四个 provider 行为不变，同时让 providers/*.py 插件可自动接入。
        provider = _create_provider_from_registry(
            rp,
            llm_http=llm_http,
            api_key=api_key,
            base_url=base_url,
            provider_options=_po,
        )
        # [AutoC 2026-08-01] 在 provider 构造之后检查实际解析出的 key。
        # fallback 链：session_override → node.api_key → config.yaml provider section
        # → {PREFIX}_API_KEY 环境变量 → 全局 api_key。全部为空才报错。
        _actual_key = getattr(provider, '_api_key', None) or ''
        if not _actual_key:
            return {
                "action": "fail", "node_id": node_id,
                "error": f"LLM provider api_key 未配置（provider={rp.provider_type}, node={node_id}）。"
            }

    await rctx.emit_event("node_started", {
        "task_id": task_id, "node_id": node.id,
        "node_name": node.name,
        # [AutoC 2026-06-09] Why: output chain tasks (quality check etc.) should
        # not create separate child progress in the frontend. How: pass the
        # _output_chain flag from task input so event_router can merge into main.
        "_output_chain": bool(input_data.get("_output_chain")),
        "node_type": node.type,
    })

    input_attachments = input_data.get("attachments") if isinstance(input_data.get("attachments"), list) else None

    # 如果是被 switch 过来的节点，在 instruction 前注入提示
    instruction = str(input_data.get("instruction") or "").strip()
    switched_from = str(input_data.get("switched_from") or "").strip()
    if switched_from:
        instruction = f"[系统提示：你当前是通过节点切换接管此会话的。会话的默认入口节点是 {switched_from}。用户可以要求切回默认节点，你可以使用 switch_node 工具（target 传空字符串）恢复默认。]\n\n{instruction}"

    # Step 2（2026-04-16）：主节点 ConversationStore 模式下的 resume 去重保护。
    # resume 时（compact_done / child_result / child_preempted 等），JSONL 已包含原
    # instruction 及其后续全部对话；assemble_initial_messages 末尾再追加一次 instruction
    # 会破坏对话顺序并复发图片附件。旧 snapshot 路径下此 case 通过 snapshot.messages
    # 直接回放解决，新路径下需要在这里把 instruction 和 attachments 清空。
    # message_assembly.py 同步改为 instruction 为空且无 attachments 时不追加末尾消息。
    if _main_conv_enabled and not child_session_id and is_resume:
        instruction = ""
        input_attachments = None

    # Phase 1 补丁：将 user_input (instruction) 影子写入 ConversationStore。
    # 此前 _shadow_write 只写了 assistant 和 tool_result，用户指令完全缺失，
    # 导致子节点 accumulate 恢复时 JSONL 里缺少用户输入、上下文不完整。
    # [Fork/Merge 2026-05-12] 写入目标 session：优先 child_session_id，否则 rctx 运行 session。
    # Why: session_id may be an entry branch rather than the user-facing parent. How: keep the
    # existing child-session priority and otherwise write to the branch/runtime session. Purpose:
    # branch histories remain complete until supervisor merge.
    #
    # Step 2（2026-04-16）：resume 场景（compact_done / child_result / child_failed 等）
    # 下 caller task 被 supervisor 重新唤醒，input.instruction 仍是原先的用户指令，
    # 此指令已在首次进入时写过 JSONL，这里不应再写一次。
    # 只在非 resume 场景追加 user_input 消息。
    _inbound_message_type = str(input_data.get("inbound_message_type") or "").strip()
    _inbound_summary = str(input_data.get("inbound_summary") or "").strip()
    _inbound_child_session_id = str(input_data.get("inbound_child_session_id") or "").strip()
    _inbound_child_task_id = str(
        input_data.get("inbound_child_task_id")
        or input_data.get("inbound_task_id")
        or ""
    ).strip()
    _inbound_node_id = str(
        input_data.get("inbound_child_node_id")
        or input_data.get("inbound_node_id")
        or ""
    ).strip()
    _inbound_caller_node_id = str(input_data.get("inbound_caller_node_id") or "").strip()
    # [AutoC 2026-06-04] Why: dispatch_result.text can be empty when the child only
    # returns a summary, but the callback still has semantic content. How: allow the
    # shadow write when dispatch_result has inbound_summary. Purpose: ConversationStore
    # keeps a structured callback row even without raw result text.
    _should_write_user_input = bool(
        instruction
        or input_attachments
        or (_inbound_message_type == "dispatch_result" and _inbound_summary)
    )
    if _should_write_user_input and _conv_store and not is_resume:
        from datetime import datetime, timezone
        _target_sid = child_session_id or session_id
        # [AutoC 2026-06-01] Why: persisting only plain instruction text lost
        # image attachments before the next step could reload ConversationStore.
        # How: when attachments are present, build the same multimodal content
        # that message assembly sends to the provider; otherwise keep the legacy
        # string content. Purpose: user image input remains available inside the
        # current task and can still be stripped when another task reloads it.
        if input_attachments:
            _user_content = build_multimodal_content(instruction, input_attachments, workspace_root=ws_root)
        else:
            _user_content = instruction
        # [AutoC 2026-06-04] Why: supervisor-injected dispatch callbacks now carry
        # child_* metadata and summary separately from raw text. How: persist only the
        # selected structured fields in Message.meta while leaving content as the raw
        # child result. Purpose: refreshed history can render callbacks without storing
        # the LLM-only English prefix or any backend-localized prose.
        _inbound_meta: dict[str, Any] = {}
        if _inbound_summary:
            _inbound_meta["summary"] = _inbound_summary
        if _inbound_child_session_id:
            _inbound_meta["child_session_id"] = _inbound_child_session_id
        if _inbound_child_task_id:
            _inbound_meta["child_task_id"] = _inbound_child_task_id
        if _inbound_node_id:
            _inbound_meta["child_node_id"] = _inbound_node_id
        if _inbound_caller_node_id:
            _inbound_meta["caller_node_id"] = _inbound_caller_node_id
        if input_attachments:
            # [AutoC 2026-06-17] Why: ConversationStore content keeps provider-style
            # multimodal blocks, but the web history API needs the original attachment
            # metadata for thumbnails and file links. How: persist the normalized
            # attachment list in message meta beside the content. Purpose: refreshing
            # a web session keeps the same inbound attachment cards as live events.
            _inbound_meta["attachments"] = list(input_attachments)
        _user_msg = Message(
            id=str(uuid.uuid4()),
            role="user",
            content=_user_content,
            message_type=_inbound_message_type or MessageType.USER_INPUT,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=_inbound_meta,
            source_node_id=node.id,
            source_task_id=task_id,
        )
        _conv_store.append(_target_sid, _user_msg)

    if _inbound_message_type == "dispatch_result":
        # [AutoC 2026-06-04] Why: the model still needs context that this inbound came
        # from an async child task, but that presentation must not be persisted in
        # ConversationStore. How: prepend an English, LLM-only wrapper after the raw
        # callback row has been stored. Purpose: storage remains pure while the caller
        # node receives an actionable instruction.
        _prefix_parts = [f"[Async task completed] Node {_inbound_node_id or 'unknown'} finished."]
        if _inbound_summary:
            _prefix_parts.append(f"Summary: {_inbound_summary}")
        _prefix_parts.append(f"Result:\n{instruction}")
        instruction = "\n".join(_prefix_parts)

    # Phase 0/1: Signal System — 初始化信号总线并安装 JSONL 桥接。
    # get_bus() 返回全局单例，install_event_bridge 是幂等的（多次调用只注册一次）。
    # 读取 runtime.yaml 中的 signals.enabled 配置决定是否启用。
    _signals_cfg = runtime_cfg.get("engine", {}).get("signals", {})
    _sig_bus = get_bus()
    _sig_bus.enabled = bool(_signals_cfg.get("enabled", True))
    _bridge_patterns = _signals_cfg.get("bridge_patterns")
    install_event_bridge(_sig_bus, patterns=_bridge_patterns)

    if node.type == "script":
        # [AutoC 2026-06-09] Why: script nodes are deterministic runner-level logic,
        # not model calls. How: execute the inline Python through script_step and
        # adapt its TaskAction dictionary into the object used by downstream runner
        # bookkeeping. Purpose: finish and ask results follow existing supervisor
        # routing while avoiding run_ai_node entirely.
        from .script_step import run_script_node

        script_action = await run_script_node(
            rctx=rctx,
            node=node,
            text=instruction,
            attachments=input_attachments or [],
            input_data=input_data,
        )
        action = _task_action_from_dict(script_action, node_id=node.id)
    else:
        action = await run_ai_node(
            rctx=rctx, provider=provider, registry=registry, node=node,
            instruction=instruction,
            history=history, run_id=task_id, context_ref=context_ref,
            resume_data=resume_data_raw,
            downstream_info=ds_info,
            switch_info=sw_info,
            streaming=bool(get_bool(runtime_cfg, "engine.streaming", False)),
            attachments=input_attachments,
        )

    # Child Session 隔离（Phase B）：将 child_session_id 写入 task result，
    # 供 supervisor 侧 _route_completed_task_locked 和调试/监控使用。
    if child_session_id:
        action.child_session_id = child_session_id

    await rctx.emit_event("node_completed", {
        "task_id": task_id, "node_id": node.id, "node_name": node.name,
        "action": action.action, "summary": action.summary,
        "source_inbound_seq": source_inbound_seq,
        "_output_chain": bool(input_data.get("_output_chain")),
        "node_type": node.type,
    })

    # P0 Task 内核化：写入 TaskRecord（轮摘要改为 supervisor 后置触发独立节点生成，
    # 不再在 runner 中做阻塞式 LLM 调用。见 supervisor/task_router.py 的
    # _maybe_trigger_turn_summary_locked）
    _tool_call_count = len(rctx.tool_call_log)
    _total_tokens = (rctx.total_usage.get("total_tokens") or 0) if rctx.total_usage else 0

    try:
        from engine.task_record import TaskRecord, write_task_record
        _record = TaskRecord(
            task_id=task_id,
            session_id=session_id,
            node_id=node.id,
            action=action.action,
            first_message_id=rctx.first_shadow_message_id,
            last_message_id=rctx.last_shadow_message_id,
            step_count=rctx.completed_steps,
            tool_call_count=_tool_call_count,
            token_usage=dict(rctx.total_usage) if rctx.total_usage else {},
            summary=action.summary or "",
            error=action.error or "",
            child_session_id=child_session_id,
        )
        write_task_record(ws_root, _record)
        # [AutoC 2026-05-30] Why: persistent 子节点的 snip compact 从 child_session_id
        # 加载 TaskRecord，但上面只写到了父 session 的 transcript JSONL。
        # How: 额外写一份到 child session 的 transcript。
        # Purpose: 轮摘要回写和 snip compact 都能通过 child_session_id 找到记录。
        if child_session_id and child_session_id != session_id:
            _child_record = TaskRecord(
                task_id=task_id,
                session_id=child_session_id,
                node_id=node.id,
                action=action.action,
                first_message_id=rctx.first_shadow_message_id,
                last_message_id=rctx.last_shadow_message_id,
                step_count=rctx.completed_steps,
                tool_call_count=_tool_call_count,
                token_usage=dict(rctx.total_usage) if rctx.total_usage else {},
                summary=action.summary or "",
                error=action.error or "",
                child_session_id=child_session_id,
            )
            write_task_record(ws_root, _child_record)
    except Exception as _tr_err:
        print(f"[engine] Failed to write task record: {_tr_err}", flush=True)

    _result = action.to_dict()
    # [AutoC 2026-06-09] Why: when a task is suspended (output chain) and
    # later resumed, the engine creates a fresh RunContext with empty
    # tool_call_log/total_usage. How: persist runtime state in the result
    # so the next claim can restore it. Purpose: plugins like
    # no_tool_finish_guard see the full task history, not just the
    # current claim's fragment.
    _result["_runtime_state"] = {
        "tool_call_log": list(rctx.tool_call_log),
        "total_usage": dict(rctx.total_usage) if rctx.total_usage else {},
    }
    if getattr(rctx, "current_llm_request_id", "") and not _result.get("llm_request_id"):
        # [AutoC 2026-06-04] Why: some TaskAction producers are hooks rather than
        # pseudo handlers, but they still end the current provider request. How: fill
        # the task result from RunContext when the action did not set it itself.
        # Purpose: supervisor outbound events remain request-scoped for every finish path.
        _result["llm_request_id"] = rctx.current_llm_request_id
    # 轮摘要节点化：传递 _tool_call_count 和 _total_tokens 给 supervisor 做门控判断
    _result["_tool_call_count"] = _tool_call_count
    _result["_total_tokens"] = _total_tokens
    # P3 记忆提取互斥：传递本轮使用的工具名列表，供 supervisor 判断是否跳过提取
    if rctx.tool_call_log:
        _result["_tool_names"] = list(set(tc["name"] for tc in rctx.tool_call_log))
    return _result


async def _run_tool_task(
    *,
    http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    task_id: str,
) -> dict[str, Any]:
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
    tool_name = str(task.get("tool_name") or "").strip() or str(input_data.get("tool_name") or "").strip()
    arguments = dict(input_data.get("arguments") or {})

    # [Fork/Merge 2026-05-17] Why: standalone tool tasks can be spawned under
    # an entry branch but still need user-visible supervisor side effects to route
    # to the parent session. How: read parent_session_id propagated in task input.
    # Purpose: tool-task approvals, progress, and session-scoped built-ins avoid
    # temporary branch sessions.
    parent_session_id = str(input_data.get("parent_session_id") or "").strip()
    _task_ctx = input_data.get("task_context") if isinstance(input_data.get("task_context"), dict) else {}
    kctx = ToolContext(
        supervisor_url=sup_url, session_id=session_id, run_id=task_id,
        worker_id=worker_id, workspace_root=ws_root, http=http,
        registry=registry, task_id=task_id,
        session_generation=session_generation,
        parent_session_id=parent_session_id,
        conversation_key=str(_task_ctx.get("conversation_key", "")).strip(),
        approval_poll_interval_sec=0.5,
        # [AutoC 2026-05-31] Why: direct standalone tool tasks can still trigger
        # policy approvals. How: use the task id as the best available tool_call_id
        # and leave node_id as the tool name. Purpose: approval events remain
        # traceable even outside native LLM tool-call execution.
        tool_call_id=str(input_data.get("tool_call_id") or task_id or ""),
        node_id=tool_name,
    )

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] 开始执行 {tool_name}",
        "task_id": task_id, "tool_name": tool_name,
    })

    result = await registry.execute(name=tool_name, arguments=arguments, ctx=kctx)
    if isinstance(result, dict) and result.get("cancelled"):
        await kctx.emit_event("cancel_acknowledged", {"task_id": task_id, "tool_name": tool_name})
        return {
            "action": "cancelled",
            "node_id": tool_name,
            "summary": "任务已取消",
        }

    # [AutoC 2026-05-31] Why: standalone tool tasks must collect files from both
    # the new data.attachments field and the temporary top-level attachments mirror.
    # How: prefer the nested list and fall back to the legacy top-level list.
    # Purpose: keep generated media attached for direct tool-node execution.
    if isinstance(result, dict):
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        tool_attachments = result_data.get("attachments") if isinstance(result_data.get("attachments"), list) else result.get("attachments")
        if not isinstance(tool_attachments, list):
            tool_attachments = []
    else:
        tool_attachments = []

    # [summary-args 2026-05-19] Why: standalone tool tasks emit the same compact
    # handoff_progress row as AI-driven tools. How: pass the task arguments into
    # summarize_result(). Purpose: make tool-node logs include the command, query,
    # or other key parameters without changing the surrounding message format.
    summary = summarize_result(tool_name, result, args=arguments)
    # [AutoC 2026-05-31] Why: standalone tool execution should preserve the same
    # result_format metadata path used by AI-driven tool calls. How: fetch the
    # registry spec for the executed tool and pass it to result_to_raw(). Purpose:
    # keep result rendering consistent across runner entry points.
    tool_spec = registry.get_spec(tool_name)
    fmt, raw = result_to_raw(tool_name, result, tool_spec=tool_spec)
    # [2026-04-17] 移除工具结果截断机制：不再对 raw 做 max_inline 截断和 artifact 写入，
    # 直接将完整结果传递给下游，避免信息丢失。
    raw_inline = raw

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] {tool_name}: {summary}",
        "task_id": task_id, "tool_name": tool_name,
    })

    return {
        "action": "finish",
        "node_id": tool_name,
        "summary": summary,
        "result": {
            "summary": summary,
            "text": raw_inline,
            "attachments": tool_attachments,
            "format": fmt,
            "truncated": False,  # [2026-04-17] 截断机制已移除，保留字段兼容下游
            "ref": "",
            # 旧字段保留供 tool_trace 格式化用
            "raw_format": fmt,
            "raw_inline": raw_inline,
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
