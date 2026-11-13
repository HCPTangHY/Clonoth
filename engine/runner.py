from __future__ import annotations

import asyncio
import inspect
import json
import logging
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
from .restart_recovery import current_context, cancelled_task_result, runtime_state, restore_runtime_state
from .model import _current_sticky_sid_var, resolve_provider, wrap_http_with_sticky_session
from .context_store import load_context_snapshot
from providers.tool_format import sanitize_control_tool_history

from .node import Node, load_node, node_file_candidates
from engine.faces import nodes as _plugin_nodes
from .protocol import TaskAction
# [2026-04-17] write_artifact 移除：截断机制已废弃
from .tool_step import result_to_raw
# Phase 1 (Session Conversation Store): 导入 ConversationStore 用于影子写入，
# 在每个 node task 执行时实例化并挂载到 RunContext，供 ai_step 影子写入消息。
from .conversation_store import ConversationStore, Message, MessageType, is_dynamic_context
# Phase 0/1: Signal System — 导入信号总线初始化和桥接函数。
# 在 _run_node_task 中初始化 bus 并安装 EventLog 桥接，使 LLM 调用信号
# 自动转发到 data/signals.jsonl 供监控使用。
from engine.signals import get_bus
from engine.signals.bridge import install_event_bridge


logger = logging.getLogger(__name__)


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
    defaults = getattr(rp, "provider_defaults", None) or {}
    effective_options = _merge_provider_options(defaults, provider_options)
    effective_options = _merge_provider_options(effective_options, getattr(rp, "provider_options", None) or {})
    sticky_config = effective_options.get("sticky_session")
    if sticky_config is None:
        sticky_config = defaults.get("sticky_session")
    sticky_id = str(effective_options.get("_sticky_session_id") or _current_sticky_sid_var.get() or "").strip()
    # Preserve production's model-scoped routing for these requests.
    if sticky_id.startswith("child_") or rp.model == "gemini-3.8-flash":
        sticky_id = f"node:{rp.model}"
    return {
        "http": wrap_http_with_sticky_session(llm_http, sticky_config, sticky_id),
        "api_key": resolved_api_key,
        "base_url": resolved_base_url,
        "model": rp.model,
        "provider_options": effective_options,
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
    # 按构造签名传入已支持的参数，包括内置渠道的 provider_options。
    # 声明 **kwargs 的扩展接收完整参数，不要求所有插件采用相同构造函数。
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
        # This also validates global and session overrides that bypass node parsing.
        raise ValueError(f"Unknown provider '{requested_name}'. Available: {provider_registry.list()}")
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
    if _plugin_nodes.declared_node(node_id) is not None:
        source = _plugin_nodes.declared_node_path(node_id)
        if source:
            try:
                return Path(source), int(os.stat(source).st_mtime_ns)
            except OSError:
                pass
        return None, -1
    # Share load_node's file precedence; path and nanosecond mtime invalidate
    # the cache when an override appears, changes, or is removed.
    for path in node_file_candidates(workspace_root, node_id):
        try:
            if path.is_file():
                return path, int(path.stat().st_mtime_ns)
        except OSError:
            continue
    return None, -1


def _load_cached_node_info(workspace_root: Path, node_id: str) -> dict[str, str]:
    """Load id/name/description for one node YAML with an mtime cache."""
    # [AutoC 2026-08-30] Plugin-declared nodes override file sources (same
    # precedence as load_node). Why: declarations have no file path or mtime.
    # How: cache under a "plugin://" key with the face's mutation counter as
    # the invalidation token. Purpose: register/unload/reload invalidate the
    # cache immediately, exactly like a file mtime change.
    decl = _plugin_nodes.declared_node(node_id)
    if decl is not None:
        cache_key = f"plugin://{node_id}"
        version = _plugin_nodes.registry_version()
        cached = _NODE_INFO_CACHE.get(cache_key)
        if cached is not None and cached[0] == version:
            return cached[1]
        info = _fallback_node_info(node_id)
        kind = str(decl.get("kind") or "node").strip()
        node_type = str(decl.get("type") or "ai").strip().lower()
        if kind == "node" and node_type in {"ai", "tool", "script"}:
            name = str(decl.get("name") or node_id).strip() or node_id
            description = str(decl.get("description") or "").strip() or name
            info = {"id": node_id, "name": name, "description": description}
        _NODE_INFO_CACHE[cache_key] = (version, info)
        return info
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
    # System nodes are plugin declarations, not switchable user entry points.
    nodes_dirs = [workspace_root / "config" / "nodes"]
    # [AutoC 2026-08-30] entries keyed by id so plugin declarations can replace
    # file-sourced nodes wholesale (override semantics); targets are recomputed
    # from the final set because an override may change delegate_targets.
    entries: dict[str, dict[str, Any]] = {}
    for nodes_dir in nodes_dirs:
        if not nodes_dir.is_dir():
            continue
        for f in sorted(nodes_dir.iterdir()):
            if f.suffix not in (".yaml", ".yml") or f.name.startswith("_"):
                continue
            nid = f.stem.removesuffix(".local")
            try:
                n = load_node(workspace_root, nid)
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping invalid switchable node %r: %s", nid, exc)
                continue
            if n is None or n.type != "ai" or n.id in entries:
                continue
            controls = n.control_tools
            can_switch = controls in (None, "all") or (isinstance(controls, list) and "switch_node" in controls)
            entries[n.id] = {
                "id": n.id,
                "name": n.name,
                "description": n.description or n.name,
                "targets": set(n.delegate_targets),
                "switchable": not n.id.startswith("system.") and n.extra.get("switchable", True) is not False and can_switch,
            }
    # 插件声明节点并入：同 id 覆盖文件来源（Paradox 语义）。
    for decl_entry in _plugin_nodes.iter_declared_nodes():
        data = decl_entry["data"]
        if str(data.get("kind") or "node").strip() != "node":
            continue
        if str(data.get("type") or "ai").strip().lower() != "ai":
            continue
        nid = decl_entry["id"]
        name = str(data.get("name") or nid).strip() or nid
        raw_targets = data.get("delegate_targets")
        targets = {
            str(x).strip()
            for x in (raw_targets if isinstance(raw_targets, list) else [])
            if isinstance(x, str) and x.strip()
        }
        controls = data.get("control_tools", "all")
        can_switch = controls in (None, "all") or (isinstance(controls, list) and "switch_node" in controls)
        entries[nid] = {
            "id": nid,
            "name": name,
            "description": str(data.get("description") or "").strip() or name,
            "targets": targets,
            "switchable": not nid.startswith("system.") and data.get("switchable", True) is not False and can_switch,
        }
    all_nodes: list[dict[str, Any]] = [
        {"id": e["id"], "name": e["name"], "description": e["description"]}
        for e in entries.values() if e["switchable"]
    ]
    all_targets: set[str] = set()
    for e in entries.values():
        all_targets.update(e["targets"])
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


_ATTACHED_IMAGES_MARK = "\n[Attached images: "


def _wrap_dispatch_result_text(text: str, *, message_type: str, source: str = "", task_id: str = "") -> str:
    """Use identical callback framing on fresh input and history replay."""
    meta: dict[str, Any] = {}
    if source:
        meta["source"] = source
    if task_id:
        meta["task_id"] = task_id
    kind = "async_tool" if message_type == "async_tool_result" else "dispatch"
    return (
        f'<dispatch_result kind="{kind}">\n'
        + json.dumps(meta, ensure_ascii=False)
        + "\n" + text + "\n</dispatch_result>"
    )


def _wrap_stored_inbound_callback(content: Any, *, message_type: str, meta: dict[str, Any]) -> Any:
    """Frame raw callback rows without modifying stored text or image blocks."""
    if message_type not in ("dispatch_result", "async_tool_result"):
        return content
    source = str(meta.get("child_node_id") or "").strip()
    task_id = str(meta.get("child_task_id") or "").strip()
    if isinstance(content, str):
        if content.lstrip().startswith("<dispatch_result"):
            return content
        return _wrap_dispatch_result_text(content, message_type=message_type, source=source, task_id=task_id)
    if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "text":
        text = str(content[0].get("text") or "")
        if text.lstrip().startswith("<dispatch_result"):
            return content
        # ponytail: attachment annotation is the final bracketed suffix; use explicit
        # attachment metadata if arbitrary callback bodies need to mimic that suffix.
        cut = text.rfind(_ATTACHED_IMAGES_MARK)
        body, suffix = (text[:cut], text[cut:]) if cut >= 0 and text.endswith("]") else (text, "")
        first = dict(content[0])
        first["text"] = _wrap_dispatch_result_text(
            body, message_type=message_type, source=source, task_id=task_id,
        ) + suffix
        return [first, *content[1:]]
    return content


def _message_to_history_dict(msg: Message, *, current_task_id: str = "") -> dict[str, Any]:
    """将 ConversationStore 的 Message 转为 runner 期望的 history dict 格式。

    Child Session 隔离（Phase B）：从 child session JSONL 加载的 Message 对象
    需要转为与 snapshot messages 相同的 dict 格式，供 ai_step 的消息组装使用。
    """
    # [AutoC 2026-06-18] Why: output_rejected needs msg.id to call
    # ConversationStore.update_message and persist the reject result.
    # How: carry id through the history dict. Purpose: ai_step can
    # locate and update the original finish "ok" row in JSONL.
    d: dict[str, Any] = {
        "role": msg.role,
        "content": _wrap_stored_inbound_callback(
            msg.content, message_type=msg.message_type,
            meta=msg.meta if isinstance(msg.meta, dict) else {},
        ),
    }
    if is_dynamic_context(msg):
        d["_dynamic"] = True
        if (msg.meta or {}).get("_workspace_context"):
            d["_workspace_context"] = True
    if msg.id:
        d["id"] = msg.id
    # Preserve multimodal history across tasks: stripping old images changes
    # the request prefix and prevents the provider from reusing its cache.
    _meta = dict(msg.meta) if isinstance(msg.meta, dict) else {}
    if msg.message_type:
        d["message_type"] = msg.message_type
        _meta.setdefault("message_type", msg.message_type)
    if msg.created_at and not _meta.get("timestamp"):
        _meta["timestamp"] = msg.created_at
    # Preserve task attribution for segment-based compaction.
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
                        entry: dict[str, Any] = {"role": str(m.get("role")), "content": content}
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

        remote_count = await registry.load_remote_tools(supervisor_url)
        if remote_count:
            print(f"[engine] loaded {remote_count} remote tools", flush=True)
        # [AutoC 2026-08-27] MCP 工具预载移除：发现职责归 engine/builtin/mcp 插件，
        # 其后台桥在首个 AI 任务启动时做首次发现（首个任务可能赶不上，第二个起可见）。

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
        from .runtime_metrics import WorkerMetrics
        metrics = WorkerMetrics(workspace_root, wid, generation_id, runtime_cfg)

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
                metrics.reaped += len(_done)
                _done.clear()
                metrics.sample(_active, registry)

                # ---- tools hot-reload check ----
                try:
                    rr = await http.get(f"{supervisor_url}/v1/tools/reload-seq")
                    if rr.status_code == 200:
                        new_seq = int(rr.json().get("seq", 0))
                        if new_seq > _last_reload_seq:
                            _last_reload_seq = new_seq
                            count = registry.reload()
                            # [AutoC 2026-08-01] Reload remote tools after script reload.
                            # Why: Supervisor bumps reload-seq when worker tool pools change.
                            # How: re-run remote broker discovery in the same hot reload branch;
                            # MCP tools live in the reload snapshots (registered via
                            # register_builtin_tool by the mcp builtin plugin) and survive
                            # reload without re-discovery. Purpose: engine tool tables track
                            # live remote workers.
                            remote_count = await registry.load_remote_tools(supervisor_url)
                            print(f"[engine] tools reloaded ({count} local tools, {remote_count} remote tools)", flush=True)
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
                            metrics.claimed += 1
                            continue

                # [AutoC 2026-09-02] 异步工具取消消费：supervisor 的待消费队列
                # 每秒随轮询拉一次，命中的 async_id 在本地登记表里标取消意图；
                # execute_command 的 0.2s 循环与投递路径都会感知。
                try:
                    from engine.inference.async_tools import mark_async_tool_cancelled
                    cr = await http.get(
                        f"{supervisor_url}/v1/async_tools/pending_cancels",
                        params={"worker_id": wid},
                        timeout=2.0,
                    )
                    if cr.status_code == 200:
                        for _aid in (cr.json().get("async_ids") or []):
                            mark_async_tool_cancelled(str(_aid))
                            print(f"[engine] async tool {_aid} cancel requested", flush=True)
                except Exception:
                    pass
            except asyncio.CancelledError:
                stop_event.set()
            except Exception as e:
                print(f"[engine] error: {e}", flush=True)
            # Stop-aware sleep: wake immediately on stop signal
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                stop_event.set()

        # Follow production's bounded task cancellation; release diagnostics last.
        try:
            # Background tools must settle cancellation while the HTTP client still
            # exists, not during asyncio.run's final teardown after it has closed.
            from engine.inference.async_tools import _async_tool_tasks, mark_async_tool_cancelled
            background = set()
            for aid, entry in list(_async_tool_tasks.items()):
                task = entry.get('_task') if isinstance(entry, dict) else None
                if isinstance(task, asyncio.Task) and not task.done():
                    background.add(task)
                    mark_async_tool_cancelled(aid)
            if _active or background:
                print(f"[engine] shutting down, cancelling {len(_active)} active task(s)...", flush=True)
                for task in _active:
                    task.cancel()
                await asyncio.wait(_active | background, timeout=15.0)
        finally:
            metrics.close()
        print(f"[engine] worker {wid[:8]} stopped (generation {generation_id[:8]})", flush=True)


async def _heartbeat(
    http: httpx.AsyncClient,
    sup_url: str,
    task_id: str,
    worker_id: str,
    interval: float = 60.0,
    lease_sec: float = 120.0,
    execution_id: str = "",
) -> None:
    """Periodically renew task lease to prevent zombie reaping."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/renew_lease",
                    json={"worker_id": worker_id, "lease_sec": lease_sec,
                          **({"execution_id": execution_id} if execution_id else {})},
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
    execution_id = str(item.get("execution_id") or "")
    kind = str(item.get("kind") or "").strip()
    session_id = str(item.get("session_id") or "").strip()
    _hb: asyncio.Task | None = None
    sticky_token = _current_sticky_sid_var.set("")
    restart_token = current_context.set(None)
    try:
        session_generation = int(item.get("session_generation") or 0)
        _hb = asyncio.create_task(_heartbeat(http, sup_url, task_id, worker_id, execution_id=execution_id))
        cfg_raw = await fetch_llm_secret(http, sup_url)
        api_key, base_url, default_model = normalize_llm_secret(cfg_raw)
        if kind == "tool" and (item.get("input") or {}).get("_restart_cancelled_tool"):
            result = {"action": "cancelled", "interruption_reason": "service_restart",
                      "error": "Tool interrupted by service restart; external effects may already have occurred."}
        elif kind == "node":
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
        result = cancelled_task_result(current_context.get(), str(item.get("node_id") or ""))
    except Exception as exc:
        import traceback as _tb
        print(f"[engine] task {task_id} crashed: {exc}", flush=True)
        _tb.print_exc()
        result = {"action": "fail", "node_id": str(item.get("node_id") or ""), "error": f"引擎内部错误: {exc}"}
    finally:
        _current_sticky_sid_var.reset(sticky_token)
        current_context.reset(restart_token)

    if _hb is not None:
        _hb.cancel()
        await asyncio.gather(_hb, return_exceptions=True)
    # 重试 complete_task POST，防止因网络/超时导致 task 永久孤立
    try:
        for _complete_attempt in range(3):
            try:
                _cr = await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/complete",
                    json={"worker_id": worker_id, "result": result,
                          **({"execution_id": execution_id} if execution_id else {})},
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
                json={"worker_id": worker_id, "result": result,
                      **({"execution_id": execution_id} if execution_id else {})},
            )
        except Exception:
            print(f"[engine] task {task_id} ORPHANED - shutdown interrupted complete POST", flush=True)


_TASK_ACTION_FIELDS = set(TaskAction.__dataclass_fields__)


def _task_action_from_dict(data: dict[str, Any], *, node_id: str) -> TaskAction:
    """Convert a script-produced TaskAction dictionary into the runtime object."""
    # [AutoC 2026-06-09] Why: script_step intentionally returns plain dicts so it
    # can stay independent from runner internals, while the rest of runner records
    # TaskAction objects. How: filter unknown keys and supply node_id when omitted.
    # Purpose: script nodes share the existing event, transcript, and completion
    # logic without special casing every later use of action.action or to_dict().
    raw = data if isinstance(data, dict) else {}
    kwargs = {key: value for key, value in raw.items() if key in _TASK_ACTION_FIELDS}
    kwargs.setdefault("node_id", node_id)
    return TaskAction(**kwargs)


def _build_task_context(input_data: dict[str, Any]) -> dict[str, Any]:
    """Build task_context from input_data, bridging supervisor flags.

    [2026-05-09] Supervisor sets '_system_task' in input_data top-level,
    but engine checks 'is_system_task' inside task_context. This function
    bridges that gap so system nodes (turn_summarizer, compactor, etc.)
    correctly suppress switch_node injection.
    """
    ctx = dict(input_data["task_context"]) if isinstance(input_data.get("task_context"), dict) else {}
    if input_data.get("_system_task"):
        ctx["is_system_task"] = True
    # [AutoC 2026-08-28] Forward _memory_route from input_data to task_context
    # so the dream_router hook can read it from rctx.task_context at runtime.
    _mr = input_data.get("_memory_route")
    if isinstance(_mr, dict) and _mr:
        ctx["_memory_route"] = _mr
    # [AutoC 2026-08-28] Forward _memory_route_ns for memory_extractor tasks.
    _mrns = input_data.get("_memory_route_ns")
    if isinstance(_mrns, str) and _mrns:
        ctx["_memory_route_ns"] = _mrns
    # [fix 2026-09-04] Forward _memory_route_ws alongside: dream preprocessing
    # carries the session's workspace name so writes land in the @ws directory.
    _mrws = input_data.get("_memory_route_ws")
    if isinstance(_mrws, str) and _mrws:
        ctx["_memory_route_ws"] = _mrws
    origin = input_data.get("_dispatch_origin")
    if isinstance(origin, dict) and origin:
        ctx["dispatch_origin"] = dict(origin)
    if input_data.get("_output_chain"):
        ctx["_output_chain"] = True
    if input_data.get("caller_task_id"):
        ctx["caller_task_id"] = str(input_data["caller_task_id"])
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
    if node.type == "script" and (input_data.get("resume_data") or {}).get("type") == "restart_interrupted":
        # A script has no inference checkpoint; never replay its external effects.
        return {"action": "cancelled", "node_id": node_id, "interruption_reason": "service_restart",
                "error": "Script interrupted by service restart; external effects may already have occurred."}
    execution_options = input_data.get("_execution_options") or {}
    if execution_options:
        if not input_data.get("_system_task"):
            return {"action": "fail", "error": "execution options require a system task"}
        from dataclasses import replace
        from .node import ToolAccess, SkillAccess, MemoryAccess
        if not isinstance(execution_options, dict) or set(execution_options) - {"provider", "model", "max_steps"}:
            return {"action": "fail", "error": "invalid system execution options"}
        node = replace(
            node, provider=str(execution_options.get("provider") or node.provider),
            model=str(execution_options.get("model") or node.model),
            tool_access=ToolAccess(mode="none"), skill_access=SkillAccess(mode="none"),
            memory_access=MemoryAccess(mode="none"), control_tools="none", delegate_targets=[],
            output_mode="hybrid", output="", persistent=False,
        )
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
        execution_id=str(task.get("execution_id") or ""),
        parent_session_id=parent_session_id,
        source_inbound_seq=int(source_inbound_seq) if source_inbound_seq is not None else None,
        task_context=_build_task_context(input_data),
    )
    current_context.set(rctx)
    rctx._resume_execution_id = str(task.get("completed_execution_id") or "")
    if execution_options:
        rctx.task_context["execution_max_steps"] = max(1, min(10, int(execution_options.get("max_steps", 1))))
    if task.get("caller_task_id"):
        rctx.task_context["caller_task_id"] = str(task["caller_task_id"])
    # [AutoC 2026-08-10] Why: apply the durable session workspace after RunContext
    # creation. How: fetch {name, path} from supervisor and resolve into
    # workspace/workspace_name, defaulting to workspace_root when path is absent
    # or invalid. Purpose: tools and memory isolation use the same session-scoped
    # workspace each turn without re-calling set_workspace.
    _sw_sid = str(input_data.get("child_session_id") or "").strip() or session_id
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
    if isinstance(resume_data_raw, dict) and resume_data_raw.get('type') == 'restart_interrupted':
        initial_sid = str(input_data.get('child_session_id') or '') or session_id
        has_initial_input = any(m.source_task_id == task_id and m.role == 'user'
                                and m.message_type in ('', MessageType.USER_INPUT, 'dispatch_result', 'async_tool_result')
                                and not m.meta.get('preempt') for m in _conv_store.load(initial_sid))
        if not has_initial_input:
            # A claim cancelled during provider/workspace setup has not admitted
            # its input. Treat it as the original first run rather than dropping it.
            resume_data_raw = None
    is_resume = bool(resume_data_raw)
    rctx._restart_input_persisted = is_resume

    # [AutoC 2026-06-09] Why: resumed tasks get a fresh RunContext, losing
    # tool_call_log and total_usage from the previous execution fragment.
    # How: restore from task.result._runtime_state saved by the prior claim.
    # Purpose: the full task lifecycle shares one logical RunContext state.
    if is_resume:
        _prev_result = task.get("result") if isinstance(task.get("result"), dict) else {}
        _prev_state = _prev_result.get("_runtime_state") if isinstance(_prev_result.get("_runtime_state"), dict) else {}
        restore_runtime_state(rctx, _prev_state)

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

        if context_mode == "fork" and fork_from and not is_resume and not _conv_store.load(child_session_id):
            # fork: 先从父 session 复制历史到子 session
            _conv_store.fork(fork_from, child_session_id)

        # Resume must reload the child's own durable history, not its parent's snapshot.
        stored = _conv_store.load(child_session_id)
        history = [_message_to_history_dict(m, current_task_id=task_id) for m in stored if m.role != "system"]
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
        dispatch_origin = input_data.get("_dispatch_origin")
        dispatch_origin = dispatch_origin if isinstance(dispatch_origin, dict) else {}
        if not session_provider_override and dispatch_origin.get("context_mode") == "fork" and dispatch_origin.get("parent_session_id"):
            session_provider_override = await _fetch_session_provider_override(
                http, sup_url, str(dispatch_origin["parent_session_id"]).strip(),
            )
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
        # Model defaults override runtime defaults, never explicit node options.
        # Sticky config is a lower-priority transport fallback: explicit runtime,
        # node and session settings must still be able to disable it.
        _po = _merge_provider_options(global_provider_options, {
            key: value for key, value in rp.provider_defaults.items() if key != "sticky_session"
        })
        _po = _merge_provider_options(_po, node_provider_options)
        context_mode = str(input_data.get("context_mode") or "").strip()
        if input_data.get("_system_task") or node.id.startswith("system."):
            sticky_id = f"node:{node.id}"
        elif child_session_id:
            if node.persistent or context_mode == "accumulate":
                parent_id = parent_session_id or session_id
                context_key = str(input_data.get("_context_key") or "").strip()
                sticky_id = f"{parent_id}:{node.id}:{context_key}" if not parent_id.startswith("branch_") else child_session_id
            else:
                sticky_id = f"node:{node.id}"
        else:
            sticky_id = parent_session_id or session_id
        if dispatch_origin.get("context_mode") == "fork":
            sticky_id = str(dispatch_origin.get("parent_sticky_session_id") or dispatch_origin.get("parent_session_id") or sticky_id).strip()
        rctx.sticky_session_id = sticky_id
        _po["_sticky_session_id"] = sticky_id
        _current_sticky_sid_var.set(sticky_id)
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
    if is_resume and (child_session_id or _main_conv_enabled):
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
    # [AutoC 2026-08-31] Why: the summary field has been removed from the kernel
    # contract; a dispatch_result callback is written when it carries raw text or
    # attachments. Purpose: ConversationStore keeps a structured callback row based
    # on substantive content only.
    _should_write_user_input = bool(
        instruction
        or input_attachments
        or _inbound_message_type == "dispatch_result"
    )
    if _should_write_user_input and _conv_store and not is_resume:
        from datetime import datetime, timezone
        _target_sid = child_session_id or session_id
        # A task's initial inbound is immutable across crash retries. Use its
        # ownership, not text/attachment equality; never discard preempt inputs
        # or a tail owned by another task. Inspect disk even with use_context=False.
        _rows = _conv_store.load(_target_sid)
        _tail = len(_rows) - 1
        while _tail >= 0 and is_dynamic_context(_rows[_tail]) and _rows[_tail].source_task_id == task_id:
            _tail -= 1
        if _tail >= 0:
            _row = _rows[_tail]
            if (
                task_id and _row.source_task_id == task_id and _row.role == "user"
                and _row.message_type in ("", MessageType.USER_INPUT, "dispatch_result", "async_tool_result")
                and not (_row.meta or {}).get("preempt")
            ):
                _dropped_ids = {row.id for row in _rows[_tail:] if row.id}
                _conv_store.remove_messages(_target_sid, _dropped_ids)
                history = [m for m in history if m.get("id") not in _dropped_ids]
        # [AutoC 2026-06-01] Why: persisting only plain instruction text lost
        # image attachments before the next step could reload ConversationStore.
        # How: when attachments are present, build the same multimodal content
        # that message assembly sends to the provider; otherwise keep plain text.
        # Images remain available when later tasks restore the same history.
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
        delivery_ids = input_data.get("delivery_ids") or []
        if not isinstance(delivery_ids, list) or any(not isinstance(i, str) or not i for i in delivery_ids):
            raise ValueError("invalid callback delivery identities")
        if delivery_ids:
            _inbound_meta["delivery_ids"] = list(delivery_ids)
            _inbound_meta["execution_id"] = rctx.execution_id
        if source_inbound_seq is not None:
            _inbound_meta["source_inbound_seq"] = int(source_inbound_seq)
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
            id=(str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([task_id, delivery_ids])))
                if delivery_ids else str(uuid.uuid4())),
            role="user",
            content=_user_content,
            message_type=_inbound_message_type or MessageType.USER_INPUT,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=_inbound_meta,
            source_node_id=node.id,
            source_task_id=task_id,
        )
        _conv_store.append(_target_sid, _user_msg)
        rctx._restart_input_persisted = True
        rctx.inbound_message_id = _user_msg.id
        rctx.inbound_message = _message_to_history_dict(_user_msg, current_task_id=task_id)
        await rctx.admit_input(delivery_ids, _user_msg.id)

    if not is_resume and _inbound_message_type in ("dispatch_result", "async_tool_result"):
        # Label system-delivered results only after persisting the raw inbound.
        # Resumes reuse history and must not append another callback message.
        # The two callback routes use the same wrapper, distinguished by kind.
        instruction = _wrap_dispatch_result_text(
            instruction, message_type=_inbound_message_type,
            source=_inbound_node_id, task_id=_inbound_child_task_id,
        )

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
        "action": action.action,
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
                error=action.error or "",
                child_session_id=child_session_id,
            )
            write_task_record(ws_root, _child_record)
    except Exception as _tr_err:
        print(f"[engine] Failed to write task record: {_tr_err}", flush=True)

    _result = action.to_dict()
    if rctx.response_info:
        _result["_execution_model"] = {
            "provider": str(rctx.response_info.get("provider") or ""),
            "model": str(rctx.response_info.get("model") or ""),
        }
    # Keep provenance outside result: output-chain model input receives only the answer.
    if rctx.response_info and action.action in ("finish", "ask"):
        _result["response_info"] = dict(rctx.response_info)
    # [AutoC 2026-06-09] Why: when a task is suspended (output chain) and
    # later resumed, the engine creates a fresh RunContext with empty
    # tool_call_log/total_usage. How: persist runtime state in the result
    # so the next claim can restore it. Purpose: plugins like
    # no_tool_finish_guard see the full task history, not just the
    # current claim's fragment.
    _result["_runtime_state"] = runtime_state(rctx)
    if getattr(rctx, "current_llm_request_id", "") and not _result.get("llm_request_id"):
        # [AutoC 2026-06-04] Why: some TaskAction producers are hooks rather than
        # pseudo handlers, but they still end the current provider request. How: fill
        # the task result from RunContext when the action did not set it itself.
        # Purpose: supervisor outbound events remain request-scoped for every finish path.
        _result["llm_request_id"] = rctx.current_llm_request_id
    # 轮摘要节点化：传递 _tool_call_count 和 _total_tokens 给 supervisor 做门控判断
    _result["_tool_call_count"] = _tool_call_count
    _result["_total_tokens"] = _total_tokens
    # Forward canonical actions for memory-extraction mutual exclusion.
    if rctx.tool_call_log:
        _result["_tool_actions"] = [
            {"name": tc["name"], "action": tc["action"]}
            for tc in rctx.tool_call_log if tc.get("name") and tc.get("action")
        ]
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
        "message": f"[tool] {tool_name} 完成",
        "task_id": task_id, "tool_name": tool_name,
    })

    return {
        "action": "finish",
        "node_id": tool_name,
        "result": {
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
